from __future__ import annotations

import json
import logging
import os
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .classifier import ContentItem, FileEntry, classify_contents, derive_package_type
from .db import Database
from .dummy import ALIASES_SUBDIR, DUMMIES_SUBDIR, DUMMY_META_KEY
from .graph import detect_leaves, extract_dep_refs, extract_refs_from_text
from .lenient_json import loads_lenient
from .library_dirs import LibraryDirs, classify_main_var_on_disk, library_rel_subpath
from .paths import ADDON_PACKAGES, LOCAL_CONTENT_DIRS, LOCAL_PACKAGE_FILENAME, is_local_package
from .prefs import copy_sidecars_to_new_version, hide_package_content, read_all_prefs, unhide_package_content
from .store import effective_package_type
from .varfile import (
    IntegrityError,
    canonical_var_filename,
    is_unpacked_package,
    is_var_filename,
    package_signature,
    parse_var_filename,
    read_var,
    verify_package_full,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[dict], None]
_STAT_WORKERS = 8

PERSON_ATOM_ID_CONTENT_TYPES = frozenset({"scene", "legacyScene", "legacyLook"})


def _noop(_: dict) -> None:
    pass


@dataclass
class VarCandidate:
    canonical: str
    bare_path: str | None
    disabled_path: str | None


@dataclass
class DiscoveredVar:
    filename: str
    full_path: str
    mtime: float
    size: int
    storage_state: str
    library_dir_id: int | None
    subpath: str


def collect_var_candidates(dir_path: str, out: list[VarCandidate], is_root: bool) -> bool:
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return not is_root
    local: dict[str, list[str | None]] = {}
    for e in entries:
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                if is_unpacked_package(e.path):
                    local.setdefault(e.name, [None, None])[0] = e.path
                else:
                    collect_var_candidates(e.path, out, False)
                continue
            if not e.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if not is_var_filename(e.name):
            continue
        is_disabled = e.name.lower().endswith(".disabled")
        canonical = canonical_var_filename(e.name) if is_disabled else e.name
        group = local.setdefault(canonical, [None, None])
        group[1 if is_disabled else 0] = e.path
    for canonical, (bare, disabled) in local.items():
        out.append(VarCandidate(canonical, bare, disabled))
    return True


def normalize_aux_disabled(disabled_path: str) -> str | None:
    d = os.path.dirname(disabled_path)
    bare = os.path.join(d, canonical_var_filename(os.path.basename(disabled_path)))
    try:
        disabled_stat = os.stat(disabled_path)
    except OSError:
        return None
    try:
        bare_stat = os.stat(bare)
    except OSError:
        bare_stat = None
    if bare_stat is not None:
        if disabled_stat.st_size == 0 or disabled_stat.st_size == bare_stat.st_size:
            try:
                os.unlink(disabled_path)
            except OSError:
                pass
        else:
            log.warning("Refusing to remove %s: bare sibling exists with different size", disabled_path)
        return None
    if disabled_stat.st_size == 0:
        try:
            os.unlink(disabled_path)
        except OSError:
            pass
        return None
    try:
        os.replace(disabled_path, bare)
        return bare
    except OSError as err:
        log.warning("Could not normalize %s -> %s: %s", disabled_path, bare, err)
        return None


