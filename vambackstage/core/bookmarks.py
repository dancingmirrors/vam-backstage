from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Iterable, Mapping

from ..hub.client import hub_package_base_name
from .licenses import hub_resource_license
from .store import resolve_hub_download_url

KEY_PREFIX = "bookmark:"


def bookmark_key(resource_id) -> str:
    return f"{KEY_PREFIX}{resource_id}"


def is_bookmark_key(key) -> bool:
    return isinstance(key, str) and key.startswith(KEY_PREFIX)


def bookmark_resource_id(key: str) -> str:
    return key[len(KEY_PREFIX) :] if is_bookmark_key(key) else key


def _int(value) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _hub_files(data: Mapping) -> list[dict]:
    files = data.get("hubFiles")
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, dict) and f.get("filename") not in (None, "", "null")]


def bookmark_filenames(data: Mapping) -> list[str]:
    out: list[str] = []
    for f in _hub_files(data):
        name = str(f["filename"])
        if not name.lower().endswith(".var"):
            name += ".var"
        if name not in out:
            out.append(name)
    return out


def bookmark_package_names(data: Mapping) -> list[str]:
    deps = data.get("dependencies")
    candidates = [f["filename"] for f in _hub_files(data)] + (list(deps) if isinstance(deps, dict) else [])
    names: list[str] = []
    seen: set[str] = set()
    for name in candidates:
        base = hub_package_base_name(name)
        if base and "." in base and base.lower() not in seen:
            seen.add(base.lower())
            names.append(base)
    return names


def is_hub_downloadable(data: Mapping) -> bool:
    return any(resolve_hub_download_url(f) for f in _hub_files(data))


def external_link(data: Mapping) -> str | None:
    for key in ("promotional_link", "external_url"):
        v = data.get(key)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        try:
            parts = urllib.parse.urlsplit(v)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.hostname:
            return v
    return None


def snapshot_of(row: Mapping) -> dict:
    try:
        data = json.loads(row["hub_json"]) if row.get("hub_json") else {}
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


class PackageMatcher:

    def __init__(
        self,
        package_index: Mapping[str, dict],
        is_placeholder: Callable[[str], bool],
        filenames: Iterable[str] | None = None,
    ):
        self._index = package_index
        self.by_rid: dict[str, list[str]] = {}
        self.by_file: dict[str, str] = {}
        self.by_name: dict[str, list[str]] = {}
        for fn in package_index if filenames is None else filenames:
            pkg = package_index.get(fn)
            if not pkg or is_placeholder(fn):
                continue
            rid = pkg.get("hub_resource_id")
            if rid:
                self.by_rid.setdefault(str(rid), []).append(fn)
            self.by_file[fn.lower()] = fn
            name = (pkg.get("package_name") or "").lower()
            if name:
                self.by_name.setdefault(name, []).append(fn)

    @property
    def empty(self) -> bool:
        return not self.by_file

    def _newest(self, hits: list[str]) -> str:
        return min(hits, key=lambda fn: (-_int((self._index.get(fn) or {}).get("version")), fn))

    def match(self, data: Mapping, resource_id) -> str | None:
        by_rid = self.by_rid.get(str(resource_id), [])
        by_file = [self.by_file[f.lower()] for f in bookmark_filenames(data) if f.lower() in self.by_file]
        by_name = [fn for n in bookmark_package_names(data) for fn in self.by_name.get(n.lower(), ())]
        for hits in (by_rid, by_file, by_name):
            if hits:
                return self._newest(hits)
        return None


def match_packages(
    data: Mapping,
    resource_id,
    filenames: Iterable[str],
    package_index: Mapping[str, dict],
    is_placeholder: Callable[[str], bool],
) -> str | None:
    return PackageMatcher(package_index, is_placeholder, filenames).match(data, resource_id)


def bookmark_summary(row: Mapping, matcher: PackageMatcher) -> dict:
    data = snapshot_of(row)
    rid = str(row["resource_id"])
    names = bookmark_package_names(data)
    installed = matcher.match(data, rid)
    size = sum(_int(f.get("file_size")) for f in _hub_files(data))
    created = _int(row.get("created_at"))
    downloadable = is_hub_downloadable(data)
    title = str(data.get("title") or "").strip() or (names[0] if names else f"Hub resource {rid}")
    return {
        "filename": bookmark_key(rid),
        "is_bookmark": True,
        "resource_id": rid,
        "title": title,
        "creator": str(data.get("username") or "").strip(),
        "hub_user_id": data.get("user_id"),
        "type": str(data.get("type") or "").strip() or None,
        "category": str(data.get("category") or "").strip(),
        "version_string": str(data.get("version_string") or "").strip() or None,
        "tag_line": str(data.get("tag_line") or "").strip(),
        "hub_tags": data.get("tags") or None,
        "license": hub_resource_license(data),
        "external_link": external_link(data),
        "is_downloadable": downloadable,
        "package_names": names,
        "hub_filenames": bookmark_filenames(data),
        "dependency_count": _int(data.get("dependency_count")),
        "last_update": _int(data.get("last_update")),
        "installed_filename": installed,
        "note": row.get("note") or "",
        "created_at": created,
        "snapshot": data,
        "hub_display_name": None,
        "size_bytes": size,
        "dep_count": _int(data.get("dependency_count")),
        "first_seen_at": created,
        "file_mtime": 0,
    }
