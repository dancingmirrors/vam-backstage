"""Thumbnails and author avatars.

Keys: `pkg:<filename>` (package card), `ct:<filename>\\0<internal thumb path>`
(content card), `hub-icon:<resource id>` (Hub card). Resolved from, in order,
the companion .jpg next to the .var, the cached Hub icon, and the
representative content image extracted from the archive. Extracted images are
persisted under the cache dir so later launches never re-open archives."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core.paths import is_local_package, user_cache_dir
from ..core.varfile import extract_files
from .client import hub_resource_icon_url
from .http import HttpTransport, TransportError, UrllibTransport

log = logging.getLogger(__name__)

_MEM_ENTRIES = 500
_CDN_WORKERS = 20
_NOTIFY_EVERY = 25


def ct_cache_filename(package_filename: str, internal_path: str) -> str:
    h = hashlib.sha1(internal_path.encode("utf-8")).hexdigest()[:16]
    return f"{package_filename}__{h}.jpg"


def hub_icon_cache_name(rid: str) -> str:
    return f"hub-icon-{rid}.jpg"


def _read(path: str | Path) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


class ThumbnailCache:
    def __init__(self, lib, transport: HttpTransport | None = None, cache_dir: str | Path | None = None):
        self.lib = lib
        self.transport = transport or UrllibTransport()
        self.cache_dir = Path(cache_dir) if cache_dir else user_cache_dir() / "thumb-cache"
        self._mem: OrderedDict[str, bytes | None] = OrderedDict()
        self._lock = threading.Lock()
        self._resolving = False

    def _ensure_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _mem_get(self, key: str):
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return True, self._mem[key]
        return False, None

    def _mem_set(self, key: str, value: bytes | None) -> None:
        with self._lock:
            self._mem[key] = value
            self._mem.move_to_end(key)
            while len(self._mem) > _MEM_ENTRIES:
                self._mem.popitem(last=False)

    def invalidate(self, keys: Iterable[str]) -> None:
        with self._lock:
            for k in keys:
                self._mem.pop(k, None)

    def hub_icon_path(self, rid) -> Path:
        return self.cache_dir / hub_icon_cache_name(str(rid))

    def _persist_ct(self, package_filename: str, internal_path: str, data: bytes) -> None:
        try:
            self._ensure_dir()
            (self.cache_dir / ct_cache_filename(package_filename, internal_path)).write_bytes(data)
        except OSError:
            pass

    def get_hub_icon(self, rid, image_url: str | None = None) -> bytes | None:
        rid = str(rid)
        key = "hub-icon:" + rid
        hit, val = self._mem_get(key)
        if hit:
            return val
        data = _read(self.hub_icon_path(rid))
        failed = False
        if data is None:
            url = image_url or hub_resource_icon_url(rid)
            if url:
                try:
                    res = self.transport.request("GET", url, timeout=30)
                    if res.ok:
                        data = res.read() or None
                        if data:
                            self._ensure_dir()
                            self.hub_icon_path(rid).write_bytes(data)
                    elif res.status >= 500:
                        failed = True
                except (TransportError, OSError):
                    failed = True
        if data is not None or not failed:
            self._mem_set(key, data)
        return data

    def get(self, key: str) -> bytes | None:
        hit, val = self._mem_get(key)
        if hit:
            return val
        if key.startswith("hub-icon:"):
            return self.get_hub_icon(key[9:])
        vam_dir = self.lib.vam_dir
        if not vam_dir:
            return None
        data = None
        if key.startswith("pkg:"):
            data = self._package_thumb(key[4:])
        elif key.startswith("ct:"):
            sep = key.find("\0", 3)
            if sep >= 0:
                filename, thumb_path = key[3:sep], key[sep + 1 :]
                if thumb_path:
                    if is_local_package(filename):
                        data = _read(os.path.join(vam_dir, thumb_path))
                    else:
                        data = self._content_thumbs(filename, [thumb_path]).get(thumb_path)
        self._mem_set(key, data)
        return data

    def get_many(self, keys: Iterable[str]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {}
        by_var: dict[str, list[tuple[str, str]]] = {}
        for key in keys:
            hit, val = self._mem_get(key)
            if hit:
                out[key] = val
                continue
            if key.startswith("ct:") and not is_local_package(
                key[3 : key.find("\0", 3)] if key.find("\0", 3) > 0 else ""
            ):
                sep = key.find("\0", 3)
                if sep > 0:
                    by_var.setdefault(key[3:sep], []).append((key, key[sep + 1 :]))
                    continue
            out[key] = self.get(key)
        for filename, items in by_var.items():
            found = self._content_thumbs(filename, [p for _, p in items])
            for key, p in items:
                out[key] = found.get(p)
                self._mem_set(key, out[key])
        return out

    def _package_thumb(self, filename: str) -> bytes | None:
        if is_local_package(filename):
            return None
        with self.lib.lock:
            pkg = self.lib.store.package_index.get(filename)
            nominal = self.lib.dirs.pkg_var_path(pkg) if pkg else None
        if not pkg:
            return None
        if nominal:
            data = _read(
                os.path.join(os.path.dirname(nominal), re.sub(r"\.var$", ".jpg", filename, flags=re.IGNORECASE))
            )
            if data:
                return data
        internal = self.lib.db.get_content_thumbnail_path(filename)
        prefer_own = bool(internal) and bool(pkg.get("hub_detail_applied_at")) and not pkg.get("hub_display_name")
        if prefer_own:
            data = self._content_thumbs(filename, [internal]).get(internal)
            if data:
                return data
        if pkg.get("hub_resource_id"):
            data = _read(self.hub_icon_path(pkg["hub_resource_id"]))
            if data:
                return data
        if internal and not prefer_own:
            return self._content_thumbs(filename, [internal]).get(internal)
        return None

    def _content_thumbs(self, filename: str, internal_paths: list[str]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {}
        need = []
        for p in internal_paths:
            data = _read(self.cache_dir / ct_cache_filename(filename, p))
            if data is not None:
                out[p] = data
            else:
                need.append(p)
        if not need:
            return out
        var_path = self.lib.package_path(filename)
        if not var_path:
            return {**out, **{p: None for p in need}}
        try:
            extracted = extract_files(var_path, need)
        except Exception:
            extracted = {}
        for p in need:
            data = extracted.get(p)
            out[p] = data
            if data:
                self._persist_ct(filename, p, data)
        return out

    def resolve_package_thumbnails(self, packages_index: dict | None = None) -> int:
        with self._lock:
            if self._resolving:
                return 0
            self._resolving = True
        try:
            pending = self.lib.db.get_packages_needing_thumbnail()
            if not pending:
                return 0
            self._ensure_dir()
            if packages_index:
                for p in pending:
                    if not p["hub_resource_id"]:
                        entry = packages_index.get(p["package_name"])
                        if entry:
                            p["hub_resource_id"] = str(entry["resource_id"])
                            self.lib.db.set_hub_resource_id(p["filename"], p["hub_resource_id"])
            by_rid: dict[str, list[dict]] = {}
            for p in pending:
                if p["hub_resource_id"]:
                    by_rid.setdefault(str(p["hub_resource_id"]), []).append(p)
            batch: list[str] = []
            resolved = 0

            def flush() -> None:
                nonlocal batch
                if batch:
                    keys, batch = batch, []
                    self.invalidate(keys)
                    self.lib.notify("thumbnails:updated", {"keys": keys})

            def one(item):
                rid, pkgs = item
                url = hub_resource_icon_url(rid)
                if not url:
                    return []
                try:
                    res = self.transport.request("GET", url, timeout=30)
                    if not res.ok:
                        return []
                    data = res.read()
                    if not data:
                        return []
                    self.hub_icon_path(rid).write_bytes(data)
                except (TransportError, OSError):
                    return []
                keys = []
                for pkg in pkgs:
                    self.lib.db.set_package_thumbnail(pkg["filename"], url)
                    keys.append("pkg:" + pkg["filename"])
                return keys

            with ThreadPoolExecutor(max_workers=_CDN_WORKERS) as pool:
                for keys in pool.map(one, list(by_rid.items())):
                    if keys:
                        resolved += len(keys)
                        batch.extend(keys)
                        if len(batch) >= _NOTIFY_EVERY:
                            flush()
            flush()
            return resolved
        finally:
            with self._lock:
                self._resolving = False


class AvatarCache:

    def __init__(self, transport: HttpTransport | None = None, cache_dir: str | Path | None = None):
        self.transport = transport or UrllibTransport()
        self.cache_dir = Path(cache_dir) if cache_dir else user_cache_dir() / "avatar-cache"
        self._index: dict[str, dict] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _safe(s) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", str(s))

    def avatar_file(self, user_id, avatar_date) -> Path:
        return self.cache_dir / f"{self._safe(user_id)}_{self._safe(avatar_date)}.jpg"

    def _load_index(self) -> dict[str, dict]:
        if self._index is None:
            try:
                self._index = json.loads((self.cache_dir / "index.json").read_text("utf-8"))
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def _save_index(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "index.json").write_text(json.dumps(self._index or {}), "utf-8")

    def cache_from_resources(self, resources: Iterable[dict]) -> int:
        seen = set()
        items = []
        for r in resources:
            if r and r.get("icon_url") and r.get("user_id") and r.get("avatar_date") and r["user_id"] not in seen:
                seen.add(r["user_id"])
                items.append(r)
        if not items:
            return 0
        written = 0
        with self._lock:
            idx = self._load_index()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            for r in items:
                uid = str(r["user_id"])
                existing = idx.get(uid)
                if existing and existing.get("avatarDate") == r["avatar_date"]:
                    continue
                try:
                    res = self.transport.request("GET", r["icon_url"], timeout=30)
                    if not res.ok:
                        continue
                    data = res.read()
                    if existing:
                        try:
                            self.avatar_file(uid, existing["avatarDate"]).unlink()
                        except OSError:
                            pass
                    self.avatar_file(uid, r["avatar_date"]).write_bytes(data)
                    idx[uid] = {"avatarDate": r["avatar_date"]}
                    written += 1
                except (TransportError, OSError):
                    continue
            if written:
                self._save_index()
        return written

    def get(self, user_id) -> bytes | None:
        with self._lock:
            entry = self._load_index().get(str(user_id))
        if not entry:
            return None
        return _read(self.avatar_file(user_id, entry["avatarDate"]))
