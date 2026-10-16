from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .classifier import ContentItem, FileEntry, classify_contents
from .lenient_json import loads_lenient

SCENE_ATOM_ID_TYPES = frozenset({"scene", "legacyScene", "legacyLook"})

_VAR_EXT_RE = re.compile(r"\.var(\.disabled)?$", re.IGNORECASE)
_DISABLED_TAIL_RE = re.compile(r"(?:\.var)?\.disabled$", re.IGNORECASE)
_VAR_TAIL_RE = re.compile(r"\.var$", re.IGNORECASE)
_DISABLED_RE = re.compile(r"\.disabled$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class VarName:
    creator: str
    package_name: str
    version: str


@dataclass
class VarInfo:
    meta: dict | None
    file_list: list[FileEntry]
    content_items: list[ContentItem] | None = None
    extracts: dict[str, bytes] = field(default_factory=dict)


class IntegrityError(Exception):
    """A .var failed full CRC verification."""


def canonical_var_filename(name: str) -> str:
    return _DISABLED_TAIL_RE.sub(".var", name)


def qvaro_disabled_name(canonical: str) -> str:
    return _VAR_TAIL_RE.sub(".DISABLED", canonical)


def parse_var_filename(filename: str) -> VarName | None:
    stem = _VAR_TAIL_RE.sub("", canonical_var_filename(filename))
    parts = stem.split(".")
    if len(parts) < 3:
        return None
    version = parts[-1]
    if not _DIGITS_RE.fullmatch(version):
        return None
    return VarName(creator=parts[0], package_name=".".join(parts[:-1]), version=version)


def is_var_filename(name: str) -> bool:
    if _VAR_EXT_RE.search(name):
        return True
    if _DISABLED_RE.search(name):
        return parse_var_filename(name) is not None
    return False


def is_unpacked_package(path: str | Path) -> bool:
    return os.path.isdir(path) and os.path.isfile(os.path.join(path, "meta.json"))


def walk_unpacked(root: str | Path) -> list[tuple[str, str, int, float]]:
    root = str(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((rel, full, st.st_size, st.st_mtime))
    return out


def package_signature(path: str | Path) -> tuple[int, float]:
    if os.path.isdir(path):
        files = walk_unpacked(path)
        return sum(f[2] for f in files), max((f[3] for f in files), default=os.stat(path).st_mtime)
    st = os.stat(path)
    return st.st_size, st.st_mtime


def _read_unpacked(root: str | Path, extract_scene_jsons: bool) -> VarInfo:
    files = walk_unpacked(root)
    file_list = [FileEntry(rel, size) for rel, _full, size, _m in files]
    meta = None
    try:
        with open(os.path.join(str(root), "meta.json"), "rb") as f:
            parsed = loads_lenient(f.read())
        meta = parsed if isinstance(parsed, dict) else None
    except Exception:
        meta = None
    content_items = None
    extracts: dict[str, bytes] = {}
    if extract_scene_jsons:
        content_items = classify_contents(file_list)
        wanted = {c.internal_path for c in content_items if c.type in SCENE_ATOM_ID_TYPES}
        by_rel = {rel: full for rel, full, _s, _m in files}
        for rel in wanted:
            try:
                with open(by_rel[rel], "rb") as f:
                    extracts[rel] = f.read()
            except (KeyError, OSError):
                pass
    return VarInfo(meta=meta, file_list=file_list, content_items=content_items, extracts=extracts)


def read_var(var_path: str | Path, extract_scene_jsons: bool = False) -> VarInfo:
    if os.path.isdir(var_path):
        return _read_unpacked(var_path, extract_scene_jsons)
    with zipfile.ZipFile(var_path) as zf:
        infos = zf.infolist()
        file_list = [FileEntry(i.filename, i.file_size) for i in infos if not i.filename.endswith("/")]

        meta = None
        meta_info = next((i for i in infos if i.filename == "meta.json"), None)
        if meta_info is not None:
            try:
                parsed = loads_lenient(zf.read(meta_info))
                meta = parsed if isinstance(parsed, dict) else None
            except Exception:
                meta = None

        content_items = None
        extracts: dict[str, bytes] = {}
        if extract_scene_jsons:
            content_items = classify_contents(file_list)
            wanted = {c.internal_path for c in content_items if c.type in SCENE_ATOM_ID_TYPES}
            for p in wanted:
                try:
                    extracts[p] = zf.read(p)
                except Exception:
                    pass
        return VarInfo(meta=meta, file_list=file_list, content_items=content_items, extracts=extracts)


def extract_files(var_path: str | Path, internal_paths: Iterable[str]) -> dict[str, bytes]:
    wanted = set(internal_paths)
    results: dict[str, bytes] = {}
    if not wanted:
        return results
    if os.path.isdir(var_path):
        for rel in wanted:
            try:
                with open(os.path.join(str(var_path), *rel.split("/")), "rb") as f:
                    results[rel] = f.read()
            except OSError:
                pass
        return results
    with zipfile.ZipFile(var_path) as zf:
        names = zf.NameToInfo
        for p in wanted:
            if p in names:
                results[p] = zf.read(p)
    return results


def extract_file(var_path: str | Path, internal_path: str) -> bytes | None:
    return extract_files(var_path, [internal_path]).get(internal_path)


def verify_zip_file(path: str | Path) -> bool:
    if os.path.isdir(path):
        return is_unpacked_package(path)
    try:
        with zipfile.ZipFile(path):
            return True
    except Exception:
        return False


def verify_package_full(path: str | Path) -> None:
    if os.path.isdir(path):
        if not is_unpacked_package(path):
            raise IntegrityError("unpacked folder without a readable meta.json")
        return
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
    except OSError:
        raise
    except Exception as err:
        raise IntegrityError(f"{type(err).__name__}: {err}") from err
    if bad is not None:
        raise IntegrityError(f"CRC32 mismatch in {bad}")
