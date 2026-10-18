from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .library_dirs import LibraryDirs, classify_main_var_on_disk
from .paths import ACTIVENESS, STORAGE_STATES, is_local_package
from .varfile import qvaro_disabled_name

log = logging.getLogger(__name__)

AUX_STORAGE_STATES = frozenset({"offloaded", "archived"})

DISABLE_BEHAVIOR_SUFFIX = "suffix"
_MOVE_TO_PREFIX = "move-to:"


@dataclass(frozen=True)
class StorageTarget:
    storage_state: str
    library_dir_id: int | None = None


@dataclass(frozen=True)
class DisableBehavior:
    kind: str
    aux_dir_id: int | None = None


def disable_behavior_move_to(aux_dir_id: int) -> str:
    return f"{_MOVE_TO_PREFIX}{aux_dir_id}"


def parse_disable_behavior(value: str | None) -> DisableBehavior:
    if not value or value == DISABLE_BEHAVIOR_SUFFIX or not value.startswith(_MOVE_TO_PREFIX):
        return DisableBehavior("suffix")
    id_str = value[len(_MOVE_TO_PREFIX) :]
    if not id_str.isdigit():
        return DisableBehavior("suffix")
    return DisableBehavior("move-to", int(id_str))


def disable_target_for(behavior: DisableBehavior) -> StorageTarget:
    if behavior.kind == "move-to":
        return StorageTarget("offloaded", behavior.aux_dir_id)
    return StorageTarget("disabled", None)


def next_storage_state_for_intent(
    current: str, intent: str, disable_target: StorageTarget | None = None
) -> StorageTarget | None:
    if intent == "enable":
        return None if current == "enabled" else StorageTarget("enabled", None)
    if intent == "disable":
        if current != "enabled":
            return None
        return disable_target or StorageTarget("disabled", None)
    return None


def compute_install_target(
    dependents: Iterable[str] | None, package_index: Mapping[str, dict], disable_behavior_target_id: int | None = None
) -> StorageTarget | None:
    if not dependents:
        return None
    has_enabled = has_disabled = False
    offload_dirs: list[int] = []
    archive_dirs: list[int] = []
    for fn in dependents:
        dep = package_index.get(fn)
        if not dep:
            continue
        s = dep.get("storage_state")
        if s == "enabled":
            has_enabled = True
        elif s == "disabled":
            has_disabled = True
        elif s == "offloaded" and dep.get("library_dir_id") is not None:
            if dep["library_dir_id"] not in offload_dirs:
                offload_dirs.append(dep["library_dir_id"])
        elif s == "archived" and dep.get("library_dir_id") is not None:
            if dep["library_dir_id"] not in archive_dirs:
                archive_dirs.append(dep["library_dir_id"])
    if has_enabled:
        return None
    if has_disabled:
        return StorageTarget("disabled", None)
    if offload_dirs:
        target = disable_behavior_target_id if disable_behavior_target_id in offload_dirs else offload_dirs[0]
        return StorageTarget("offloaded", target)
    if not archive_dirs:
        return None
    return StorageTarget("archived", archive_dirs[0])


@dataclass
class ResettlePlan:
    to_prune: set[str]
    decisions: dict[str, StorageTarget]


class _IndexView(Protocol):
    def get(self, fn: str) -> dict | None: ...


