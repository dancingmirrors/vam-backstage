from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from .classifier import strip_disabled_suffix
from .content_types import LOOK_ITEM_EXACT_TYPES, category_of, is_gallery_visible, is_visible, tag_of
from .db import Database
from .dummy import ALIASES_SUBDIR
from .graph import (
    DepEdge,
    build_forward_deps,
    build_group_index,
    build_reverse_deps,
    compute_cascade_disable,
    compute_orphan_cascade,
    compute_removable_deps,
    get_transitive_deps,
    parse_dep_ref,
    resolve_ref,
)
from .paths import (
    LOCAL_PACKAGE_DISPLAY_NAME,
    LOCAL_PACKAGE_FILENAME,
    is_local_package,
    is_package_active,
    is_package_archived,
)
from .prefs import PrefsMap, prefs_key


def effective_package_type(pkg: dict | None) -> str | None:
    if not pkg:
        return None
    o = pkg.get("type_override")
    if o:
        return o
    return pkg.get("type")


def is_real_url(v) -> bool:
    return bool(v) and v != "null" and not str(v).endswith("?file=")


def resolve_hub_download_url(hub_file: dict | None) -> str | None:
    if not hub_file:
        return None
    if is_real_url(hub_file.get("downloadUrl")):
        return hub_file["downloadUrl"]
    if is_real_url(hub_file.get("urlHosted")):
        return hub_file["urlHosted"]
    return None