def walk_for_vars(dir_path: str, library_dir_id: int | None, dirs: LibraryDirs) -> tuple[list[DiscoveredVar], bool]:
    t0 = time.monotonic()
    candidates: list[VarCandidate] = []
    if not collect_var_candidates(dir_path, candidates, True):
        return [], False
    is_archive = dirs.is_archive_library_dir(library_dir_id)

    def process(c: VarCandidate) -> DiscoveredVar | None:
        if library_dir_id is not None:
            content_path = c.bare_path
            if c.disabled_path:
                bare = normalize_aux_disabled(c.disabled_path)
                if not content_path:
                    content_path = bare
            if not content_path:
                return None
            try:
                size, mtime = package_signature(content_path)
            except OSError:
                return None
            return DiscoveredVar(
                c.canonical,
                content_path,
                mtime,
                size,
                "archived" if is_archive else "offloaded",
                library_dir_id,
                library_rel_subpath(dir_path, content_path),
            )
        bare = c.bare_path or os.path.join(os.path.dirname(c.disabled_path or ""), c.canonical)
        cls = classify_main_var_on_disk(bare, disabled_known_absent=not c.disabled_path)
        if not cls.present or cls.content_stat is None or cls.content_path is None:
            return None
        try:
            size, mtime = package_signature(cls.content_path)
        except OSError:
            return None
        return DiscoveredVar(
            c.canonical,
            cls.content_path,
            mtime,
            size,
            cls.storage_state or "enabled",
            None,
            library_rel_subpath(dir_path, cls.content_path),
        )

    with ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
        results = [r for r in pool.map(process, candidates) if r is not None]
    log.info(
        "Library scan: indexed %d .var files in %d ms (dir=%s)",
        len(results),
        int((time.monotonic() - t0) * 1000),
        dir_path,
    )
    return results, True


def person_atom_ids_json(buf: bytes | None, package_filename: str | None = None) -> str | None:
    if not buf:
        return None
    try:
        text = buf.decode("utf-8", errors="replace")
        if package_filename:
            stem = package_filename[:-4] if package_filename.lower().endswith(".var") else package_filename
            text = text.replace("SELF:/", stem + ":/")
        data = loads_lenient(text)
        atoms = data.get("atoms") if isinstance(data, dict) else None
        ids = (
            [a.get("id") for a in atoms if isinstance(a, dict) and a.get("type") == "Person"]
            if isinstance(atoms, list)
            else []
        )
        import json

        return json.dumps([i for i in ids if isinstance(i, str)])
    except Exception:
        return None


@dataclass
class ScanResult:
    filename: str
    content_items: list[ContentItem]
    meta: dict | None
    size: int
    pkg_type: str | None
    package_name: str


def scan_and_upsert(
    db: Database,
    full_path: str,
    *,
    storage_state: str = "enabled",
    library_dir_id: int | None = None,
    subpath: str = "",
    is_direct: bool = False,
    type_override: str | None = None,
    first_seen_at: int | None = None,
    extract_scene_jsons: bool = False,
) -> ScanResult | None:
    filename = canonical_var_filename(os.path.basename(full_path))
    parsed = parse_var_filename(filename)
    if parsed is None:
        return None
    size, mtime = package_signature(full_path)
    unpacked = os.path.isdir(full_path)
    info = read_var(full_path, extract_scene_jsons=extract_scene_jsons)
    content_items = info.content_items if info.content_items is not None else classify_contents(info.file_list)
    meta = info.meta
    dep_refs = extract_dep_refs(meta, filename) if meta else []
    pkg_type = type_override or derive_package_type(content_items)
    title = str(meta.get("title")).strip() if meta and meta.get("title") else parsed.package_name.split(".")[-1]
    description = str(meta.get("description")).strip() if meta and meta.get("description") else None
    license_ = str(meta.get("licenseType")).strip() if meta and meta.get("licenseType") else None

    import json

    row = {
        "filename": filename,
        "creator": parsed.creator,
        "package_name": parsed.package_name,
        "version": parsed.version,
        "type": pkg_type,
        "title": title,
        "description": description or None,
        "license": license_ or None,
        "size_bytes": size,
        "file_mtime": mtime,
        "is_unpacked": 1 if unpacked else 0,
        "is_direct": 1 if is_direct else 0,
        "storage_state": storage_state,
        "library_dir_id": library_dir_id,
        "subpath": subpath or "",
        "dep_refs": json.dumps(dep_refs),
        "is_dummy": 1 if meta and meta.get(DUMMY_META_KEY) else 0,
    }
    if first_seen_at is not None:
        row["first_seen_at"] = first_seen_at
    with db.transaction():
        db.upsert_package(row)
        db.delete_contents_for_package(filename)
        db.insert_contents(
            {
                "package_filename": filename,
                "internal_path": c.internal_path,
                "display_name": c.display_name,
                "type": c.type,
                "thumbnail_path": c.thumbnail_path,
                "person_atom_ids": (
                    person_atom_ids_json(info.extracts.get(c.internal_path), filename)
                    if c.type in PERSON_ATOM_ID_CONTENT_TYPES and info.extracts
                    else None
                ),
            }
            for c in content_items
        )
    return ScanResult(filename, content_items, meta, size, pkg_type, parsed.package_name)


