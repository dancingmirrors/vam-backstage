from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from ..core.graph import is_flexible_ref, parse_dep_ref, resolve_ref
from ..core.store import resolve_hub_download_url
from ..core.varfile import IntegrityError, parse_var_filename, verify_package_full, verify_zip_file
from .client import HubClient, HubError, is_null_find_entry
from .http import HttpTransport, TransportError, UrllibTransport

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 0.25
MAX_AUTO_RETRIES = 5
RETRY_BASE_DELAY = 2.0
PAUSED_SETTING = "downloads_paused"


class DownloadError(Exception):
    pass


class _Cancelled(Exception):
    pass


def ensure_var_ext(filename) -> str | None:
    if not filename:
        return None
    return filename if re.search(r"\.var$", filename, re.IGNORECASE) else filename + ".var"


def is_flexible_filename(filename: str | None) -> bool:
    if not filename:
        return False
    parts = re.sub(r"\.var$", "", filename, flags=re.IGNORECASE).split(".")
    if len(parts) < 3:
        return False
    last = parts[-1].lower()
    return last == "latest" or re.fullmatch(r"min\d+", last) is not None


def concrete_dep_filename(file: dict | None) -> str | None:
    if not file:
        return None
    name = file.get("packageName")
    ver = file.get("latest_version")
    if not name or not str(ver).isdigit():
        return None
    return f"{name}.{ver}.var"


def is_dep_ref_present_locally(ref, package_index, group_index) -> bool:
    if not ref:
        return False
    stem = re.sub(r"\.var$", "", str(ref), flags=re.IGNORECASE)
    if not stem:
        return False
    if resolve_ref(stem, package_index, group_index).resolved:
        return True
    return bool(group_index.get(stem))


def _file_size(file: dict) -> int | None:
    try:
        n = int(str(file.get("file_size") or "0"))
    except ValueError:
        return None
    return n or None


_CD_FILENAME = re.compile(r"""filename\*?\s*=\s*(?:UTF-8''|"?)([^";]+)""", re.IGNORECASE)


def served_filename(res) -> str | None:
    m = _CD_FILENAME.search(res.header("content-disposition") or "")
    if not m:
        return None
    name = os.path.basename(urllib.parse.unquote(m.group(1).strip().strip('"')).replace("\\", "/"))
    return name if parse_var_filename(name) else None


def announced_total(res, offset: int) -> int | None:
    if res.status == 206:
        m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", res.header("content-range") or "")
        if m and m.group(1) != "*":
            return int(m.group(1))
    length = (res.header("content-length") or "").strip()
    if length.isdigit():
        return offset + int(length)
    return None


@dataclass
class Transfer:
    id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    bytes_loaded: int = 0
    progress: int = 0
    speed: int = 0
    started: float = field(default_factory=time.monotonic)
    last_check: float = field(default_factory=time.monotonic)
    last_bytes: int = 0
    last_emit: float = 0.0


