from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Mapping

from ..core.db import Database
from ..core.graph import DepEdge
from ..core.paths import is_package_archived
from .http import HttpTransport, TransportError, UrllibTransport

log = logging.getLogger(__name__)

PACKAGES_JSON_URL = "https://s3cdn.virtamate.com/data/packages.json"
DB_KEY_DATA = "packages_json_data"
DB_KEY_ETAG = "packages_json_etag"


def parse_package_entry(key: str) -> tuple[str, int] | None:
    stem = key[:-4] if key.lower().endswith(".var") else key
    parts = stem.split(".")
    if len(parts) < 3 or not parts[-1].isdigit():
        return None
    return ".".join(parts[:-1]), int(parts[-1])


def build_indexes(data: Mapping[str, int]) -> tuple[dict[str, dict], dict[str, int]]:
    index: dict[str, dict] = {}
    fn_index: dict[str, int] = {}
    for key, rid in data.items():
        fn_index[key] = rid
        parsed = parse_package_entry(key)
        if not parsed:
            continue
        name, version = parsed
        existing = index.get(name)
        if existing is None or version > existing["version"]:
            index[name] = {"version": version, "filename": key, "resource_id": rid}
    return index, fn_index


class PackagesJsonIndex:
    def __init__(self, db: Database, transport: HttpTransport | None = None, timeout: float = 60.0):
        self.db = db
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.index: dict[str, dict] | None = None
        self.filename_index: dict[str, int] | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    @property
    def age_seconds(self) -> float:
        return (time.monotonic() - self._fetched_at) if self._fetched_at else float("inf")

    def load_from_cache(self) -> bool:
        raw = self.db.get_setting(DB_KEY_DATA)
        if not raw:
            return False
        try:
            data = json.loads(raw)
        except ValueError as err:
            log.warning("packages.json cache unreadable: %s", err)
            return False
        self.index, self.filename_index = build_indexes(data)
        log.info("packages.json: loaded %d groups (%d files) from cache", len(self.index), len(self.filename_index))
        return True

    def fetch(self, force: bool = False) -> dict[str, dict] | None:
        with self._lock:
            headers = {}
            etag = self.db.get_setting(DB_KEY_ETAG)
            if etag and not force and self.index is not None:
                headers["If-None-Match"] = etag
            try:
                res = self.transport.request(
                    "GET", f"{PACKAGES_JSON_URL}?cb={uuid.uuid4()}", headers=headers, timeout=self.timeout
                )
            except TransportError as err:
                raise RuntimeError(f"packages.json unreachable: {err}") from err
            if res.status == 304:
                self._fetched_at = time.monotonic()
                return self.index
            if not res.ok:
                raise RuntimeError(f"packages.json HTTP {res.status}")
            text = res.read().decode("utf-8")
            data = json.loads(text)
            self.index, self.filename_index = build_indexes(data)
            self._fetched_at = time.monotonic()
            log.info("packages.json: fetched %d groups (%d files)", len(self.index), len(self.filename_index))
            try:
                self.db.set_setting(DB_KEY_DATA, text)
                new_etag = res.header("etag")
                if new_etag:
                    self.db.set_setting(DB_KEY_ETAG, new_etag)
            except Exception as err:
                log.warning("packages.json cache write failed: %s", err)
            return self.index

    def ensure_fresh(self, max_age: float = 300.0, force: bool = False) -> bool:
        if force or self.index is None or self.age_seconds > max_age:
            try:
                self.fetch(force=force)
            except Exception as err:
                log.warning("packages.json refresh failed: %s", err)
        return self.index is not None

    def invalidate_cache(self) -> None:
        self.db.set_setting(DB_KEY_ETAG, None)

    def check_updates(
        self,
        package_index: Mapping[str, dict],
        group_index: Mapping[str, list[str]],
        forward_deps: Mapping[str, list[DepEdge]] | None = None,
    ) -> dict[str, dict] | None:
        if self.index is None:
            return None
        updates: dict[str, dict] = {}

        def local_newer(filenames, exclude, hub_version):
            best = None
            best_v = -1
            for fn in filenames:
                if fn == exclude:
                    continue
                pkg = package_index.get(fn)
                if not pkg or is_package_archived(pkg.get("storage_state")):
                    continue
                v = _int(pkg.get("version"))
                if v >= hub_version and v > best_v:
                    best_v = v
                    best = fn
            return best

        for name, filenames in group_index.items():
            best = None
            best_ver = -1
            for fn in filenames:
                pkg = package_index.get(fn)
                if not pkg or not pkg.get("is_direct") or is_package_archived(pkg.get("storage_state")):
                    continue
                v = _int(pkg.get("version"))
                if v > best_ver:
                    best_ver = v
                    best = fn
            if best is None:
                continue
            entry = self.index.get(name)
            if not entry or entry["version"] <= best_ver:
                continue
            if local_newer(filenames, best, entry["version"]):
                continue
            updates[best] = {
                "current_version": best_ver,
                "hub_version": entry["version"],
                "hub_filename": entry["filename"],
                "hub_resource_id": str(entry["resource_id"]),
                "package_name": name,
                "local_newer_filename": None,
            }

        if forward_deps:
            latest: dict[str, dict] = {}
            for filename, deps in forward_deps.items():
                if is_package_archived((package_index.get(filename) or {}).get("storage_state")):
                    continue
                for dep in deps:
                    if dep.resolution != "latest" or not dep.resolved:
                        continue
                    pkg = package_index.get(dep.resolved)
                    if not pkg or is_package_archived(pkg.get("storage_state")):
                        continue
                    e = latest.setdefault(
                        pkg["package_name"],
                        {
                            "resolved_filename": dep.resolved,
                            "resolved_version": _int(pkg.get("version")),
                            "needed_by": [],
                        },
                    )
                    if filename not in e["needed_by"]:
                        e["needed_by"].append(filename)
            for name, info in latest.items():
                if info["resolved_filename"] in updates:
                    continue
                entry = self.index.get(name)
                if not entry or entry["version"] <= info["resolved_version"]:
                    continue
                if local_newer(group_index.get(name, []), info["resolved_filename"], entry["version"]):
                    continue
                updates[info["resolved_filename"]] = {
                    "current_version": info["resolved_version"],
                    "hub_version": entry["version"],
                    "hub_filename": entry["filename"],
                    "hub_resource_id": str(entry["resource_id"]),
                    "package_name": name,
                    "local_newer_filename": None,
                    "is_dep_update": True,
                    "needed_by": list(info["needed_by"]),
                }
        return updates


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