def inherit_from_older_version(
    lib, filename: str, package_name: str, content_items: Iterable[ContentItem]
) -> dict | None:
    if not package_name or not filename:
        return None
    donors = lib.db.get_donor_versions_by_package_name(package_name, filename)
    if not donors:
        return None
    donor = donors[0]
    copied_override = False
    if donor.get("type_override"):
        lib.db.set_package_type_override(filename, donor["type_override"])
        copied_override = True
    lib.db.copy_package_labels(donor["filename"], filename)
    paths = [c.internal_path for c in content_items if c.internal_path]
    lib.db.copy_content_labels_for_paths(donor["filename"], filename, paths)
    if lib.vam_dir:
        try:
            copy_sidecars_to_new_version(lib.vam_dir, donor["filename"], filename, paths)
        except OSError as err:
            log.warning("sidecar copy %s -> %s failed: %s", donor["filename"], filename, err)
    return {"donor": donor["filename"], "copied_type_override": copied_override}


@dataclass
class ScanSummary:
    scanned: int = 0
    added: int = 0
    removed: int = 0
    unreadable: list[str] = field(default_factory=list)
    added_filenames: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)


def is_placeholder_location(subpath: str | None) -> bool:
    return (subpath or "").replace("\\", "/").split("/")[0] in (DUMMIES_SUBDIR, ALIASES_SUBDIR)