def plan_resettle(
    candidates: Iterable[str],
    package_index: _IndexView,
    reverse_deps: Mapping[str, set[str]],
    replaceable_set: set[str],
    disable_behavior_target_id: int | None = None,
    prune: bool = True,
) -> ResettlePlan:
    overlay: dict[str, dict] = {}

    class Sim:
        def get(self, fn):
            return overlay.get(fn) or package_index.get(fn)

    sim = Sim()
    to_prune: set[str] = set()
    decisions: dict[str, StorageTarget] = {}

    queue = list(dict.fromkeys(candidates))
    watchers: dict[str, list[str]] = {}
    for fn in queue:
        for d in reverse_deps.get(fn, ()):
            watchers.setdefault(d, []).append(fn)
    queued = set(queue)

    def enqueue_watchers(fn: str) -> None:
        for w in watchers.get(fn, ()):
            if w not in queued:
                queued.add(w)
                queue.append(w)

    head = 0
    while head < len(queue):
        fn = queue[head]
        head += 1
        queued.discard(fn)
        if fn in to_prune:
            continue
        pkg = sim.get(fn)
        if not pkg or pkg.get("is_direct") or pkg.get("storage_state") == "archived":
            continue
        all_dependents = reverse_deps.get(fn)
        dependents = {d for d in all_dependents if d not in to_prune} if all_dependents else None
        target = compute_install_target(dependents, sim, disable_behavior_target_id)
        if target is None:
            if fn in decisions:
                del decisions[fn]
                overlay.pop(fn, None)
                enqueue_watchers(fn)
            continue
        if ACTIVENESS.get(target.storage_state, -1) >= ACTIVENESS.get(pkg.get("storage_state"), -1):
            continue
        if target.storage_state == "archived" and prune and fn in replaceable_set:
            to_prune.add(fn)
            decisions.pop(fn, None)
            overlay.pop(fn, None)
        else:
            decisions[fn] = target
            overlay[fn] = {**pkg, "storage_state": target.storage_state, "library_dir_id": target.library_dir_id}
        enqueue_watchers(fn)
    return ResettlePlan(to_prune, decisions)


class StorageStateError(Exception):
    pass


def _stat(p: str) -> os.stat_result | None:
    try:
        return os.stat(p)
    except OSError:
        return None


def _lstat(p: str) -> os.stat_result | None:
    try:
        return os.lstat(p)
    except OSError:
        return None


def guarded_rename(src: str, dst: str) -> None:
    import stat as statmod

    src_stat = _stat(src)
    if src_stat is None:
        raise StorageStateError(f"Source file missing: {src}")
    dst_stat = _lstat(dst)
    if dst_stat is not None:
        replaceable = statmod.S_ISREG(dst_stat.st_mode) and (
            dst_stat.st_size == src_stat.st_size or dst_stat.st_size == 0
        )
        if not replaceable:
            raise StorageStateError(
                f"Refusing to move {src} -> {dst}: a different file already exists at the destination "
                f"({dst_stat.st_size} bytes vs source {src_stat.st_size} bytes)"
            )
        if dst_stat.st_size == src_stat.st_size:
            os.unlink(src)
            return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)


def move_backup_along(from_path: str, to_path: str, filename: str) -> bool:
    src = os.path.join(os.path.dirname(from_path), filename + ".orig")
    dst = to_path + ".orig"
    if not os.path.isfile(src) or os.path.lexists(dst):
        return False
    try:
        os.replace(src, dst)
    except OSError as err:
        log.warning("could not move the backup %s along with its package: %s", src, err)
        return False
    return True


def ensure_empty_marker(marker_path: str) -> bool:
    s = _stat(marker_path)
    if s is not None:
        if s.st_size == 0:
            return False
        raise StorageStateError(
            f"Refusing to mark disabled: unexpected non-empty file at {marker_path} ({s.st_size} bytes)"
        )
    with open(marker_path, "wb"):
        pass
    return True


def remove_disabled_marker(marker_path: str, content_path: str) -> bool:
    s = _stat(marker_path)
    if s is None:
        return False
    if s.st_size != 0:
        c = _stat(content_path)
        if c is None or c.st_size != s.st_size:
            raise StorageStateError(
                f"Refusing to remove {marker_path}: not an empty marker and not a byte-identical copy of {content_path}"
            )
    os.unlink(marker_path)
    return True


