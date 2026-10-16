from __future__ import annotations

import os
from dataclasses import dataclass

from .db import Database
from .paths import ADDON_PACKAGES, ADDON_PACKAGES_FILE_PREFS, LOCAL_CONTENT_DIRS
from .varfile import qvaro_disabled_name


@dataclass(frozen=True)
class MainVarClass:
    present: bool
    storage_state: str | None = None
    content_in_disabled: bool = False


def classify_main_var(bare_size: int | None, disabled_size: int | None) -> MainVarClass:
    has_bare = bare_size is not None
    bare_has_content = has_bare and bare_size > 0
    if disabled_size is not None:
        if bare_has_content:
            return MainVarClass(True, "disabled", False)
        if disabled_size > 0:
            return MainVarClass(True, "disabled", True)
        return MainVarClass(False)
    if has_bare:
        return MainVarClass(True, "enabled", False)
    return MainVarClass(False)


@dataclass
class OnDiskVar:
    present: bool
    storage_state: str | None = None
    content_path: str | None = None
    content_stat: os.stat_result | None = None


def _stat_or_none(p: str) -> os.stat_result | None:
    try:
        return os.stat(p)
    except OSError:
        return None


def classify_main_var_on_disk(bare_path: str, disabled_known_absent: bool = False) -> OnDiskVar:
    marker_path = bare_path + ".disabled"
    qvaro_path = qvaro_disabled_name(bare_path)
    bare_stat = _stat_or_none(bare_path)
    marker_stat = None if disabled_known_absent else _stat_or_none(marker_path)
    qvaro_stat = None if disabled_known_absent else _stat_or_none(qvaro_path)
    use_qvaro = marker_stat is None and qvaro_stat is not None and qvaro_stat.st_size > 0
    disabled_path, disabled_stat = (qvaro_path, qvaro_stat) if use_qvaro else (marker_path, marker_stat)
    import stat as _stat

    bare_size = None if bare_stat is None else (1 if _stat.S_ISDIR(bare_stat.st_mode) else bare_stat.st_size)
    cls = classify_main_var(bare_size, disabled_stat.st_size if disabled_stat else None)
    if not cls.present:
        return OnDiskVar(False)
    if cls.content_in_disabled:
        return OnDiskVar(True, cls.storage_state, disabled_path, disabled_stat)
    return OnDiskVar(True, cls.storage_state, bare_path, bare_stat)


def library_rel_subpath(library_dir: str, full_path: str) -> str:
    if not library_dir or not full_path:
        return ""
    try:
        rel = os.path.relpath(os.path.dirname(full_path), library_dir)
    except ValueError:
        return ""
    if not rel or rel == ".":
        return ""
    if rel.startswith("..") or os.path.isabs(rel):
        return ""
    return rel.replace(os.sep, "/")


def is_path_inside(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    a = child.rstrip("/\\")
    b = parent.rstrip("/\\")
    if a == b:
        return True
    return a.startswith(b + "/") or a.startswith(b + "\\")


def _real(p: str) -> str:
    try:
        return os.path.realpath(p)
    except OSError:
        return p


class LibraryDirs:

    def __init__(self, db: Database):
        self.db = db
        self._by_id: dict[int, dict] = {}
        self.refresh()

    def refresh(self) -> None:
        self._by_id = {row["id"]: row for row in self.db.list_library_dirs()}

    @property
    def vam_dir(self) -> str | None:
        return self.db.get_setting("vam_dir")

    def main_dir_path(self) -> str | None:
        v = self.vam_dir
        return os.path.join(v, ADDON_PACKAGES) if v else None

    def get_library_dir_path(self, library_dir_id: int | None) -> str | None:
        if library_dir_id is None:
            return self.main_dir_path()
        row = self._by_id.get(library_dir_id)
        return row["path"] if row else None

    def all_library_dirs(self) -> list[dict]:
        out = []
        main = self.main_dir_path()
        if main:
            out.append({"id": None, "path": main, "archive": False})
        for row in self._by_id.values():
            out.append({"id": row["id"], "path": row["path"], "archive": bool(row["archive"])})
        return out

    def aux_library_dirs(self) -> list[dict]:
        return list(self._by_id.values())

    def is_archive_library_dir(self, library_dir_id: int | None) -> bool:
        if library_dir_id is None:
            return False
        row = self._by_id.get(library_dir_id)
        return bool(row and row["archive"])

    def archive_library_dirs(self) -> list[dict]:
        return [r for r in self._by_id.values() if r["archive"]]

    def pkg_var_path(self, pkg: dict) -> str | None:
        if not pkg:
            return None
        d = self.get_library_dir_path(pkg.get("library_dir_id"))
        if not d:
            return None
        sub = pkg.get("subpath") or ""
        return os.path.join(d, sub, pkg["filename"]) if sub else os.path.join(d, pkg["filename"])

    def resolve_content_path(self, pkg: dict) -> str | None:
        nominal = self.pkg_var_path(pkg)
        if not nominal:
            return None
        if pkg.get("storage_state") == "disabled":
            cls = classify_main_var_on_disk(nominal)
            return cls.content_path if cls.present else nominal
        return nominal

    def validate_new_aux_dir_path(self, path: str) -> str | None:
        if not path or not isinstance(path, str):
            return "Invalid path"
        main = self.main_dir_path()
        if main:
            if _real(path) == _real(main):
                return "That folder is your main AddonPackages directory and is already part of your library."
            if is_path_inside(path, main) or is_path_inside(main, path):
                return "Offload directory cannot overlap the main AddonPackages directory"
        vam_dir = self.vam_dir
        if vam_dir:
            roots = [os.path.join(vam_dir, ADDON_PACKAGES_FILE_PREFS)] + [
                os.path.join(vam_dir, d) for d in LOCAL_CONTENT_DIRS
            ]
            for root in roots:
                if is_path_inside(path, root) or is_path_inside(root, path):
                    return f"Offload directory cannot overlap a VaM-managed directory: {root}"
        for aux in self._by_id.values():
            if is_path_inside(path, aux["path"]) or is_path_inside(aux["path"], path):
                return f"Offload directory overlaps an already-registered directory: {aux['path']}"
        return None

    def same_filesystem_as_main(self, path: str) -> bool:
        main = self.main_dir_path()
        if not main:
            return False
        try:
            return os.stat(path).st_dev == os.stat(main).st_dev
        except OSError:
            return False