class DownloadManager:
    def __init__(self, lib, hub: HubClient, transport: HttpTransport | None = None, max_concurrent: int = 5):
        self.lib = lib
        self.hub = hub
        self.transport = transport or UrllibTransport()
        self.max_concurrent = max_concurrent
        self._lock = threading.RLock()
        self._active: dict[int, Transfer] = {}
        self._paused_progress: dict[int, tuple[int, int]] = {}
        self._retry_counts: dict[int, int] = {}
        self._retry_timers: dict[int, threading.Timer] = {}
        self._pending_dep_lookups: set[str] = set()
        self._threads: dict[int, threading.Thread] = {}
        self.paused = False
        self.on_integrated = None

    @property
    def db(self):
        return self.lib.db

    def _emit_updated(self) -> None:
        self.lib.notify("downloads:updated")

    def _emit_failed(self, entry: dict, error: str | None) -> None:
        label = (entry.get("display_name") or "").strip() or entry.get("package_ref") or "Download"
        self.lib.notify(
            "toast", {"type": "error", "message": f"Download failed: {label}" + (f" — {error}" if error else "")}
        )

    def init(self) -> None:
        self.db.clear_completed_downloads()
        if self.db.get_setting(PAUSED_SETTING) == "1":
            self.paused = True
            self.db.reset_active_downloads()
        else:
            for r in self.db.get_all_downloads():
                if r["temp_path"] and r["status"] in ("active", "queued"):
                    try:
                        os.unlink(r["temp_path"])
                    except OSError:
                        pass
            self.db.fail_unfinished_downloads()

    def shutdown(self) -> None:
        with self._lock:
            for t in self._active.values():
                t.cancel.set()
            for timer in self._retry_timers.values():
                timer.cancel()
            threads = list(self._threads.values())
        deadline = time.monotonic() + 8
        for th in threads:
            th.join(timeout=max(0.0, deadline - time.monotonic()))

    def _insert(
        self,
        package_ref: str,
        *,
        url: str,
        resource_id=None,
        file_size=None,
        priority: str = "dependency",
        parent_ref: str | None = None,
        display_name: str | None = None,
        auto_queue_deps: bool = True,
        reinstall: bool = False,
    ) -> bool:
        if os.path.basename(package_ref) != package_ref or parse_var_filename(package_ref) is None:
            log.warning("refusing to queue a file the Hub named %r", package_ref)
            return False
        store = self.lib.store
        if not reinstall and store.find_local_by_filename(package_ref) and not store.is_placeholder(package_ref):
            return False
        existing = self.db.get_download_by_ref(package_ref)
        if existing:
            if existing["status"] in ("queued", "active"):
                return False
            if existing.get("temp_path"):
                try:
                    os.unlink(existing["temp_path"])
                except OSError:
                    pass
            self.db.delete_download(existing["id"])
        rid = str(resource_id) if resource_id not in (None, "", "null") else None
        self.db.insert_download(
            {
                "package_ref": package_ref,
                "hub_resource_id": rid,
                "download_url": url,
                "file_size": file_size,
                "priority": priority,
                "parent_ref": parent_ref,
                "display_name": display_name,
                "auto_queue_deps": 1 if auto_queue_deps else 0,
                "reinstall": 1 if reinstall else 0,
            }
        )
        return True

    def enqueue_reinstall(self, filename: str) -> dict:
        fn = ensure_var_ext(filename)
        with self.lib.lock:
            pkg = self.lib.store.package_index.get(fn) if fn else None
        if not pkg:
            raise DownloadError(f"{filename} is not in the library")
        detail = None
        file = None
        rid = pkg.get("hub_resource_id")
        if rid:
            try:
                detail = self.hub.get_resource_detail(rid)
                file = next(
                    (
                        f
                        for f in detail.get("hubFiles") or []
                        if (ensure_var_ext(f.get("filename")) or "").lower() == fn.lower()
                        and resolve_hub_download_url(f)
                    ),
                    None,
                )
            except HubError as err:
                log.info("reinstall %s: resource %s unavailable (%s), looking the file up by name", fn, rid, err)
        if file is None:
            file = self._find_exact_file(fn)
        if file is None:
            raise DownloadError(f"{fn} is not available on the Hub")
        existing = self.db.get_download_by_ref(fn)
        if existing and existing["status"] in ("queued", "active"):
            raise DownloadError(f"{fn} is already in the download queue")
        title = (detail or {}).get("title") or pkg.get("hub_display_name") or pkg.get("title") or fn
        self._insert(
            fn,
            url=resolve_hub_download_url(file),
            resource_id=file.get("resource_id") or rid,
            file_size=_file_size(file),
            priority="direct" if pkg.get("is_direct") else "dependency",
            display_name=f"{title} (reinstall)",
            auto_queue_deps=True,
            reinstall=True,
        )
        self._emit_updated()
        self.process_queue()
        return {"ok": True, "filename": fn, "queued": True}

    def enqueue_reinstall_many(self, filenames) -> dict:
        queued, unavailable = [], []
        for fn in filenames:
            try:
                self.enqueue_reinstall(fn)
                queued.append(fn)
            except (DownloadError, HubError) as err:
                log.info("reinstall skipped for %s: %s", fn, err)
                unavailable.append(fn)
        return {"queued": queued, "unavailable": unavailable}

    def _find_exact_file(self, filename: str) -> dict | None:
        stem = re.sub(r"\.var$", "", filename, flags=re.IGNORECASE)
        file = self.hub.find_packages([stem]).get(stem)
        if is_null_find_entry(file) or (ensure_var_ext(file.get("filename")) or "").lower() != filename.lower():
            return None
        return file if resolve_hub_download_url(file) else None

    def enqueue_install(
        self,
        resource_id=None,
        hub_detail: dict | None = None,
        auto_queue_deps: bool = True,
        package_name: str | None = None,
        as_dependency: bool = False,
        target_filename: str | None = None,
    ) -> dict:
        target = ensure_var_ext(target_filename) if target_filename else None
        detail = hub_detail
        if detail is None:
            try:
                detail = (
                    self.hub.get_resource_detail(resource_id)
                    if resource_id
                    else self.hub.get_resource_detail_by_name(package_name or "")
                )
            except HubError as err:
                if not target or not err.not_found:
                    raise
                log.warning("resource %s is gone, resolving %s by name", resource_id, target)
        if not detail and not target:
            raise DownloadError("Resource not found on Hub")
        if detail and detail.get("user_id"):
            try:
                self.db.upsert_hub_user(
                    detail["user_id"],
                    detail.get("username"),
                    {
                        "user_id": detail["user_id"],
                        "username": detail.get("username"),
                        "avatar_date": detail.get("avatar_date"),
                    },
                )
            except Exception:
                pass
        hub_files = list((detail or {}).get("hubFiles") or [])
        if target:
            file = next(
                (f for f in hub_files if (ensure_var_ext(f.get("filename")) or "").lower() == target.lower()), None
            )
            if not file or not resolve_hub_download_url(file):
                file = self._find_exact_file(target)
            if not file:
                raise DownloadError(f"{target} is no longer available on the Hub")
            hub_files = [file]
        elif not hub_files:
            raise DownloadError("No downloadable files")

        title = (detail or {}).get("title") or None
        inserted = already_local = already_queued = 0
        with self.lib.lock:
            for f in hub_files:
                url = resolve_hub_download_url(f)
                fn = ensure_var_ext(f.get("filename"))
                if not url or not fn:
                    continue
                if self.lib.store.find_local_by_filename(fn) and not self.lib.store.is_placeholder(fn):
                    already_local += 1
                    continue
                existing = self.db.get_download_by_ref(fn)
                if existing and existing["status"] in ("queued", "active"):
                    already_queued += 1
                    continue
                if self._insert(
                    fn,
                    url=url,
                    resource_id=f.get("resource_id") or (detail or {}).get("resource_id"),
                    file_size=_file_size(f),
                    priority="dependency" if as_dependency else "direct",
                    display_name=title,
                    auto_queue_deps=auto_queue_deps,
                ):
                    inserted += 1
        if inserted + already_local + already_queued == 0:
            raise DownloadError("No download URL available")
        self._emit_updated()
        self.process_queue()
        main_ref = ensure_var_ext(hub_files[0].get("filename")) or ""
        unresolved = self._enqueue_missing_deps(detail, main_ref, auto_queue_deps) if detail else []
        self._emit_updated()
        self.process_queue()
        return {
            "ok": True,
            "inserted": inserted,
            "already_local": already_local,
            "already_queued": already_queued,
            "paused": self.paused,
            "unresolved_deps": unresolved,
        }

    def _enqueue_missing_deps(self, detail: dict, parent_ref: str, auto_queue_deps: bool) -> list[str]:
        deps = detail.get("dependencies") or {}
        if not deps:
            return []
        with self.lib.lock:
            package_index = dict(self.lib.store.package_index)
            group_index = {k: list(v) for k, v in self.lib.store.group_index.items()}
        needs_lookup: dict[str, None] = {}
        for group, files in deps.items():
            for f in files or []:
                if not isinstance(f, dict):
                    continue
                dep_fn = concrete_dep_filename(f)
                if dep_fn and dep_fn in package_index and not self.lib.store.is_placeholder(dep_fn):
                    continue
                url = resolve_hub_download_url(f) if dep_fn else None
                if dep_fn and url:
                    with self.lib.lock:
                        self._insert(
                            dep_fn,
                            url=url,
                            resource_id=f.get("resource_id"),
                            file_size=_file_size(f),
                            parent_ref=parent_ref,
                            auto_queue_deps=auto_queue_deps,
                        )
                    continue
                ref = re.sub(r"\.var$", "", f.get("filename") or "", flags=re.IGNORECASE)
                if (ref and is_dep_ref_present_locally(ref, package_index, group_index)) or is_dep_ref_present_locally(
                    f.get("packageName") or group, package_index, group_index
                ):
                    continue
                key = f.get("packageName") or f.get("filename") or group
                if key:
                    needs_lookup[key] = None
        if not needs_lookup:
            return []
        refs = list(needs_lookup)
        try:
            results = self.hub.find_packages(refs)
        except HubError as err:
            log.warning("findPackages failed: %s", err)
            return [r for r in refs if not is_dep_ref_present_locally(r, package_index, group_index)]
        unresolved = []
        for ref in refs:
            f = results.get(ref)
            dep_fn = ensure_var_ext(f.get("filename")) if f and not is_null_find_entry(f) else None
            if not dep_fn or is_flexible_filename(dep_fn):
                if not is_dep_ref_present_locally(ref, package_index, group_index):
                    unresolved.append(ref)
                continue
            if dep_fn in package_index:
                continue
            url = resolve_hub_download_url(f)
            if not url:
                if not is_dep_ref_present_locally(ref, package_index, group_index):
                    unresolved.append(ref)
                continue
            with self.lib.lock:
                self._insert(
                    dep_fn,
                    url=url,
                    resource_id=f.get("resource_id"),
                    file_size=_file_size(f),
                    parent_ref=parent_ref,
                    auto_queue_deps=auto_queue_deps,
                )
        return unresolved

    def _queue_refs(
        self, refs: list[str], parent_ref: str | None, auto_queue_deps: bool
    ) -> tuple[int, list[str], list[str]]:
        if not refs:
            return 0, [], []
        try:
            results = self.hub.find_packages(refs)
        except HubError as err:
            log.warning("findPackages failed: %s", err)
            return 0, list(refs), []
        queued = 0
        unresolved = []
        already_local = []
        with self.lib.lock:
            for ref in refs:
                f = results.get(ref)
                fn = ensure_var_ext(f.get("filename")) if f and not is_null_find_entry(f) else None
                if not fn or is_flexible_filename(fn):
                    unresolved.append(ref)
                    continue
                if self.lib.store.find_local_by_filename(fn) and not self.lib.store.is_placeholder(fn):
                    already_local.append(ref)
                    continue
                url = resolve_hub_download_url(f)
                if not url:
                    unresolved.append(ref)
                    continue
                if self._insert(
                    fn,
                    url=url,
                    resource_id=f.get("resource_id"),
                    file_size=_file_size(f),
                    parent_ref=parent_ref,
                    auto_queue_deps=auto_queue_deps,
                ):
                    queued += 1
        if queued:
            self._emit_updated()
            self.process_queue()
        return queued, unresolved, already_local

    def enqueue_install_refs(self, refs, parent_ref: str | None = None) -> dict:
        queued, unresolved, already = self._queue_refs(list(refs), parent_ref, True)
        return {"ok": True, "queued": queued, "unresolved_deps": unresolved, "already_installed": already}

    def enqueue_install_missing(self, package_filename: str, auto_queue_deps: bool = True) -> dict:
        with self.lib.lock:
            refs = sorted(self.lib.store.get_transitive_missing_refs(package_filename, include_fallbacks=True))
        if not refs:
            return {"ok": True, "queued": 0, "unresolved_deps": [], "already_installed": []}
        queued, unresolved, already = self._queue_refs(refs, package_filename, auto_queue_deps)
        return {"ok": True, "queued": queued, "unresolved_deps": unresolved, "already_installed": already}

    def enqueue_install_all_missing(self) -> dict:
        with self.lib.lock:
            store = self.lib.store
            refs: dict[str, None] = {}
            for fn, _pkg in store.demand_entries():
                for d in store.forward_deps.get(fn, ()):
                    if store.is_missing_edge(d):
                        refs[d.ref] = None
        if not refs:
            return {"ok": True, "queued": 0, "unresolved_deps": [], "already_installed": []}
        queued, unresolved, already = self._queue_refs(list(refs), None, True)
        return {"ok": True, "queued": queued, "unresolved_deps": unresolved, "already_installed": already}

    def enqueue_install_ref(self, hub_file: dict, as_dependency: bool = True) -> dict:
        fn = ensure_var_ext(hub_file.get("filename"))
        if not fn:
            raise DownloadError("No filename in hub data")
        if self.lib.store.find_local_by_filename(fn) and not self.lib.store.is_placeholder(fn):
            return {"ok": True, "already": True}
        existing = self.db.get_download_by_ref(fn)
        if existing and existing["status"] in ("queued", "active"):
            return {"ok": True, "queued": True}
        url = resolve_hub_download_url(hub_file)
        size = _file_size(hub_file)
        rid = hub_file.get("resource_id")
        found = bool(url)
        if not url:
            stem = re.sub(r"\.var$", "", fn, flags=re.IGNORECASE)
            f = self.hub.find_packages([stem]).get(stem)
            found = bool(f) and f.get("filename") not in (None, "null")
            if found:
                url = resolve_hub_download_url(f)
                size = size or _file_size(f)
                rid = rid or f.get("resource_id")
        if not url:
            raise DownloadError(
                f"The Hub lists {fn} but offers no download for it (paid, or hosted elsewhere)"
                if found
                else f"The Hub does not serve {fn}"
            )
        with self.lib.lock:
            self._insert(
                fn,
                url=url,
                resource_id=rid,
                file_size=size,
                priority="dependency" if as_dependency else "direct",
                auto_queue_deps=False,
            )
        self._emit_updated()
        self.process_queue()
        return {"ok": True}

    def list(self) -> list[dict]:
        rows = self.db.get_all_downloads()
        with self._lock:
            out = []
            for r in rows:
                live = self._active.get(r["id"])
                snap = self._paused_progress.get(r["id"])
                row = dict(r)
                row["progress"] = (
                    live.progress if live else (snap[0] if snap else (100 if r["status"] == "completed" else 0))
                )
                row["speed"] = live.speed if live else 0
                row["bytes_loaded"] = live.bytes_loaded if live else (snap[1] if snap else 0)
                out.append(row)
            return out

    def cancel(self, dl_id: int) -> None:
        row = self.db.get_download(dl_id)
        with self._lock:
            t = self._active.get(dl_id)
            if t:
                t.cancel.set()
            self._paused_progress.pop(dl_id, None)
            self._clear_retry(dl_id)
        self.db.cancel_download(dl_id)
        if row and row["temp_path"] and not t:
            try:
                os.unlink(row["temp_path"])
            except OSError:
                pass
        self._emit_updated()

    def retry(self, dl_id: int) -> None:
        with self._lock:
            self._clear_retry(dl_id)
        self.db.retry_download(dl_id)
        self._emit_updated()
        self.process_queue()

    def clear_completed(self) -> None:
        self.db.clear_completed_downloads()
        self._emit_updated()

    def clear_failed(self) -> None:
        self.db.clear_failed_downloads()
        self._emit_updated()

    def remove_failed(self, dl_id: int) -> None:
        row = self.db.get_download(dl_id)
        if row and row["status"] == "failed":
            self.db.delete_download(dl_id)
            self._emit_updated()

    def pause_all(self) -> None:
        self.paused = True
        self.db.set_setting(PAUSED_SETTING, "1")
        with self._lock:
            for dl_id, t in list(self._active.items()):
                self._paused_progress[dl_id] = (t.progress, t.bytes_loaded)
                t.cancel.set()
        self._emit_updated()

    def resume_all(self) -> None:
        self.paused = False
        self.db.set_setting(PAUSED_SETTING, None)
        self.db.reset_active_downloads()
        self._emit_updated()
        self.process_queue()

    def cancel_all(self) -> None:
        rows = self.db.get_all_downloads()
        temps = [r["temp_path"] for r in rows if r["temp_path"] and r["status"] != "completed"]
        with self._lock:
            for t in self._active.values():
                t.cancel.set()
            for timer in self._retry_timers.values():
                timer.cancel()
            self._retry_timers.clear()
            self._retry_counts.clear()
            self._paused_progress.clear()
        self.db.cancel_all_downloads()
        self.paused = False
        self.db.set_setting(PAUSED_SETTING, None)
        for p in temps:
            try:
                os.unlink(p)
            except OSError:
                pass
        self._emit_updated()

    def _clear_retry(self, dl_id: int) -> None:
        self._retry_counts.pop(dl_id, None)
        timer = self._retry_timers.pop(dl_id, None)
        if timer:
            timer.cancel()

    def network_online(self) -> None:
        if self.paused:
            return
        with self._lock:
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
        for t in timers:
            t.cancel()
        if timers:
            self.process_queue()

    def process_queue(self) -> None:
        if self.paused:
            return
        with self._lock:
            while len(self._active) < self.max_concurrent:
                nxt = self._pick_next()
                if not nxt:
                    break
                self._start(nxt)

    def _pick_next(self) -> dict | None:
        queued = [
            d
            for d in self.db.get_all_downloads()
            if d["status"] == "queued" and d["id"] not in self._active and d["id"] not in self._retry_timers
        ]
        queued.sort(key=lambda d: (0 if d["priority"] == "direct" else 1, d["created_at"], d["id"]))
        return queued[0] if queued else None

    def _start(self, entry: dict) -> None:
        transfer = Transfer(entry["id"])
        self._active[entry["id"]] = transfer
        self._paused_progress.pop(entry["id"], None)
        self.db.update_download_status(entry["id"], "active")
        th = threading.Thread(target=self._run, args=(entry, transfer), name=f"download-{entry['id']}", daemon=True)
        self._threads[entry["id"]] = th
        th.start()

    def _finish(self, dl_id: int) -> None:
        with self._lock:
            self._active.pop(dl_id, None)

    def _emit_progress(self, transfer: Transfer, file_size: int | None, final: bool = False) -> None:
        now = time.monotonic()
        if not final and now - transfer.last_emit < PROGRESS_INTERVAL:
            return
        elapsed = now - transfer.last_check
        if elapsed > 0:
            transfer.speed = int((transfer.bytes_loaded - transfer.last_bytes) / elapsed)
            transfer.last_check = now
            transfer.last_bytes = transfer.bytes_loaded
        if final:
            transfer.progress = 100
            transfer.speed = 0
        elif file_size:
            transfer.progress = min(99, int(transfer.bytes_loaded * 100 / file_size))
        transfer.last_emit = now
        self.lib.notify(
            "download:progress",
            {
                "id": transfer.id,
                "progress": transfer.progress,
                "speed": transfer.speed,
                "bytes_loaded": transfer.bytes_loaded,
                "file_size": file_size,
            },
        )

    def _run(self, entry: dict, transfer: Transfer) -> None:
        try:
            self._run_inner(entry, transfer)
        finally:
            with self._lock:
                self._threads.pop(entry["id"], None)

    def _run_inner(self, entry: dict, transfer: Transfer) -> None:
        dl_id = entry["id"]
        package_ref = entry["package_ref"]
        file_size = entry["file_size"]
        self._emit_updated()
        addon_dir = self.lib.dirs.main_dir_path()
        if not addon_dir:
            self._finish(dl_id)
            self.db.update_download_status(dl_id, "failed", error="VaM directory not configured")
            self._emit_failed(entry, "VaM directory not configured")
            self._emit_updated()
            self.process_queue()
            return
        final_path = os.path.join(addon_dir, package_ref)
        with self.lib.lock:
            placeholder = self.lib.store.is_placeholder(package_ref)
        if entry.get("reinstall") and not placeholder:
            with self.lib.lock:
                pkg = self.lib.store.package_index.get(package_ref)
                current = self.lib.dirs.resolve_content_path(pkg) if pkg else None
            if current and os.path.exists(current):
                final_path = current
        temp_path = final_path + ".tmp"
        row = self.db.get_download(dl_id)
        if not row or row["status"] == "cancelled":
            self._finish(dl_id)
            self._emit_updated()
            self.process_queue()
            return
        self.db.update_download_status(dl_id, "active", temp_path=temp_path)
        try:
            os.makedirs(addon_dir, exist_ok=True)
            existing = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
            transfer.bytes_loaded = existing
            headers = {"Cookie": "vamhubconsent=yes"}
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
            res = self.transport.request("GET", entry["download_url"], headers=headers, timeout=60)
            if existing > 0 and res.status == 206:
                m = re.match(r"bytes (\d+)-", res.header("content-range") or "")
                if not m or int(m.group(1)) != existing:
                    res.close()
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    raise TransportError("Server ignored the resume offset, will retry from scratch")
            if existing > 0 and res.status == 200:
                existing = 0
                transfer.bytes_loaded = 0
            elif existing > 0 and res.status == 416:
                res.close()
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise TransportError("Resume range rejected by server, will retry from scratch")
            elif not res.ok:
                res.close()
                raise DownloadError(f"HTTP {res.status}")
            ctype = (res.header("content-type") or "").split(";")[0].strip().lower()
            if ctype in ("text/html", "application/xhtml+xml"):
                res.close()
                raise DownloadError(
                    "The Hub answered with a web page instead of the file (resource gone, or it needs a login)"
                )
            served = served_filename(res)
            if served and served.lower() != package_ref.lower():
                try:
                    package_ref = self._adopt_served_name(entry, served, placeholder)
                except DownloadError:
                    res.close()
                    raise
                final_path = os.path.join(addon_dir, package_ref)
                with self.lib.lock:
                    placeholder = self.lib.store.is_placeholder(package_ref)
            total = announced_total(res, existing)
            if total and total != file_size:
                file_size = total
                self.db.update_download_fields(dl_id, file_size=total)
            with open(temp_path, "ab" if existing > 0 else "wb") as f:
                for chunk in res.iter_content():
                    if transfer.cancel.is_set():
                        res.close()
                        raise _Cancelled()
                    f.write(chunk)
                    transfer.bytes_loaded += len(chunk)
                    self._emit_progress(transfer, file_size)
            if total is not None and transfer.bytes_loaded < total:
                raise TransportError(f"connection closed after {transfer.bytes_loaded} of {total} bytes")
            if total is not None and transfer.bytes_loaded > total:
                raise DownloadError(f"Download corrupt (server announced {total} bytes, got {transfer.bytes_loaded})")
            if not verify_zip_file(temp_path):
                raise DownloadError("Download is not a valid package (truncated or corrupt)")
            if entry.get("reinstall") or placeholder:
                try:
                    verify_package_full(temp_path)
                except IntegrityError as err:
                    raise DownloadError(f"Downloaded file failed verification, local file kept: {err}") from err
            if placeholder:
                with self.lib.hold_scans():
                    try:
                        res_replace = self.lib.replace_placeholder(
                            package_ref,
                            temp_path,
                            move=True,
                            source_name=package_ref,
                            is_direct=entry["priority"] == "direct",
                        )
                    except ValueError as err:
                        raise DownloadError(str(err)) from err
                    self._finish(dl_id)
                    with self._lock:
                        self._retry_counts.pop(dl_id, None)
                    self.db.update_download_status(dl_id, "completed", temp_path=None)
                    self._emit_progress(transfer, file_size, final=True)
                    log.info("%s: the real package replaced the %s", package_ref, res_replace.get("replaced"))
                    self.lib.notify(
                        "toast",
                        {"message": f"{package_ref}: the real package replaced the {res_replace.get('replaced')}"},
                    )
                    self._after_integration(entry, {"filename": package_ref, "landed": res_replace.get("landed")})
                self._emit_updated()
                return
            if entry.get("reinstall"):
                if os.path.isdir(final_path):
                    shutil.rmtree(final_path)
                    for leftover in (final_path + ".orig", final_path + ".depend.txt"):
                        try:
                            os.unlink(leftover)
                        except OSError:
                            pass
            with self.lib.hold_scans():
                os.replace(temp_path, final_path)
                self._finish(dl_id)
                with self._lock:
                    self._retry_counts.pop(dl_id, None)
                self.db.update_download_status(dl_id, "completed", temp_path=None)
                self._emit_progress(transfer, file_size, final=True)
                self._integrate(entry, final_path)
            self._emit_updated()
        except _Cancelled:
            self._finish(dl_id)
            if not self.paused:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                self.db.cancel_download(dl_id)
        except TransportError as err:
            self._finish(dl_id)
            with self._lock:
                attempt = self._retry_counts.get(dl_id, 0) + 1
                if attempt <= MAX_AUTO_RETRIES and not self.paused:
                    self._retry_counts[dl_id] = attempt
                    delay = RETRY_BASE_DELAY * 2 ** (attempt - 1)
                    log.warning(
                        "Download %s network error (attempt %d/%d), retrying in %.0fs: %s",
                        package_ref,
                        attempt,
                        MAX_AUTO_RETRIES,
                        delay,
                        err,
                    )
                    self.db.update_download_status(dl_id, "queued")
                    timer = threading.Timer(delay, self._retry_fire, args=(dl_id,))
                    timer.daemon = True
                    self._retry_timers[dl_id] = timer
                    timer.start()
                    retry = True
                else:
                    retry = False
            if not retry and not self.paused:
                with self._lock:
                    self._retry_counts.pop(dl_id, None)
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                self.db.update_download_status(
                    dl_id, "failed", error=f"Network error after {MAX_AUTO_RETRIES} retries: {err}"
                )
                self._emit_failed(entry, str(err))
            self._emit_updated()
        except Exception as err:
            self._finish(dl_id)
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            log.warning("Download %s failed: %s", package_ref, err)
            self.db.update_download_status(dl_id, "failed", error=str(err))
            self._emit_failed(entry, str(err))
            self._emit_updated()
        self.process_queue()

    def _retry_fire(self, dl_id: int) -> None:
        with self._lock:
            self._retry_timers.pop(dl_id, None)
        self.process_queue()

    def _adopt_served_name(self, entry: dict, served: str, placeholder: bool = False) -> str:
        ref = entry["package_ref"]
        if entry.get("reinstall") and not placeholder:
            raise DownloadError(f"The Hub no longer serves {ref} (it offers {served} instead); local file kept")
        with self.lib.lock:
            local = self.lib.store.find_local_by_filename(served) and not self.lib.store.is_placeholder(served)
        if local:
            raise DownloadError(f"The Hub served {served} instead, which is already installed")
        if not self.db.rename_download(entry["id"], served):
            raise DownloadError(f"The Hub served {served} instead, which is already queued")
        log.warning("Hub served %s for %s; keeping it under its real name", served, ref)
        entry["package_ref"] = served
        entry["substituted_for"] = ref
        self.lib.notify(
            "toast", {"message": f"The Hub served {served} instead of {ref}; installing it under its real name"}
        )
        self._emit_updated()
        return served

    def _integrate(self, entry: dict, final_path: str) -> None:
        rid = entry.get("hub_resource_id")
        detail = self.hub.get_cached_detail(rid) if rid else None
        try:
            if entry.get("reinstall"):
                res = self.lib.refresh_package_file(entry["package_ref"], hub_detail=detail)
            else:
                res = self.lib.integrate_new_var(
                    final_path, is_direct=entry["priority"] == "direct", hub_resource_id=rid, hub_detail=detail
                )
        except Exception as err:
            log.exception("post-download integration failed for %s", entry["package_ref"])
            self.db.update_download_status(entry["id"], "failed", error=f"Downloaded, but could not be indexed: {err}")
            if not entry.get("reinstall") and not verify_zip_file(final_path):
                try:
                    os.unlink(final_path)
                except OSError:
                    pass
            self._emit_failed(entry, str(err))
            return
        self._after_integration(entry, res)

    def _after_integration(self, entry: dict, res: dict | None) -> None:
        if not res:
            return
        if entry.get("auto_queue_deps") and res.get("landed") != "archived":
            self._auto_queue_transitive_deps(res["filename"], entry)
        if self.on_integrated and not entry.get("reinstall"):
            try:
                self.on_integrated(res["filename"])
            except Exception:
                log.exception("post-download enrichment hook failed")

    def _auto_queue_transitive_deps(self, filename: str, entry: dict) -> None:
        with self.lib.lock:
            store = self.lib.store
            missing = [
                d.ref
                for d in store.forward_deps.get(filename, ())
                if d.ref and (not d.resolved or store.is_placeholder(d.resolved))
            ]
            if not missing:
                return
            queued_bases = set()
            for d in self.db.get_all_downloads():
                if d["status"] in ("queued", "active"):
                    p = parse_dep_ref(re.sub(r"\.var$", "", d["package_ref"], flags=re.IGNORECASE))
                    if p:
                        queued_bases.add(p.package_name)
            truly = []
            with self._lock:
                for ref in missing:
                    if ref in self._pending_dep_lookups:
                        continue
                    fn = ensure_var_ext(ref) or ref
                    if (
                        store.find_local_by_filename(fn) and not store.is_placeholder(fn)
                    ) or self.db.get_download_by_ref(fn):
                        continue
                    p = parse_dep_ref(ref)
                    if is_flexible_ref(p) and p and p.package_name in queued_bases:
                        continue
                    truly.append(ref)
                self._pending_dep_lookups.update(truly)
        if not truly:
            return
        root_parent = entry.get("parent_ref") or filename
        try:
            self._queue_refs(list(dict.fromkeys(truly)), root_parent, True)
        except Exception:
            log.exception("transitive dep discovery failed")
        finally:
            with self._lock:
                self._pending_dep_lookups.difference_update(truly)