def _try_parse(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _int_version(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _empty_stats() -> dict:
    return {
        "direct_count": 0,
        "dep_count": 0,
        "total_count": 0,
        "enabled_count": 0,
        "broken_count": 0,
        "total_content": 0,
        "total_morph_count": 0,
        "total_size": 0,
        "direct_size": 0,
        "dep_size": 0,
        "content_by_type": {},
        "missing_dep_count": 0,
        "dep_content_count": 0,
        "dep_content_visible": 0,
    }


class Store:
    def __init__(self, db: Database):
        self.db = db
        self.strict_versions: bool = True
        self.package_index: dict[str, dict] = {}
        self.group_index: dict[str, list[str]] = {}
        self.forward_deps: dict[str, list[DepEdge]] = {}
        self.reverse_deps: dict[str, set[str]] = {}
        self.content_items: list[dict] = []
        self.content_items_deduped: list[dict] = []
        self.content_by_package: dict[str, list[dict]] = {}
        self.prefs_map: PrefsMap = {}
        self.removable_size_map: dict[str, int] = {}
        self.morph_count_by_package: dict[str, int] = {}
        self.look_item_count_by_package: dict[str, int] = {}
        self.aggregate_morph_count_map: dict[str, int] = {}
        self.transitive_deps_count_map: dict[str, int] = {}
        self.transitive_missing_map: dict[str, int] = {}
        self.local_file_refs: dict[str, list[str]] = {}
        self._pkg_missing_cache: dict[str, list[str]] = {}
        self.transitive_inactive_map: dict[str, int] = {}
        self.orphan_set: set[str] = set()
        self.superseded_set: set[str] = set()
        self.superseded_by: dict[str, str] = {}
        self.direct_orphan_set: set[str] = set()
        self.label_index: dict[int, dict] = {}
        self.labels_by_package: dict[str, list[int]] = {}
        self.labels_by_content: dict[tuple[str, str], list[int]] = {}
        self.non_downloadable_rids: set[str] = set()
        self.replaceable_set: set[str] = set()
        self.hub_filename_index: dict[str, int] | None = None
        self.stats: dict = _empty_stats()

    def user_package_entries(self) -> Iterator[tuple[str, dict]]:
        for fn, pkg in self.package_index.items():
            if not is_local_package(fn):
                yield fn, pkg

    def user_package_values(self) -> list[dict]:
        return [pkg for _, pkg in self.user_package_entries()]

    def demand_entries(self) -> Iterator[tuple[str, dict]]:
        for fn, pkg in self.package_index.items():
            if not is_package_archived(pkg.get("storage_state")):
                yield fn, pkg

    def set_hub_filename_index(self, index: dict[str, int] | None) -> None:
        self.hub_filename_index = index

    def package_replaceability(self, pkg: dict) -> str:
        if self.hub_filename_index is None:
            return "unknown"
        rid = pkg.get("hub_resource_id")
        if rid and str(rid) in self.non_downloadable_rids:
            return "irreplaceable"
        return "replaceable" if pkg["filename"] in self.hub_filename_index else "irreplaceable"

    def is_not_downloadable(self, pkg: dict) -> bool:
        return self.package_replaceability(pkg) == "irreplaceable"

    def _build_non_downloadable_rids(self) -> None:
        rids: set[str] = set()
        for row in self.db.get_all_hub_resource_jsons():
            detail = _try_parse(row["hub_json"])
            search = _try_parse(row["search_json"])
            find = _try_parse(row["find_json"])
            rid = str(row["resource_id"])
            if detail and detail.get("_unavailable"):
                rids.add(rid)
                continue
            category = (detail or {}).get("category") or (search or {}).get("category")
            if category == "Paid":
                rids.add(rid)
                continue
            dl = (detail or {}).get("hubDownloadable")
            if dl is False or dl == "false":
                rids.add(rid)
                continue
            if find and not is_real_url(find.get("downloadUrl")) and not is_real_url(find.get("urlHosted")):
                rids.add(rid)
        self.non_downloadable_rids = rids

    def _build_replaceable_set(self) -> None:
        self.replaceable_set = {
            fn for fn, pkg in self.user_package_entries() if self.package_replaceability(pkg) == "replaceable"
        }

    def rebuild_replaceable_set(self) -> None:
        self._build_non_downloadable_rids()
        self._build_replaceable_set()

    def build_graph_only(self) -> None:
        self._pkg_missing_cache = {}
        self.package_index = {r["filename"]: r for r in self.db.get_all_packages()}
        self.group_index = build_group_index(self.package_index)
        self.forward_deps = build_forward_deps(self.package_index, self.group_index)
        self.reverse_deps = build_reverse_deps(self.forward_deps)

    def build_from_db(self, skip_graph: bool = False) -> None:
        pkg_rows = self.db.get_all_packages()
        content_rows = self.db.get_all_contents()
        self.package_index = {r["filename"]: r for r in pkg_rows}
        self._pkg_missing_cache = {}
        self.local_file_refs = {}
        for path, meta in self.db.get_local_refs_meta().items():
            refs = _try_parse(meta.get("refs")) if isinstance(meta.get("refs"), str) else meta.get("refs")
            if isinstance(refs, list) and refs:
                self.local_file_refs[path] = [r for r in refs if isinstance(r, str)]
        if not skip_graph:
            self.group_index = build_group_index(self.package_index)
            self.forward_deps = build_forward_deps(self.package_index, self.group_index)
            self.reverse_deps = build_reverse_deps(self.forward_deps)

        all_items: list[dict] = []
        for row in content_rows:
            pkg = self.package_index.get(row["package_filename"])
            prefs = self.prefs_map.get(prefs_key(row["package_filename"], row["internal_path"]))
            item = dict(row)
            item["hidden"] = bool(prefs and prefs.get("hidden"))
            item["favorite"] = bool(prefs and prefs.get("favorite"))
            item["local_disabled"] = is_local_package(row["package_filename"]) and row["internal_path"].endswith(
                ".disabled"
            )
            item["category"] = category_of(row["type"])
            item["tag"] = tag_of(row["type"])
            item["creator"] = (pkg or {}).get("creator") or ""
            item["package_name"] = (pkg or {}).get("package_name") or ""
            item["package_title"] = (pkg or {}).get("title") or ""
            item["package_hub_display_name"] = (pkg or {}).get("hub_display_name")
            item["hub_tags"] = (pkg or {}).get("hub_tags")
            item["is_direct"] = bool((pkg or {}).get("is_direct"))
            item["storage_state"] = (pkg or {}).get("storage_state") or "enabled"
            item["library_dir_id"] = (pkg or {}).get("library_dir_id")
            item["first_seen_at"] = (pkg or {}).get("first_seen_at") or 0
            all_items.append(item)

        managed = [c for c in all_items if c["category"] is not None]
        self.content_items = [c for c in managed if is_gallery_visible(c["type"])]

        self.morph_count_by_package = {}
        self.look_item_count_by_package = {}
        for item in all_items:
            pf = item["package_filename"]
            if item["type"] == "morphBinary":
                self.morph_count_by_package[pf] = self.morph_count_by_package.get(pf, 0) + 1
            if item["type"] in LOOK_ITEM_EXACT_TYPES:
                self.look_item_count_by_package[pf] = self.look_item_count_by_package.get(pf, 0) + 1

        self.content_by_package = {}
        for item in managed:
            self.content_by_package.setdefault(item["package_filename"], []).append(item)

        multi = {name for name, fns in self.group_index.items() if len(fns) > 1}
        if multi:
            best: dict[tuple, dict] = {}
            exclude: set[int] = set()
            for item in self.content_items:
                if item["package_name"] not in multi:
                    continue
                key = (item["package_name"], item["category"], item["display_name"])
                existing = best.get(key)
                if existing is None:
                    best[key] = item
                    continue
                ev = _int_version(self.package_index.get(existing["package_filename"], {}).get("version"))
                iv = _int_version(self.package_index.get(item["package_filename"], {}).get("version"))
                if iv > ev:
                    exclude.add(existing["id"])
                    best[key] = item
                else:
                    exclude.add(item["id"])
            self.content_items_deduped = (
                [c for c in self.content_items if c["id"] not in exclude] if exclude else self.content_items
            )
        else:
            self.content_items_deduped = self.content_items

        self._build_non_downloadable_rids()
        self._build_replaceable_set()
        self._compute_transitive_missing()
        self._compute_transitive_inactive()
        self._compute_stats()
        self._compute_all_removable_sizes()
        self._compute_all_morph_counts()
        self._compute_orphan_sets()
        self.refresh_labels()

    def refresh_labels(self) -> None:
        self.label_index = {
            row["id"]: {
                "id": row["id"],
                "name": row["name"],
                "color": row["color"],
                "package_count": 0,
                "content_count": 0,
            }
            for row in self.db.get_all_labels()
        }
        self.labels_by_package = {}
        for row in self.db.get_all_label_packages():
            entry = self.label_index.get(row["label_id"])
            if not entry:
                continue
            self.labels_by_package.setdefault(row["package_filename"], []).append(row["label_id"])
            entry["package_count"] += 1
        self.labels_by_content = {}
        for row in self.db.get_all_label_contents():
            entry = self.label_index.get(row["label_id"])
            if not entry:
                continue
            key = (row["package_filename"], strip_disabled_suffix(row["internal_path"]))
            self.labels_by_content.setdefault(key, []).append(row["label_id"])
            entry["content_count"] += 1

    def get_label_list(self) -> list[dict]:
        return sorted(self.label_index.values(), key=lambda row: row["name"].casefold())

    def _compute_all_removable_sizes(self) -> None:
        self.removable_size_map = {}
        for fn in self.package_index:
            r = compute_removable_deps(
                fn, self.package_index, self.forward_deps, self.reverse_deps, self.replaceable_set
            )
            if r.removable_size > 0:
                self.removable_size_map[fn] = r.removable_size

    def _compute_all_morph_counts(self) -> None:
        self.aggregate_morph_count_map = {}
        self.transitive_deps_count_map = {}
        for fn in self.package_index:
            transitive = get_transitive_deps(fn, self.forward_deps)
            morphs = self.morph_count_by_package.get(fn, 0) + sum(
                self.morph_count_by_package.get(d, 0) for d in transitive
            )
            if morphs > 0:
                self.aggregate_morph_count_map[fn] = morphs
            total = len(transitive) + self.transitive_missing_map.get(fn, 0)
            if total > 0:
                self.transitive_deps_count_map[fn] = total

    def _compute_orphan_sets(self) -> None:
        r = compute_orphan_cascade(self.package_index, self.forward_deps, self.reverse_deps, self.replaceable_set)
        self.orphan_set = r.orphans
        self.direct_orphan_set = r.direct_orphans
        self._compute_superseded()

    def _compute_superseded(self) -> None:
        self.superseded_set = set()
        self.superseded_by = {}
        for filenames in self.group_index.values():
            live = []
            for fn in filenames:
                pkg = self.package_index.get(fn)
                if not pkg or is_local_package(fn) or is_package_archived(pkg.get("storage_state")):
                    continue
                try:
                    live.append((int(pkg.get("version") or 0), fn))
                except (TypeError, ValueError):
                    continue
            if len(live) < 2:
                continue
            newest_v, newest_fn = max(live)
            if self.package_index[newest_fn].get("storage_state") != "enabled":
                continue
            for v, fn in live:
                if fn == newest_fn or v == newest_v or self.reverse_deps.get(fn):
                    continue
                self.superseded_set.add(fn)
                self.superseded_by[fn] = newest_fn

    def get_superseded_total_size(self) -> int:
        return sum(
            (self.package_index[fn].get("size_bytes") or 0) for fn in self.superseded_set if fn in self.package_index
        )

    def is_missing_edge(self, dep) -> bool:
        return not dep.resolved or (self.strict_versions and dep.resolution == "fallback")

    def _compute_transitive_missing(self) -> None:
        self.transitive_missing_map = {}
        memo: dict[str, set[str]] = {}

        def collect(fn: str) -> set[str]:
            if fn in memo:
                return memo[fn]
            refs: set[str] = set()
            memo[fn] = refs
            for dep in self.forward_deps.get(fn, ()):
                if self.is_missing_edge(dep):
                    refs.add(dep.ref)
                else:
                    refs.update(collect(dep.resolved))
            return refs

        for fn in self.package_index:
            n = len(collect(fn))
            if n:
                self.transitive_missing_map[fn] = n

    def _compute_transitive_inactive(self) -> None:
        self.transitive_inactive_map = {}
        memo: dict[str, set[str]] = {}

        def collect(fn: str) -> set[str]:
            if fn in memo:
                return memo[fn]
            inactive: set[str] = set()
            memo[fn] = inactive
            for dep in self.forward_deps.get(fn, ()):
                if not dep.resolved:
                    continue
                dp = self.package_index.get(dep.resolved)
                if dp and not is_package_active(dp.get("storage_state")):
                    inactive.add(dep.resolved)
                inactive.update(collect(dep.resolved))
            return inactive

        for fn in self.package_index:
            n = len(collect(fn))
            if n:
                self.transitive_inactive_map[fn] = n

    def is_broken_pkg(self, fn: str, pkg: dict) -> bool:
        if is_package_archived(pkg.get("storage_state")):
            return False
        if pkg.get("is_corrupted"):
            return True
        if self.transitive_missing_map.get(fn, 0) > 0:
            return True
        return is_package_active(pkg.get("storage_state")) and self.transitive_inactive_map.get(fn, 0) > 0

    def recompute_inactive_deps(self) -> None:
        self._compute_transitive_inactive()
        self._compute_stats()

    def recompute_demand_aggregates(self) -> None:
        self._compute_orphan_sets()
        self._compute_all_removable_sizes()
        self.recompute_inactive_deps()

    def _compute_stats(self) -> None:
        s = _empty_stats()
        for fn, pkg in self.user_package_entries():
            s["total_count"] += 1
            if is_package_active(pkg.get("storage_state")):
                s["enabled_count"] += 1
            size = pkg.get("size_bytes") or 0
            if pkg.get("is_direct"):
                s["direct_count"] += 1
                s["direct_size"] += size
            else:
                s["dep_count"] += 1
                s["dep_size"] += size
            s["total_size"] += size
            if self.is_broken_pkg(fn, pkg):
                s["broken_count"] += 1
        by_type: dict[str, int] = {}
        for item in self.content_items:
            by_type[item["category"]] = by_type.get(item["category"], 0) + 1
            if not item["is_direct"]:
                s["dep_content_count"] += 1
                if not item["hidden"]:
                    s["dep_content_visible"] += 1
        s["content_by_type"] = by_type
        s["total_content"] = len(self.content_items)
        s["total_morph_count"] = sum(self.morph_count_by_package.values())
        missing = 0
        for fn, _pkg in self.demand_entries():
            for d in self.forward_deps.get(fn, ()):
                if self.is_missing_edge(d):
                    missing += 1
        s["missing_dep_count"] = missing
        self.stats = s

    def set_prefs_map(self, prefs: PrefsMap) -> None:
        self.prefs_map = prefs
        for items in self.content_by_package.values():
            for item in items:
                p = prefs.get(prefs_key(item["package_filename"], item["internal_path"]))
                item["hidden"] = bool(p and p.get("hidden"))
                item["favorite"] = bool(p and p.get("favorite"))

    def update_pref(self, package_filename: str, internal_path: str, field: str, value: bool) -> None:
        canonical = strip_disabled_suffix(internal_path)
        key = package_filename + "/" + canonical
        entry = self.prefs_map.setdefault(key, {"hidden": False, "favorite": False})
        entry[field] = value
        for item in self.content_by_package.get(package_filename, ()):
            if strip_disabled_suffix(item["internal_path"]) == canonical:
                item[field] = value
                break

    def patch_type_override(self, filename: str, type_override: str | None) -> None:
        pkg = self.package_index.get(filename)
        if pkg:
            pkg["type_override"] = type_override or None

    def patch_storage_state(
        self, filenames: Iterable[str], storage_state: str, library_dir_id=..., subpath=...
    ) -> None:
        for fn in filenames:
            pkg = self.package_index.get(fn)
            if not pkg:
                continue
            pkg["storage_state"] = storage_state
            if library_dir_id is not ...:
                pkg["library_dir_id"] = library_dir_id
            if subpath is not ...:
                pkg["subpath"] = subpath or ""
            for item in self.content_by_package.get(fn, ()):
                item["storage_state"] = storage_state

    def patch_direct(self, filename: str, is_direct: bool) -> None:
        pkg = self.package_index.get(filename)
        if pkg:
            pkg["is_direct"] = 1 if is_direct else 0
            for item in self.content_by_package.get(filename, ()):
                item["is_direct"] = is_direct

    def _shadowed_by_sibling(self, pkg: dict) -> dict | None:
        if not pkg.get("is_direct"):
            return None
        try:
            self_ver = int(pkg["version"])
        except (TypeError, ValueError):
            return None
        best = None
        best_ver = self_ver
        for fn in self.group_index.get(pkg["package_name"], ()):
            if fn == pkg["filename"]:
                continue
            sib = self.package_index.get(fn)
            if not sib or not sib.get("is_direct") or sib.get("storage_state") != "enabled":
                continue
            try:
                v = int(sib["version"])
            except (TypeError, ValueError):
                continue
            if v > best_ver:
                best_ver = v
                best = sib
        return best

    def enrich_package_summary(self, pkg: dict) -> dict:
        fn = pkg["filename"]
        contents = self.content_by_package.get(fn, [])
        favorite_count = sum(1 for c in contents if c["favorite"])
        effective_type = effective_package_type(pkg)
        look_items = self.look_item_count_by_package.get(fn, 0)
        shadowed = self._shadowed_by_sibling(pkg)
        return {
            "filename": fn,
            "creator": pkg["creator"],
            "package_name": pkg["package_name"],
            "version": pkg["version"],
            "type": effective_type,
            "derived_type": pkg.get("type"),
            "type_override": pkg.get("type_override"),
            "title": pkg.get("title"),
            "hub_display_name": pkg.get("hub_display_name") or None,
            "description": pkg.get("description"),
            "license": pkg.get("license"),
            "size_bytes": pkg.get("size_bytes") or 0,
            "removable_size": self.removable_size_map.get(fn, 0),
            "is_direct": bool(pkg.get("is_direct")),
            "storage_state": pkg.get("storage_state") or "enabled",
            "library_dir_id": pkg.get("library_dir_id"),
            "subpath": pkg.get("subpath") or "",
            "hub_resource_id": pkg.get("hub_resource_id"),
            "hub_user_id": pkg.get("hub_user_id"),
            "hub_tags": pkg.get("hub_tags") or None,
            "promotional_link": pkg.get("promotional_link") or None,
            "image_url": pkg.get("image_url"),
            "content_count": len(contents),
            "favorite_content_count": favorite_count,
            "dep_count": self.transitive_deps_count_map.get(fn, 0),
            "missing_deps": self.transitive_missing_map.get(fn, 0),
            "inactive_deps": self.transitive_inactive_map.get(fn, 0),
            "morph_count": self.aggregate_morph_count_map.get(fn, 0),
            "first_seen_at": pkg.get("first_seen_at") or 0,
            "file_mtime": pkg.get("file_mtime") or 0,
            "is_corrupted": bool(pkg.get("is_corrupted")),
            "is_dummy": bool(pkg.get("is_dummy")),
            "is_alias": not pkg.get("is_dummy")
            and (pkg.get("subpath") or "").replace("\\", "/").split("/")[0] == ALIASES_SUBDIR,
            "is_unpacked": bool(pkg.get("is_unpacked")),
            "is_orphan": fn in self.orphan_set,
            "is_superseded": fn in self.superseded_set,
            "superseded_by": self.superseded_by.get(fn),
            "is_cascade_orphan": fn in self.orphan_set and fn not in self.direct_orphan_set,
            "is_local_only": self.is_not_downloadable(pkg),
            "is_hub_replaceable": fn in self.replaceable_set,
            "no_look_preset_tag": effective_type == "Looks" and look_items == 0,
            "is_shadowed": shadowed is not None,
            "shadowed_by_filename": shadowed["filename"] if shadowed else None,
            "shadowed_by_version": shadowed["version"] if shadowed else None,
            "label_ids": list(self.labels_by_package.get(fn, ())),
        }

    def get_filtered_packages(self) -> list[dict]:
        return [self.enrich_package_summary(p) for p in self.user_package_values()]

    def build_dep_tree(self, root: str, visited: set[str] | None = None) -> list[dict]:
        visited = visited if visited is not None else set()
        if root in visited:
            return []
        visited.add(root)
        out = []
        for d in self.forward_deps.get(root, ()):
            dep_pkg = self.package_index.get(d.resolved) if d.resolved else None
            out.append(
                {
                    "ref": d.ref,
                    "resolved": d.resolved,
                    "resolution": d.resolution,
                    "filename": d.resolved,
                    "creator": dep_pkg.get("creator") if dep_pkg else None,
                    "package_name": dep_pkg.get("package_name") if dep_pkg else None,
                    "version": dep_pkg.get("version") if dep_pkg else None,
                    "type": effective_package_type(dep_pkg) if dep_pkg else None,
                    "size_bytes": dep_pkg.get("size_bytes") if dep_pkg else None,
                    "is_direct": bool(dep_pkg.get("is_direct")) if dep_pkg else False,
                    "storage_state": (dep_pkg.get("storage_state") if dep_pkg else None) or "enabled",
                    "children": self.build_dep_tree(d.resolved, visited) if d.resolved else [],
                }
            )
        return out

    def _content_row(self, c: dict, **extra) -> dict:
        row = {
            "id": c["id"],
            "package_filename": c["package_filename"],
            "internal_path": c["internal_path"],
            "display_name": c["display_name"],
            "type": c["type"],
            "category": category_of(c["type"]),
            "tag": tag_of(c["type"]),
            "hidden": c["hidden"],
            "favorite": c["favorite"],
            "thumbnail_path": c.get("thumbnail_path"),
            "local_disabled": bool(c.get("local_disabled")),
            "file_mtime": (c.get("file_mtime") or 0) if is_local_package(c["package_filename"]) else 0,
            "creator": c.get("creator") or "",
            "package_name": c.get("package_name") or "",
            "package_title": c.get("package_title") or "",
            "is_direct": bool(c.get("is_direct")),
            "storage_state": c.get("storage_state") or "enabled",
            "own_label_ids": list(
                self.labels_by_content.get((c["package_filename"], strip_disabled_suffix(c["internal_path"])), ())
            ),
        }
        if is_local_package(c["package_filename"]):
            nodes = [
                self.resolve_node(r) for r in self.local_file_refs.get(strip_disabled_suffix(c["internal_path"]), ())
            ]
            row["refs"] = nodes
            row["missing_refs"] = [n["ref"] for n in nodes if n["missing"]]
        else:
            row["refs"] = []
            row["missing_refs"] = self._package_missing_refs(c["package_filename"])
        row.update(extra)
        return row

    def resolve_node(self, ref: str) -> dict:
        res = resolve_ref(ref, self.package_index, self.group_index)
        resolved = res.resolved
        return {
            "ref": ref,
            "resolved": resolved,
            "resolution": res.resolution,
            "storage_state": (self.package_index.get(resolved) or {}).get("storage_state") if resolved else None,
            "missing": not resolved or (self.strict_versions and res.resolution == "fallback"),
        }

    def _package_missing_refs(self, filename: str) -> list[str]:
        cache = getattr(self, "_pkg_missing_cache", None)
        if cache is None:
            cache = self._pkg_missing_cache = {}
        refs = cache.get(filename)
        if refs is None:
            refs = (
                sorted(self.get_transitive_missing_refs(filename, include_fallbacks=self.strict_versions))
                if self.transitive_missing_map.get(filename)
                else []
            )
            cache[filename] = refs
        return refs

    def local_files_missing(self) -> list[dict]:
        out = []
        for path, refs in sorted(self.local_file_refs.items()):
            missing = [r for r in refs if self.resolve_node(r)["missing"]]
            if missing:
                out.append({"path": path, "missing": missing})
        return out

    def hub_reported_type(self, resource_id) -> str | None:
        return self._hub_cached_field(resource_id, "type")

    def hub_version_string(self, resource_id) -> str | None:
        return self._hub_cached_field(resource_id, "version_string")

    def _hub_cached_field(self, resource_id, field: str) -> str | None:
        if not resource_id:
            return None
        row = self.db.get_hub_resource(resource_id)
        if not row:
            return None
        for col in ("hub_json", "search_json"):
            detail = _try_parse(row.get(col)) if row.get(col) else None
            v = detail.get(field) if isinstance(detail, dict) else None
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _local_summary(self, pkg: dict) -> dict:
        fn = pkg["filename"]
        contents = self.content_by_package.get(fn, [])
        return {
            "filename": fn,
            "is_local": True,
            "creator": "Saves, Custom",
            "package_name": LOCAL_PACKAGE_DISPLAY_NAME,
            "version": "",
            "type": "Local",
            "derived_type": None,
            "type_override": None,
            "title": LOCAL_PACKAGE_DISPLAY_NAME,
            "hub_display_name": None,
            "description": None,
            "license": None,
            "size_bytes": 0,
            "removable_size": 0,
            "is_direct": True,
            "storage_state": "enabled",
            "library_dir_id": None,
            "subpath": "",
            "hub_resource_id": None,
            "hub_user_id": None,
            "hub_tags": None,
            "promotional_link": None,
            "image_url": None,
            "content_count": len(contents),
            "favorite_content_count": sum(1 for c in contents if c["favorite"]),
            "dep_count": self.transitive_deps_count_map.get(fn, 0),
            "missing_deps": self.transitive_missing_map.get(fn, 0),
            "inactive_deps": self.transitive_inactive_map.get(fn, 0),
            "morph_count": 0,
            "first_seen_at": 0,
            "file_mtime": 0,
            "is_corrupted": False,
            "is_dummy": False,
            "is_alias": False,
            "is_unpacked": False,
            "is_orphan": False,
            "is_cascade_orphan": False,
            "is_superseded": False,
            "superseded_by": None,
            "is_local_only": True,
            "is_hub_replaceable": False,
            "no_look_preset_tag": False,
            "is_shadowed": False,
            "shadowed_by_filename": None,
            "shadowed_by_version": None,
            "label_ids": [],
        }

    def is_placeholder(self, filename: str) -> bool:
        pkg = self.package_index.get(filename)
        if not pkg:
            return False
        return (
            bool(pkg.get("is_dummy")) or (pkg.get("subpath") or "").replace("\\", "/").split("/")[0] == ALIASES_SUBDIR
        )

    def placeholder_counts(self) -> dict:
        dummies = aliases = 0
        for pkg in self.user_package_values():
            if pkg.get("is_dummy"):
                dummies += 1
            elif (pkg.get("subpath") or "").replace("\\", "/").split("/")[0] == ALIASES_SUBDIR:
                aliases += 1
        return {"dummies": dummies, "aliases": aliases}

    def local_summary(self) -> dict | None:
        pkg = self.package_index.get(LOCAL_PACKAGE_FILENAME)
        return self._local_summary(pkg) if pkg else None

    def local_demand_summary(self) -> dict | None:
        pkg = self.package_index.get(LOCAL_PACKAGE_FILENAME)
        if not pkg or not self.transitive_missing_map.get(LOCAL_PACKAGE_FILENAME):
            return None
        return self._local_summary(pkg)

    def get_package_detail(self, filename: str) -> dict | None:
        pkg = self.package_index.get(filename)
        if not pkg:
            return None
        if is_local_package(filename):
            detail = self._local_summary(pkg)
            detail.update(
                {
                    "hub_type": None,
                    "deps": self.build_dep_tree(filename),
                    "deps_total": self.transitive_deps_count_map.get(filename, 0),
                    "missing_deps_total": self.transitive_missing_map.get(filename, 0),
                    "dependents": [],
                    "contents": [],
                    "removable_size": 0,
                    "removable_deps_count": 0,
                    "removable_deps": [],
                    "cascade_disable_deps": [],
                    "files_missing": self.local_files_missing(),
                }
            )
            return detail
        deps = self.build_dep_tree(filename)
        dependents = []
        for fn in sorted(self.reverse_deps.get(filename, ())):
            dp = self.package_index.get(fn)
            dependents.append(
                {
                    "filename": fn,
                    "display": LOCAL_PACKAGE_DISPLAY_NAME + " (Saves, Custom)" if is_local_package(fn) else None,
                    "creator": dp.get("creator") if dp else None,
                    "package_name": dp.get("package_name") if dp else None,
                    "version": dp.get("version") if dp else None,
                    "storage_state": (dp.get("storage_state") if dp else None) or "enabled",
                    "is_direct": bool(dp.get("is_direct")) if dp else False,
                }
            )
        contents = [self._content_row(c) for c in self.content_by_package.get(filename, ()) if is_visible(c["type"])]
        removable = compute_removable_deps(
            filename, self.package_index, self.forward_deps, self.reverse_deps, self.replaceable_set
        )
        removable_deps = []
        for f in sorted(removable.removable_filenames):
            p = self.package_index.get(f)
            removable_deps.append(
                {
                    "filename": f,
                    "name": (p.get("package_name") or f).split(".")[-1] if p else f,
                    "size_bytes": (p.get("size_bytes") if p else 0) or 0,
                    "is_local_only": self.is_not_downloadable(p) if p else False,
                }
            )
        cascade = []
        if pkg.get("storage_state") == "enabled":
            for f in sorted(
                compute_cascade_disable(filename, self.package_index, self.forward_deps, self.reverse_deps)
            ):
                p = self.package_index.get(f)
                cascade.append({"filename": f, "name": (p.get("package_name") or f).split(".")[-1] if p else f})
        detail = self.enrich_package_summary(pkg)
        detail.update(
            {
                "hub_type": self.hub_reported_type(pkg.get("hub_resource_id")),
                "hub_version_string": self.hub_version_string(pkg.get("hub_resource_id")),
                "deps": deps,
                "deps_total": self.transitive_deps_count_map.get(filename, 0),
                "missing_deps_total": self.transitive_missing_map.get(filename, 0),
                "dependents": dependents,
                "contents": contents,
                "removable_size": removable.removable_size,
                "removable_deps_count": len(removable.removable_filenames),
                "removable_deps": removable_deps,
                "cascade_disable_deps": cascade,
            }
        )
        return detail

    def get_filtered_contents(self, package_filename: str | None = None) -> list[dict]:
        if package_filename:
            return [self._content_row(c) for c in self.content_items if c["package_filename"] == package_filename]
        return [self._content_row(c) for c in self.content_items_deduped]

    def get_status_counts(self) -> dict:
        direct = dependency = broken = local = offloaded = archived = disabled = 0
        for fn, pkg in self.user_package_entries():
            if is_package_archived(pkg.get("storage_state")):
                archived += 1
                continue
            if pkg.get("is_direct"):
                direct += 1
            else:
                dependency += 1
            if self.is_broken_pkg(fn, pkg):
                broken += 1
            if self.is_not_downloadable(pkg):
                local += 1
            if pkg.get("storage_state") == "offloaded":
                offloaded += 1
            if pkg.get("storage_state") == "disabled":
                disabled += 1
        missing_groups: set[str] = set()
        for fn, _pkg in self.demand_entries():
            for d in self.forward_deps.get(fn, ()):
                if not self.is_missing_edge(d):
                    continue
                missing_groups.add(d.ref)
        return {
            "direct": direct,
            "dependency": dependency,
            "broken": broken,
            "orphan": len(self.orphan_set),
            "superseded": len(self.superseded_set),
            "local": local,
            "offloaded": offloaded,
            "disabled": disabled,
            "archived": archived,
            "missing_unique": len(missing_groups),
        }

    def get_orphan_total_size(self) -> int:
        return sum(
            (self.package_index[fn].get("size_bytes") or 0) for fn in self.orphan_set if fn in self.package_index
        )

    def get_transitive_missing_refs(self, filename: str, include_fallbacks: bool = False) -> set[str]:
        visited: set[str] = set()
        refs: set[str] = set()
        stack = [filename]
        while stack:
            cur = stack.pop()
            for dep in self.forward_deps.get(cur, ()):
                if not dep.resolved:
                    refs.add(dep.ref)
                    continue
                if include_fallbacks and dep.resolution == "fallback":
                    refs.add(dep.ref)
                if dep.resolved not in visited:
                    visited.add(dep.resolved)
                    stack.append(dep.resolved)
        return refs

    def near_matches(self, parsed) -> list[str]:
        if parsed is None or "." not in parsed.package_name:
            return []
        want = parsed.package_name.split(".", 1)[1].lower()
        creator = parsed.creator.lower()
        found = []
        for fn, pkg in self.package_index.items():
            if is_local_package(fn) or pkg.get("is_dummy"):
                continue
            name = pkg.get("package_name") or ""
            if (
                "." not in name
                or name.split(".", 1)[1].lower() != want
                or (pkg.get("creator") or "").lower() == creator
            ):
                continue
            try:
                v = int(pkg.get("version") or 0)
            except (TypeError, ValueError):
                v = 0
            found.append((-v, fn))
        return [fn for _, fn in sorted(found)]

    def get_missing_deps(
        self, hub_packages_index: dict | None = None, hub_filename_index: dict | None = None
    ) -> list[dict]:
        groups: dict[str, dict] = {}
        for fn, _pkg in self.demand_entries():
            for d in self.forward_deps.get(fn, ()):
                if not self.is_missing_edge(d):
                    continue
                g = groups.get(d.ref)
                if g is None:
                    g = {"needed_by": [], "parsed": parse_dep_ref(d.ref), "fallback_version": None}
                    groups[d.ref] = g
                if fn not in g["needed_by"]:
                    g["needed_by"].append(fn)
                if d.resolution == "fallback" and d.resolved and not g["fallback_version"]:
                    fb = self.package_index.get(d.resolved)
                    if fb:
                        g["fallback_version"] = fb.get("version")
        rows = []
        for ref, g in groups.items():
            parsed = g["parsed"]
            local_files = (
                [p for p, refs in sorted(self.local_file_refs.items()) if any(r.lower() == ref.lower() for r in refs)]
                if LOCAL_PACKAGE_FILENAME in g["needed_by"]
                else []
            )
            row = {
                "ref": ref,
                "local_files": local_files,
                "package_name": parsed.package_name if parsed else ref,
                "creator": parsed.creator if parsed else (ref.split(".")[0] if ref else ""),
                "version": parsed.version if parsed else None,
                "min_version": parsed.min_version if parsed else None,
                "display_name": parsed.package_name.split(".")[-1] if parsed else ref,
                "needed_by": [
                    {
                        "filename": fn,
                        "name": (
                            LOCAL_PACKAGE_DISPLAY_NAME
                            if is_local_package(fn)
                            else ((self.package_index.get(fn) or {}).get("package_name") or fn).split(".")[-1]
                        ),
                    }
                    for fn in g["needed_by"]
                ],
                "is_fallback": bool(g["fallback_version"]),
                "fallback_version": g["fallback_version"],
                "near_matches": self.near_matches(parsed) if not g["fallback_version"] else [],
                "hub": None,
            }
            rows.append(row)
            if not hub_packages_index or not hub_filename_index:
                continue
            exact = ref + ".var"
            rid = hub_filename_index.get(exact)
            if rid is not None:
                row["hub"] = {"filename": exact, "resource_id": str(rid), "is_exact": True, "download_url": None}
                continue
            if not parsed:
                continue
            entry = hub_packages_index.get(parsed.package_name)
            if not entry:
                continue
            row["hub"] = {
                "filename": entry["filename"],
                "resource_id": str(entry["resource_id"]),
                "is_exact": False,
                "installed_locally": entry["filename"] in self.package_index,
                "hub_version": entry["version"],
                "download_url": None,
            }
        return rows

    def get_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pkg in self.user_package_values():
            t = effective_package_type(pkg)
            if t:
                counts[t] = counts.get(t, 0) + 1
        return counts

    def get_content_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.content_items:
            counts[c["category"]] = counts.get(c["category"], 0) + 1
        return counts

    def get_content_visibility_counts(self) -> dict[str, int]:
        hidden = sum(1 for c in self.content_items if c["hidden"])
        favorites = sum(1 for c in self.content_items if c["favorite"])
        total = len(self.content_items)
        return {"all": total, "visible": total - hidden, "hidden": hidden, "favorites": favorites}

    def find_local_by_hub_resource_id(self, resource_id) -> dict | None:
        rid = str(resource_id)
        for pkg in self.user_package_values():
            if pkg.get("hub_resource_id") == rid:
                return pkg
        return None

    def find_local_by_filename(self, filename: str) -> dict | None:
        if is_local_package(filename):
            return None
        return self.package_index.get(filename)

    def hub_install_snapshot(self, resource_ids: Iterable | None = None) -> dict[str, dict]:
        by_rid: dict[str, dict] = {}
        for pkg in self.user_package_values():
            rid = pkg.get("hub_resource_id")
            if rid and str(rid) not in by_rid:
                by_rid[str(rid)] = pkg
        wanted = None if resource_ids is None else {str(r) for r in resource_ids}
        out = {}
        for rid, pkg in by_rid.items():
            if wanted is not None and rid not in wanted:
                continue
            out[rid] = {
                "storage_state": pkg.get("storage_state") or "enabled",
                "is_direct": bool(pkg.get("is_direct")),
                "filename": pkg["filename"],
                "is_placeholder": self.is_placeholder(pkg["filename"]),
            }
        return out