def has_dummy_marker(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return bool(json.loads(zf.read("meta.json").decode("utf-8", "replace")).get(DUMMY_META_KEY))
    except Exception:
        return False


def remove_superseded_placeholder(v: DiscoveredVar) -> bool:
    top = (v.subpath or "").replace("\\", "/").split("/")[0]
    path = v.full_path
    try:
        if top == DUMMIES_SUBDIR:
            if not has_dummy_marker(path):
                return False
        elif top == ALIASES_SUBDIR:
            if os.path.isdir(path) or os.stat(path).st_nlink < 2:
                log.warning(
                    "%s: the real package arrived, but the copy in %s/ is not a hard link; delete it by hand",
                    v.filename,
                    ALIASES_SUBDIR,
                )
                return False
        else:
            return False
        os.unlink(path)
        for sibling in (path + ".disabled",):
            try:
                os.unlink(sibling)
            except FileNotFoundError:
                pass
    except OSError as err:
        log.warning("could not remove superseded placeholder %s: %s", path, err)
        return False
    log.info("placeholder %s removed: the real package is in %s", path, v.filename)
    return True


def _discovery_still_true(v: DiscoveredVar) -> bool:
    if not os.path.exists(v.full_path):
        return False
    if v.library_dir_id is not None:
        return True
    bare = os.path.join(os.path.dirname(v.full_path), v.filename)
    cls = classify_main_var_on_disk(bare)
    return cls.present and cls.storage_state == v.storage_state


def run_scan(lib, on_progress: ProgressFn | None = None) -> ScanSummary:
    progress = on_progress or _noop
    vam_dir = lib.vam_dir
    if not vam_dir:
        raise RuntimeError("VaM directory not configured")
    lib.dirs.refresh()
    dirs = lib.dirs.all_library_dirs()
    is_initial = not lib.db.get_setting("initial_scan_done")

    progress({"phase": "indexing", "step": 0, "total": 0, "message": "Indexing .var files…"})
    var_files: list[DiscoveredVar] = []
    chosen: dict[str, int] = {}
    replaced: list[str] = []
    reachable: set[int | None] = set()
    for d in dirs:
        results, ok = walk_for_vars(d["path"], d["id"], lib.dirs)
        if not ok:
            log.warning("Library directory unreachable, skipping prune for it: %s", d["path"])
            continue
        reachable.add(d["id"])
        for r in results:
            idx = chosen.get(r.filename)
            if idx is None:
                chosen[r.filename] = len(var_files)
                var_files.append(r)
                continue
            kept = var_files[idx]
            if is_placeholder_location(kept.subpath) and not is_placeholder_location(r.subpath):
                if remove_superseded_placeholder(kept):
                    replaced.append(r.filename)
                var_files[idx] = r
            elif is_placeholder_location(r.subpath) and not is_placeholder_location(kept.subpath):
                if remove_superseded_placeholder(r):
                    replaced.append(r.filename)
    offline = len(dirs) - len(reachable)
    progress(
        {
            "phase": "indexing",
            "step": len(var_files),
            "total": len(var_files),
            "message": f"Found {len(var_files)} .var files" + (f" ({offline} dir(s) offline)" if offline else ""),
        }
    )

    summary = ScanSummary()
    summary.replaced = replaced
    new_additions: dict[str, ScanResult] = {}
    scan_started_at = int(time.time())
    db = lib.db
    for i, v in enumerate(var_files):
        progress({"phase": "reading", "step": i + 1, "total": len(var_files), "message": v.filename})
        cached = db.get_package_reconcile_info(v.filename)
        if cached and cached["file_mtime"] == v.mtime and cached["size_bytes"] == v.size:
            if (
                cached["storage_state"] != v.storage_state
                or cached["library_dir_id"] != v.library_dir_id
                or (cached["subpath"] or "") != v.subpath
                or cached["missing_since"] is not None
            ):
                with lib.lock:
                    if _discovery_still_true(v):
                        db.set_storage_state(v.filename, v.storage_state, v.library_dir_id, v.subpath)
            continue
        try:
            result = scan_and_upsert(
                db,
                v.full_path,
                storage_state=v.storage_state,
                library_dir_id=v.library_dir_id,
                subpath=v.subpath,
                is_direct=False,
                first_seen_at=scan_started_at,
            )
        except Exception as err:
            log.warning("Failed to scan %s: %s", v.filename, err)
            summary.unreadable.append(v.filename)
            continue
        if result is None:
            continue
        summary.scanned += 1
        if not cached:
            summary.added += 1
            new_additions[v.filename] = result

    inherited: set[str] = set()
    if not is_initial and new_additions:
        for fn, info in new_additions.items():
            try:
                if inherit_from_older_version(lib, fn, info.package_name, info.content_items):
                    inherited.add(fn)
            except Exception as err:
                log.warning("Inherit from older version failed for %s: %s", fn, err)

    progress({"phase": "graph", "step": 0, "total": 1, "message": "Detecting removed packages…"})
    disk_filenames = {v.filename for v in var_files}
    removed = [
        r["filename"]
        for r in db.get_all_db_filenames_with_dir()
        if not is_local_package(r["filename"])
        and r["library_dir_id"] in reachable
        and r["filename"] not in disk_filenames
    ]
    if removed:
        db.mark_packages_missing(removed)
    summary.removed = len(removed)

    with lib.lock:
        store = lib.store
        if (is_initial or new_additions or removed) and var_files:
            store.build_graph_only()
            if is_initial:
                leaves = detect_leaves(store.package_index, store.reverse_deps)
                db.batch_set_direct([(fn, fn in leaves) for fn in store.package_index])
            else:
                updates = [(fn, True) for fn in new_additions]
                if updates:
                    db.batch_set_direct(updates)
                    store.build_graph_only()
                    for fn, info in new_additions.items():
                        if fn in inherited:
                            continue
                        try:
                            sync_auto_hide_after_direct_change(lib, fn, True, info.content_items)
                        except Exception as err:
                            log.warning("auto-hide sync failed for %s: %s", fn, err)
        progress({"phase": "graph", "step": 1, "total": 1, "message": "Dependency graph built"})

    progress({"phase": "local", "step": 0, "total": 1, "message": "Indexing loose Saves/Custom…"})
    try:
        run_local_scan(lib)
    except Exception as err:
        log.warning("Local content scan failed: %s", err)
    progress({"phase": "local", "step": 1, "total": 1, "message": "Loose content indexed"})

    progress({"phase": "finalizing", "step": 0, "total": 1, "message": "Loading preferences…"})
    prefs = read_all_prefs(vam_dir)
    with lib.lock:
        lib.store.set_prefs_map(prefs)
        progress({"phase": "finalizing", "step": 0, "total": 1, "message": "Building indexes…"})
        lib.store.build_from_db()
        if is_initial:
            db.set_setting("initial_scan_done", "1")
    summary.added_filenames = list(new_additions)
    progress({"phase": "finalizing", "step": 1, "total": 1, "message": "Done"})
    return summary


def _collect_local_files(dir_path: str, out: list[str]) -> bool:
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return False
    for e in entries:
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                _collect_local_files(e.path, out)
            elif e.is_file(follow_symlinks=False):
                out.append(e.path)
        except OSError:
            continue
    return True


def run_local_scan(lib) -> dict:
    vam_dir = lib.vam_dir
    if not vam_dir:
        return {"added": 0, "removed": 0, "total": 0}
    db = lib.db
    db.ensure_local_package()
    paths: list[str] = []
    any_root = False
    for d in LOCAL_CONTENT_DIRS:
        if _collect_local_files(os.path.join(vam_dir, d), paths):
            any_root = True
    if not any_root:
        return {"added": 0, "removed": 0, "total": 0}

    def stat_one(full: str):
        try:
            s = os.stat(full)
        except OSError:
            return None
        rel = os.path.relpath(full, vam_dir).replace(os.sep, "/")
        return rel, s.st_size, s.st_mtime

    with ThreadPoolExecutor(max_workers=_STAT_WORKERS) as pool:
        records = [r for r in pool.map(stat_one, paths) if r is not None]
    stat_by_path = {rel: (mtime, size) for rel, size, mtime in records}
    items = classify_contents([FileEntry(rel, size) for rel, size, _ in records])
    previous = db.get_local_content_meta(LOCAL_PACKAGE_FILENAME)

    rows = []
    added = 0
    for item in items:
        mtime, size = stat_by_path[item.internal_path]
        prev = previous.get(item.internal_path)
        stat_moved = not prev or prev["mtime"] != mtime or prev["size"] != size
        class_moved = (
            not prev
            or prev["display_name"] != item.display_name
            or prev["type"] != item.type
            or prev["thumbnail_path"] != item.thumbnail_path
        )
        if not stat_moved and not class_moved:
            continue
        if not prev:
            added += 1
        person_atom_ids = prev["person_atom_ids"] if prev else None
        if stat_moved and item.type in PERSON_ATOM_ID_CONTENT_TYPES:
            try:
                with open(os.path.join(vam_dir, item.internal_path), "rb") as f:
                    person_atom_ids = person_atom_ids_json(f.read(), None)
            except OSError:
                person_atom_ids = None
        rows.append(
            {
                "package_filename": LOCAL_PACKAGE_FILENAME,
                "internal_path": item.internal_path,
                "display_name": item.display_name,
                "type": item.type,
                "thumbnail_path": item.thumbnail_path,
                "person_atom_ids": person_atom_ids,
                "file_mtime": mtime,
                "size_bytes": size,
            }
        )
    db.upsert_contents(rows)
    want = {i.internal_path for i in items}
    to_delete = [p for p in previous if p not in want]
    db.delete_contents_for_package_paths(LOCAL_PACKAGE_FILENAME, to_delete)
    refs_changed = _index_local_refs(db, vam_dir, records)
    return {"added": added, "removed": len(to_delete), "total": len(items), "refs_changed": refs_changed}


_REF_FILE_SUFFIXES = (".json", ".vap", ".vaj", ".vam")


def _index_local_refs(db: Database, vam_dir: str, records) -> bool:
    previous = db.get_local_refs_meta()
    current = {rel: (mtime, size) for rel, size, mtime in records if rel.lower().endswith(_REF_FILE_SUFFIXES)}
    rows = []
    union: set[str] = set()
    for rel, (mtime, size) in current.items():
        prev = previous.get(rel)
        if prev and prev["file_mtime"] == mtime and prev["size_bytes"] == size:
            union.update(json.loads(prev["refs"]))
            continue
        try:
            with open(os.path.join(vam_dir, rel), "rb") as f:
                refs = extract_refs_from_text(f.read())
        except OSError:
            continue
        rows.append({"internal_path": rel, "file_mtime": mtime, "size_bytes": size, "refs": json.dumps(refs)})
        union.update(refs)
    gone = [rel for rel in previous if rel not in current]
    if rows:
        db.upsert_local_refs(rows)
    if gone:
        db.delete_local_refs(gone)
    return db.set_package_dep_refs(LOCAL_PACKAGE_FILENAME, json.dumps(sorted(union)))


@dataclass(frozen=True)
class AutoHideRule:
    id: str
    setting_key: str
    matches: Callable[[dict, dict], bool]


def _is(c: dict, *types: str) -> bool:
    return c.get("type") in types


AUTO_HIDE_RULES: tuple[AutoHideRule, ...] = (
    AutoHideRule("deps", "auto_hide_deps", lambda ctx, c: not ctx["is_direct"]),
    AutoHideRule(
        "foreign_hair",
        "auto_hide_foreign_hair",
        lambda ctx, c: ctx["effective_type"] != "Hairstyles" and _is(c, "hairItem", "hairPreset"),
    ),
    AutoHideRule(
        "foreign_poses",
        "auto_hide_foreign_poses",
        lambda ctx, c: ctx["effective_type"] != "Poses" and _is(c, "pose", "legacyPose"),
    ),
    AutoHideRule(
        "foreign_clothing",
        "auto_hide_foreign_clothing",
        lambda ctx, c: ctx["effective_type"] != "Clothing" and _is(c, "clothingItem", "clothingPreset"),
    ),
)


def find_rule(rule_id: str) -> AutoHideRule:
    for r in AUTO_HIDE_RULES:
        if r.id == rule_id:
            return r
    raise ValueError(f"Unknown auto-hide rule: {rule_id}")


def _rule_enabled(db: Database, rule: AutoHideRule) -> bool:
    return db.get_setting(rule.setting_key) == "1"


def _ctx(filename: str, pkg: dict) -> dict:
    return {
        "filename": filename,
        "is_direct": bool(pkg.get("is_direct")),
        "effective_type": effective_package_type(pkg),
    }


def _claimed_by_any_except(db: Database, ctx: dict, content: dict, exclude: str) -> bool:
    return any(r.id != exclude and _rule_enabled(db, r) and r.matches(ctx, content) for r in AUTO_HIDE_RULES)


def compute_auto_hide_paths_for_new_package(
    db: Database, filename: str, effective_type: str | None, is_direct: bool, content_items: Iterable
) -> list[str]:
    if is_local_package(filename):
        return []
    ctx = {"filename": filename, "is_direct": bool(is_direct), "effective_type": effective_type}
    hits: dict[str, None] = {}
    items = [c if isinstance(c, dict) else {"internal_path": c.internal_path, "type": c.type} for c in content_items]
    for rule in AUTO_HIDE_RULES:
        if not _rule_enabled(db, rule):
            continue
        for c in items:
            if rule.matches(ctx, c):
                hits[c["internal_path"]] = None
    return list(hits)


def _collect_rule_work(
    lib, rule: AutoHideRule, pick: Callable[[dict, dict], str | None]
) -> tuple[list[tuple[str, list[str]]], int]:
    work = []
    total = 0
    for filename, pkg in lib.store.package_index.items():
        if is_local_package(filename):
            continue
        ctx = _ctx(filename, pkg)
        paths = []
        for c in lib.store.content_by_package.get(filename, ()):
            if not rule.matches(ctx, c):
                continue
            p = pick(ctx, c)
            if p is not None:
                paths.append(p)
        if paths:
            work.append((filename, paths))
            total += len(paths)
    return work, total


def apply_auto_hide_rule(lib, rule_id: str, on_progress: ProgressFn | None = None) -> None:
    progress = on_progress or _noop
    rule = find_rule(rule_id)
    work, total = _collect_rule_work(lib, rule, lambda ctx, c: None if c["hidden"] else c["internal_path"])
    progress({"current": 0, "total": len(work), "items": total})
    for done, (filename, paths) in enumerate(work):
        progress({"current": done, "total": len(work), "filename": filename, "items": total})
        hide_package_content(lib.vam_dir, filename, paths)
        for p in paths:
            lib.store.update_pref(filename, p, "hidden", True)
    progress({"current": len(work), "total": len(work), "items": total})


def remove_auto_hide_rule(lib, rule_id: str, on_progress: ProgressFn | None = None) -> None:
    progress = on_progress or _noop
    rule = find_rule(rule_id)

    def pick(ctx: dict, c: dict) -> str | None:
        if not c["hidden"] or _claimed_by_any_except(lib.db, ctx, c, rule.id):
            return None
        return c["internal_path"]

    work, total = _collect_rule_work(lib, rule, pick)
    progress({"current": 0, "total": len(work), "items": total})
    for done, (filename, paths) in enumerate(work):
        progress({"current": done, "total": len(work), "filename": filename, "items": total})
        unhide_package_content(lib.vam_dir, filename, paths)
        for p in paths:
            lib.store.update_pref(filename, p, "hidden", False)
    progress({"current": len(work), "total": len(work), "items": total})


def sync_auto_hide_after_direct_change(
    lib, filename: str, is_direct: bool, content_items: Iterable | None = None
) -> None:
    pkg = lib.store.package_index.get(filename)
    if not pkg:
        return
    effective_type = effective_package_type(pkg)
    if content_items is not None:
        items = [
            c if isinstance(c, dict) else {"internal_path": c.internal_path, "type": c.type} for c in content_items
        ]
    else:
        items = [
            {"internal_path": c["internal_path"], "type": c["type"]}
            for c in lib.store.content_by_package.get(filename, ())
        ]
    hide = set(compute_auto_hide_paths_for_new_package(lib.db, filename, effective_type, is_direct, items))
    paths = [c["internal_path"] for c in items]
    to_hide = [p for p in paths if p in hide]
    to_unhide = [p for p in paths if p not in hide]
    if to_hide:
        hide_package_content(lib.vam_dir, filename, to_hide)
    if to_unhide:
        unhide_package_content(lib.vam_dir, filename, to_unhide)
    for p in to_hide:
        lib.store.update_pref(filename, p, "hidden", True)
    for p in to_unhide:
        lib.store.update_pref(filename, p, "hidden", False)


_INTEGRITY_WORKERS = max(1, min(4, os.cpu_count() or 1))


def run_integrity_check(
    lib, on_progress: ProgressFn | None = None, cancel: threading.Event | None = None, workers: int | None = None
) -> dict:
    progress = on_progress or _noop
    targets = []
    unpacked: list[str] = []
    for pkg in lib.store.package_index.values():
        if is_local_package(pkg["filename"]):
            continue
        p = lib.dirs.resolve_content_path(pkg)
        if p and os.path.isdir(p):
            unpacked.append(pkg["filename"])
        elif p and os.path.isfile(p):
            targets.append((pkg["filename"], p))
    started = time.monotonic()
    total = len(targets)
    checked: list[str] = []
    corrupted: list[str] = []
    errors: dict[str, str] = {}
    bytes_done = 0
    cancelled = False

    def verify(item):
        filename, path = item
        if cancel is not None and cancel.is_set():
            return filename, None, 0
        try:
            size = os.path.getsize(path)
            verify_package_full(path)
            return filename, "", size
        except (IntegrityError, OSError) as err:
            return filename, str(err), 0

    with ThreadPoolExecutor(max_workers=workers or _INTEGRITY_WORKERS) as pool:
        for filename, error, size in pool.map(verify, targets):
            if error is None:
                cancelled = True
                continue
            checked.append(filename)
            bytes_done += size
            if error:
                corrupted.append(filename)
                errors[filename] = error
                log.warning("Integrity check failed for %s: %s", filename, error)
            progress(
                {"step": len(checked), "total": total, "filename": filename, "status": "checking", "bytes": bytes_done}
            )
    progress(
        {
            "step": len(checked),
            "total": total,
            "filename": "",
            "status": "cancelled" if cancelled else "done",
            "bytes": bytes_done,
        }
    )
    return {
        "total": total,
        "checked": len(checked),
        "checked_files": checked,
        "corrupted": len(corrupted),
        "corrupted_files": corrupted,
        "errors": errors,
        "bytes": bytes_done,
        "seconds": time.monotonic() - started,
        "cancelled": cancelled,
        "unpacked": unpacked,
    }


def _walk_up(starts: Iterable[tuple[str, str]], max_depth: int) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out = []
    for start, source in starts:
        d = os.path.abspath(start)
        for _ in range(max_depth):
            if d not in seen:
                seen.add(d)
                out.append((d, source))
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return out


def resolve_vam_dir(path: str) -> str | None:
    d = os.path.abspath(os.path.expanduser(path))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, ADDON_PACKAGES)):
            return d
        if os.path.basename(d).lower() == ADDON_PACKAGES.lower() and os.path.isdir(d):
            return os.path.dirname(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def common_vam_locations() -> list[str]:
    import glob

    home = os.path.expanduser("~")
    names = ("VaM", "Virt-a-Mate", "VirtAMate")
    out: list[str] = []
    for r in (home, os.path.join(home, "Games")):
        out += [os.path.join(r, n) for n in names]
    prefixes = [os.path.join(home, ".wine")]
    if os.environ.get("WINEPREFIX"):
        prefixes.append(os.environ["WINEPREFIX"])
    for pattern in (
        os.path.join(home, ".local", "share", "wineprefixes", "*"),
        os.path.join(home, ".local", "share", "bottles", "bottles", "*"),
        os.path.join(home, "Games", "*"),
        os.path.join(home, ".var", "app", "*", "data", "wine"),
        os.path.join(home, ".steam", "steam", "steamapps", "compatdata", "*", "pfx"),
        os.path.join(home, ".local", "share", "Steam", "steamapps", "compatdata", "*", "pfx"),
    ):
        prefixes += sorted(glob.glob(pattern))
    for prefix in prefixes:
        drive_c = os.path.join(prefix, "drive_c")
        if not os.path.isdir(drive_c):
            continue
        out += [os.path.join(drive_c, n) for n in names]
        for depth in ("*", os.path.join("*", "*")):
            out += [os.path.dirname(p) for p in sorted(glob.glob(os.path.join(drive_c, depth, ADDON_PACKAGES)))]
    if os.name == "nt":
        out += [f"{letter}:\\{n}" for letter in "CDEFG" for n in names] + [
            f"{letter}:\\Games\\{n}" for letter in "CDEFG" for n in names
        ]
    seen: dict[str, None] = {}
    for c in out:
        seen.setdefault(c, None)
    return list(seen)


def detect_vam_dir(extra_candidates: Iterable[str] = ()) -> dict | None:
    import sys

    starts = [(os.getcwd(), "cwd"), (os.path.dirname(os.path.abspath(sys.argv[0] or ".")), "exe")]
    env = os.environ.get("VAM_DIR")
    if env:
        starts.insert(0, (env, "env"))
    for c in extra_candidates:
        starts.append((c, "candidate"))
    for d, source in _walk_up(starts, 5):
        if os.path.isdir(os.path.join(d, ADDON_PACKAGES)):
            return {"path": d, "source": source}
    for c in common_vam_locations():
        if os.path.isdir(os.path.join(c, ADDON_PACKAGES)):
            return {"path": c, "source": "common"}
    return None


def quick_count_vars(vam_dir: str) -> int:
    n = 0
    for _root, _dirs, files in os.walk(os.path.join(vam_dir, ADDON_PACKAGES)):
        n += sum(1 for f in files if f.lower().endswith(".var"))
    return n
