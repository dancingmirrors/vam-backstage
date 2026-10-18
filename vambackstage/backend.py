from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .core.library import Library
from .core.paths import user_cache_dir
from .core.store import resolve_hub_download_url
from .core.watcher import LibraryWatcher
from .hub.client import HubClient
from .hub.downloads import DownloadManager
from .hub.enrich import enrich_new_packages, retry_pending_hub_details, scan_hub_details
from .hub.http import HttpTransport, UrllibTransport
from .hub.packages_json import PackagesJsonIndex
from .hub.thumbnails import AvatarCache, ThumbnailCache

log = logging.getLogger(__name__)

IGNORED_UPDATES_SETTING = "ignored_updates"


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Backend:
    def __init__(
        self,
        db_path: str | Path | None = None,
        transport: HttpTransport | None = None,
        cache_dir: str | Path | None = None,
    ):
        self.lib = Library(db_path)
        self.events = self.lib.events
        self.transport = transport or UrllibTransport()
        cache = Path(cache_dir) if cache_dir else user_cache_dir()
        self.cache_dir = cache
        self.hub = HubClient(self.lib.db, self.events, self.transport)
        self.packages_json = PackagesJsonIndex(self.lib.db, self.transport)
        self.thumbs = ThumbnailCache(self.lib, self.transport, cache / "thumb-cache")
        self.avatars = AvatarCache(self.transport, cache / "avatar-cache")
        self.downloads = DownloadManager(self.lib, self.hub, self.transport)
        self.watcher = LibraryWatcher(self.lib, self._on_library_changed)
        self.downloads.on_integrated = lambda fn: self.run_in_thread(self._enrich_new, [fn])
        self.lib.on_new_packages = lambda fns: self.run_in_thread(self._enrich_new, fns)
        self.lib.install_missing_hook = lambda fn: self.downloads.enqueue_install_missing(fn)["queued"]
        if self.packages_json.load_from_cache():
            self._catalog_updated(rebuild=False)

    def start(self) -> None:
        self.lib.load()
        self.downloads.init()
        self.start_watcher()

    def start_watcher(self) -> None:
        if self.lib.is_configured and self.lib.initial_scan_done and self.lib.get_setting("watch_library") != "0":
            self.watcher.start()

    def stop_watcher(self) -> None:
        self.watcher.stop(join=False)

    def _on_library_changed(self) -> None:
        summary = self.lib.scan()
        if summary.added or summary.removed or summary.replaced:
            log.info(
                "library changed on disk: %d added, %d removed, %d placeholders replaced",
                summary.added,
                summary.removed,
                len(summary.replaced),
            )
            msg = f"Library changed on disk: {summary.added} added, {summary.removed} removed"
            if summary.replaced:
                msg += f"; the real package replaced {len(summary.replaced)} placeholder(s): " + ", ".join(
                    summary.replaced[:3]
                )
            self.lib.notify("toast", {"message": msg})

    def close(self) -> None:
        self.watcher.stop()
        self.downloads.shutdown()
        self.lib.close()

    def run_in_thread(self, fn: Callable, *args, name: str | None = None) -> threading.Thread:
        def runner():
            try:
                fn(*args)
            except Exception:
                log.exception("background task failed: %s", getattr(fn, "__name__", fn))

        th = threading.Thread(target=runner, name=name or getattr(fn, "__name__", "task"), daemon=True)
        th.start()
        return th

    def _catalog_updated(self, rebuild: bool = True) -> None:
        with self.lib.lock:
            self.lib.store.set_hub_filename_index(self.packages_json.filename_index)
            if rebuild:
                self.lib.store.rebuild_replaceable_set()
                self.lib.store.recompute_demand_aggregates()

    def refresh_catalog(self, force: bool = False, max_age: float = 300.0) -> bool:
        before = self.packages_json.filename_index
        ok = self.packages_json.ensure_fresh(max_age=max_age, force=force)
        if ok and self.packages_json.filename_index is not before:
            self._catalog_updated()
            self.lib.notify("packages:updated")
        return ok

    def check_updates(self, force: bool = False) -> dict | None:
        self.refresh_catalog(force=force)
        with self.lib.lock:
            s = self.lib.store
            updates = self.packages_json.check_updates(s.package_index, s.group_index, s.forward_deps)
        if not updates:
            return updates
        ignored = self.ignored_updates()
        return {
            fn: u
            for fn, u in updates.items()
            if (u.get("package_name"), _int_or_none(u.get("hub_version"))) not in ignored
        }

    def ignored_updates(self) -> set[tuple[str, int]]:
        raw = self.lib.get_setting(IGNORED_UPDATES_SETTING)
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            items = []
        out = set()
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) == 2 and _int_or_none(item[1]) is not None:
                out.add((str(item[0]), int(item[1])))
        return out

    def ignore_updates(self, entries) -> int:
        ignored = self.ignored_updates()
        before = len(ignored)
        for u in entries:
            v = _int_or_none(u.get("hub_version"))
            if u.get("package_name") and v is not None:
                ignored.add((u["package_name"], v))
        self.lib.set_setting(IGNORED_UPDATES_SETTING, json.dumps(sorted(ignored)))
        return len(ignored) - before

    def clear_ignored_updates(self) -> int:
        n = len(self.ignored_updates())
        self.lib.set_setting(IGNORED_UPDATES_SETTING, None)
        return n

    def probe_updates(self, entries) -> dict[str, dict]:
        names = {}
        for u in entries:
            fn = u.get("hub_filename") or ""
            if fn.lower().endswith(".var"):
                names[fn] = fn[:-4]
        if not names:
            return {}
        results = self.hub.find_packages(list(names.values()))
        out = {}
        for fn, stem in names.items():
            f = results.get(stem)
            found = bool(f) and f.get("filename") not in (None, "null")
            out[fn] = {
                "found": found,
                "downloadable": found and bool(resolve_hub_download_url(f)),
                "resource_id": (f.get("resource_id") if found and f.get("resource_id") not in ("null", "") else None),
            }
        return out

    def enqueue_updates(self, updates) -> dict:
        from .hub.downloads import DownloadError

        queued = 0
        already = 0
        failed: list[tuple[str, str]] = []
        for u in updates:
            fn = u.get("hub_filename")
            if not fn:
                continue
            try:
                res = self.downloads.enqueue_install_ref(
                    {"filename": fn, "resource_id": u.get("hub_resource_id")}, as_dependency=False
                )
            except DownloadError as err:
                failed.append((fn, str(err)))
                continue
            if res.get("already"):
                already += 1
            else:
                queued += 1
        return {"queued": queued, "already": already, "failed": failed}

    def missing_deps(self) -> list[dict]:
        self.refresh_catalog()
        return self.lib.missing_deps(self.packages_json.index, self.packages_json.filename_index)

    def enrich_hub(self, on_progress=None) -> dict:
        result = scan_hub_details(self.lib, self.hub, self.packages_json, on_progress)
        self._catalog_updated()
        self.thumbs.resolve_package_thumbnails(self.packages_json.index)
        return result

    def _enrich_new(self, filenames: list[str]) -> None:
        self.refresh_catalog()
        changed = enrich_new_packages(self.lib, self.hub, self.packages_json, filenames)
        if self.retry_pending_hub_details()["fetched"] or changed:
            self.thumbs.resolve_package_thumbnails(self.packages_json.index)

    def retry_pending_hub_details(self) -> dict:
        return retry_pending_hub_details(self.lib, self.hub)

    def resolve_thumbnails(self) -> int:
        return self.thumbs.resolve_package_thumbnails(self.packages_json.index)
