from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass

from .varfile import is_unpacked_package, parse_var_filename, verify_zip_file, walk_unpacked

BACKUP_SUFFIX = ".orig"

_STORED_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".assetbundle", ".ogg", ".mp3", ".mp4", ".zip", ".vac"}
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


class UnpackError(Exception):
    """A refused or failed unpack."""


@dataclass(frozen=True)
class UnpackResult:
    folder: str
    backup: str
    backup_created: bool
    files: int
    bytes: int


@dataclass(frozen=True)
class RepackResult:
    archive: str
    files: int
    bytes: int
    backup_removed: bool


@dataclass(frozen=True)
class RestoreResult:
    archive: str
    discarded: str | None


def backup_path(var_path: str | os.PathLike) -> str:
    return os.fspath(var_path) + BACKUP_SUFFIX


def has_backup(var_path: str | os.PathLike) -> bool:
    return os.path.isfile(backup_path(var_path))


def _require_var_name(path: str) -> str:
    name = os.path.basename(path)
    if not name.lower().endswith(".var") or parse_var_filename(name) is None:
        raise UnpackError(f"{name} is not a package name (Creator.Package.N.var)")
    return name


def _safe_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    out = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        parts = name.split("/")
        if (
            not name
            or "\0" in name
            or name.startswith("/")
            or any(p in ("", ".", "..") for p in parts)
            or _DRIVE_RE.match(parts[0])
        ):
            raise UnpackError(f"refusing to extract an entry that escapes the package folder: {info.filename!r}")
        out.append((info, name))
    return out


def _restore_mtime(dest: str, info: zipfile.ZipInfo) -> None:
    try:
        ts = time.mktime((*info.date_time, 0, 0, -1))
        os.utime(dest, (ts, ts))
    except (OverflowError, ValueError, OSError):
        pass


def unpack_package(var_path: str | os.PathLike) -> UnpackResult:
    var_path = os.fspath(var_path)
    name = _require_var_name(var_path)
    if os.path.isdir(var_path):
        raise UnpackError(f"{name} is already unpacked")
    if not os.path.isfile(var_path):
        raise UnpackError(f"{name} is not on disk")
    parent = os.path.dirname(var_path) or "."
    backup = backup_path(var_path)
    keep_older_backup = os.path.isfile(backup)
    try:
        zf = zipfile.ZipFile(var_path)
    except (zipfile.BadZipFile, OSError) as err:
        raise UnpackError(f"{name} is not a valid package archive: {err}") from err
    tmp = tempfile.mkdtemp(prefix=".unpack-", suffix=".tmp", dir=parent)
    files = total = 0
    try:
        with zf:
            for info, rel in _safe_members(zf):
                dest = os.path.join(tmp, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                _restore_mtime(dest, info)
                files += 1
                total += info.file_size
        if not os.path.isfile(os.path.join(tmp, "meta.json")):
            raise UnpackError(f"{name} has no meta.json: VaM would not load the unpacked folder")
        holder = None
        if keep_older_backup:
            holder = tempfile.mkdtemp(prefix=".unpack-old-", suffix=".tmp", dir=parent)
            moved_to = os.path.join(holder, name)
        else:
            moved_to = backup
        os.replace(var_path, moved_to)
        try:
            os.replace(tmp, var_path)
        except OSError:
            os.replace(moved_to, var_path)
            raise
        if holder:
            shutil.rmtree(holder, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return UnpackResult(var_path, backup, not keep_older_backup, files, total)


def _compression_for(rel: str) -> int:
    return zipfile.ZIP_STORED if os.path.splitext(rel)[1].lower() in _STORED_SUFFIXES else zipfile.ZIP_DEFLATED


def repack_package(folder: str | os.PathLike, *, keep_backup: bool = False) -> RepackResult:
    folder = os.fspath(folder)
    name = _require_var_name(folder)
    if not os.path.isdir(folder):
        raise UnpackError(f"{name} is not an unpacked folder")
    if not is_unpacked_package(folder):
        raise UnpackError(f"{name} has no meta.json: VaM would not load the archive")
    parent = os.path.dirname(folder) or "."
    fd, tmp = tempfile.mkstemp(prefix=".repack-", suffix=".tmp", dir=parent)
    os.close(fd)
    files = 0
    try:
        entries = walk_unpacked(folder)
        entries.sort(key=lambda e: (e[0] != "meta.json", e[0]))
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True, strict_timestamps=False) as zf:
            for rel, full, _size, _mtime in entries:
                zf.write(full, rel, compress_type=_compression_for(rel))
                files += 1
        holder = tempfile.mkdtemp(prefix=".repack-old-", suffix=".tmp", dir=parent)
        aside = os.path.join(holder, name)
        os.replace(folder, aside)
        try:
            os.replace(tmp, folder)
        except OSError:
            os.replace(aside, folder)
            shutil.rmtree(holder, ignore_errors=True)
            raise
        shutil.rmtree(holder, ignore_errors=True)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    backup_removed = False
    if not keep_backup:
        try:
            os.unlink(backup_path(folder))
            backup_removed = True
        except FileNotFoundError:
            pass
    return RepackResult(folder, files, os.path.getsize(folder), backup_removed)


def restore_original(var_path: str | os.PathLike) -> RestoreResult:
    var_path = os.fspath(var_path)
    name = _require_var_name(var_path)
    backup = backup_path(var_path)
    if not os.path.isfile(backup):
        raise UnpackError(f"There is no {name}{BACKUP_SUFFIX} to restore")
    if not verify_zip_file(backup):
        raise UnpackError(f"{name}{BACKUP_SUFFIX} is not a valid package archive; nothing was changed")
    parent = os.path.dirname(var_path) or "."
    if os.path.isdir(var_path):
        holder = tempfile.mkdtemp(prefix=".restore-old-", suffix=".tmp", dir=parent)
        aside = os.path.join(holder, name)
        os.replace(var_path, aside)
        try:
            os.replace(backup, var_path)
        except OSError:
            os.replace(aside, var_path)
            shutil.rmtree(holder, ignore_errors=True)
            raise
        shutil.rmtree(holder, ignore_errors=True)
        return RestoreResult(var_path, "folder")
    discarded = "archive" if os.path.isfile(var_path) else None
    os.replace(backup, var_path)
    return RestoreResult(var_path, discarded)