def resolve_current_content_path(dirs: LibraryDirs, pkg: dict, main_bare: str) -> str:
    sub = pkg.get("subpath") or ""
    if pkg.get("storage_state") in AUX_STORAGE_STATES:
        aux = dirs.get_library_dir_path(pkg.get("library_dir_id"))
        if not aux:
            raise StorageStateError(
                f"Cannot resolve current path for {pkg['filename']} (library_dir_id={pkg.get('library_dir_id')})"
            )
        return os.path.join(aux, sub, pkg["filename"]) if sub else os.path.join(aux, pkg["filename"])
    cls = classify_main_var_on_disk(main_bare)
    if not cls.present:
        raise StorageStateError(f"Source file missing for {pkg['filename']} in main ({main_bare})")
    return cls.content_path  # type: ignore[return-value]


@dataclass
class ApplyResult:
    ok: bool
    from_path: str | None
    to_path: str | None
    changed: bool


def apply_storage_state(lib, filename: str, target: StorageTarget) -> ApplyResult:
    if is_local_package(filename):
        return ApplyResult(True, None, None, False)
    if target.storage_state not in STORAGE_STATES:
        raise StorageStateError(f"Invalid storageState: {target.storage_state} (filename={filename})")
    dirs: LibraryDirs = lib.dirs
    if target.storage_state in AUX_STORAGE_STATES and target.library_dir_id is None:
        raise StorageStateError(f"Illegal target: {target.storage_state} requires a libraryDirId (filename={filename})")
    if target.storage_state not in AUX_STORAGE_STATES and target.library_dir_id is not None:
        raise StorageStateError(
            f"Illegal target: {target.storage_state} must have libraryDirId=null (filename={filename})"
        )
    if target.storage_state == "archived" and not dirs.is_archive_library_dir(target.library_dir_id):
        raise StorageStateError(f"Illegal target: archived requires an archive-role libraryDirId (filename={filename})")
    if target.storage_state == "offloaded" and dirs.is_archive_library_dir(target.library_dir_id):
        raise StorageStateError(
            f"Illegal target: offloaded requires an offload-role libraryDirId (filename={filename})"
        )

    pkg = lib.store.package_index.get(filename)
    if not pkg:
        raise StorageStateError(f"Package not in store: {filename}")
    main_dir = dirs.get_library_dir_path(None)
    if not main_dir:
        raise StorageStateError("Main library directory not configured")
    target_dir = dirs.get_library_dir_path(target.library_dir_id)
    if not target_dir:
        raise StorageStateError(f"Library directory not configured for libraryDirId={target.library_dir_id}")

    subpath = pkg.get("subpath") or ""
    main_bare_at_source = os.path.join(main_dir, subpath, filename) if subpath else os.path.join(main_dir, filename)
    from_path = resolve_current_content_path(dirs, pkg, main_bare_at_source)

    def with_sub(d: str) -> str:
        return os.path.join(d, subpath) if subpath else d

    main_marker = os.path.join(with_sub(main_dir), filename) + ".disabled"
    to_path = os.path.join(with_sub(target_dir), filename)

    moved = False
    if os.path.normcase(os.path.normpath(from_path)) != os.path.normcase(os.path.normpath(to_path)):
        guarded_rename(from_path, to_path)
        move_backup_along(from_path, to_path, filename)
        moved = True

    if target.storage_state == "disabled":
        marker_changed = ensure_empty_marker(main_marker)
    else:
        marker_changed = remove_disabled_marker(main_marker, to_path)
        qvaro_sibling = qvaro_disabled_name(os.path.join(with_sub(main_dir), filename))
        if os.path.normcase(os.path.normpath(qvaro_sibling)) != os.path.normcase(os.path.normpath(from_path)):
            marker_changed = remove_disabled_marker(qvaro_sibling, to_path) or marker_changed

    lib.db.set_storage_state(filename, target.storage_state, target.library_dir_id, subpath)
    lib.store.patch_storage_state([filename], target.storage_state, target.library_dir_id, subpath)
    return ApplyResult(True, from_path, to_path, moved or marker_changed)
