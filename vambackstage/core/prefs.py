from __future__ import annotations

import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .classifier import strip_disabled_suffix
from .paths import ADDON_PACKAGES_FILE_PREFS, LOCAL_CONTENT_DIRS, LOCAL_PACKAGE_FILENAME, is_local_package

PrefsMap = dict[str, dict]

_STEM_WORKERS = 8


def prefs_key(package_filename: str, internal_path: str) -> str:
    return package_filename + "/" + strip_disabled_suffix(internal_path)


def prefs_dir(vam_dir: str | Path) -> Path:
    return Path(vam_dir) / ADDON_PACKAGES_FILE_PREFS


def sidecar_path(vam_dir: str | Path, package_filename: str, internal_path: str, ext: str) -> Path:
    if is_local_package(package_filename):
        return Path(vam_dir) / (strip_disabled_suffix(internal_path) + ext)
    stem = package_filename[:-4] if package_filename.lower().endswith(".var") else package_filename
    return prefs_dir(vam_dir) / stem / (internal_path + ext)


def _walk_sidecar_dir(dir_path: Path, relative: str, key_prefix: str, prefs: PrefsMap, require_sibling: bool) -> None:
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return
    siblings = None
    if require_sibling:
        siblings = set()
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False):
                    siblings.add(strip_disabled_suffix(e.name))
            except OSError:
                pass
    for e in entries:
        rel = f"{relative}/{e.name}" if relative else e.name
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                _walk_sidecar_dir(Path(e.path), rel, key_prefix, prefs, require_sibling)
                continue
            if not e.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        name = e.name
        if name.endswith(".hide"):
            ext = ".hide"
        elif name.endswith(".fav"):
            ext = ".fav"
        else:
            continue
        target = name[: -len(ext)]
        if siblings is not None and strip_disabled_suffix(target) not in siblings:
            continue
        content_path = strip_disabled_suffix(rel[: -len(ext)])
        key = key_prefix + "/" + content_path
        entry = prefs.get(key)
        if entry is None:
            entry = {"hidden": False, "favorite": False}
            prefs[key] = entry
        entry["hidden" if ext == ".hide" else "favorite"] = True


def read_all_prefs(vam_dir: str | Path) -> PrefsMap:
    prefs: PrefsMap = {}
    root = prefs_dir(vam_dir)
    try:
        stems = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        stems = []

    def walk_stem(entry: os.DirEntry) -> PrefsMap:
        local: PrefsMap = {}
        _walk_sidecar_dir(Path(entry.path), "", entry.name + ".var", local, False)
        return local

    if stems:
        with ThreadPoolExecutor(max_workers=_STEM_WORKERS) as pool:
            for local in pool.map(walk_stem, stems):
                prefs.update(local)

    for local_dir in LOCAL_CONTENT_DIRS:
        _walk_sidecar_dir(Path(vam_dir) / local_dir, local_dir, LOCAL_PACKAGE_FILENAME, prefs, True)
    return prefs


def read_package_prefs(vam_dir: str | Path, package_filename: str) -> PrefsMap:
    prefs: PrefsMap = {}
    if is_local_package(package_filename):
        return prefs
    stem = package_filename[:-4] if package_filename.lower().endswith(".var") else package_filename
    _walk_sidecar_dir(prefs_dir(vam_dir) / stem, "", package_filename, prefs, False)
    return prefs


def _set_sidecar(vam_dir: str | Path, package_filename: str, internal_path: str, ext: str, on: bool) -> Path:
    p = sidecar_path(vam_dir, package_filename, internal_path, ext)
    if on:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    else:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return p


def set_hidden(vam_dir: str | Path, package_filename: str, internal_path: str, hidden: bool) -> Path:
    return _set_sidecar(vam_dir, package_filename, internal_path, ".hide", hidden)


def set_favorite(vam_dir: str | Path, package_filename: str, internal_path: str, favorite: bool) -> Path:
    return _set_sidecar(vam_dir, package_filename, internal_path, ".fav", favorite)


def hide_package_content(vam_dir: str | Path, package_filename: str, content_paths: Iterable[str]) -> None:
    for p in content_paths:
        set_hidden(vam_dir, package_filename, p, True)


def unhide_package_content(vam_dir: str | Path, package_filename: str, content_paths: Iterable[str]) -> None:
    for p in content_paths:
        set_hidden(vam_dir, package_filename, p, False)


def copy_sidecars_to_new_version(
    vam_dir: str | Path, from_filename: str, to_filename: str, internal_paths: Iterable[str]
) -> int:
    if is_local_package(from_filename) or is_local_package(to_filename):
        return 0
    from_stem = from_filename[:-4] if from_filename.lower().endswith(".var") else from_filename
    from_dir = prefs_dir(vam_dir) / from_stem
    if not from_dir.is_dir():
        return 0
    copied = 0
    for ip in internal_paths:
        for ext in (".hide", ".fav"):
            if (from_dir / (ip + ext)).exists():
                try:
                    _set_sidecar(vam_dir, to_filename, ip, ext, True)
                    copied += 1
                except OSError:
                    pass
    return copied
