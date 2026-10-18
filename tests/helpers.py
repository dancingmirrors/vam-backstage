from __future__ import annotations

from pathlib import Path

from vambackstage.core.library import Library

from .conftest import build_var, place_var


def make_lib(vam) -> Library:
    lib = Library(vam.db_path)
    lib.set_vam_dir(str(vam.vam_dir))
    return lib


def var_bytes(name: str, deps=None, files=None, extra_meta=None) -> bytes:
    parts = name[:-4].split(".")
    group = ".".join(parts[:-1])
    meta = {"packageName": group, "creator": parts[0], "licenseType": "CC BY"}
    if deps:
        meta["dependencies"] = {d: {"licenseType": "CC BY", "dependencies": {}} for d in deps}
    if extra_meta:
        meta.update(extra_meta)
    if files is None:
        files = {f"Saves/scene/{parts[-2]}.json": '{"atoms":[]}'}
    return build_var(meta=meta, files=files)


def drop(dir_path, name: str, deps=None, files=None, **layout) -> Path:
    return place_var(Path(dir_path), name, var_bytes(name, deps, files), **layout)


def by_name(lib: Library) -> dict[str, dict]:
    return {p["filename"]: p for p in lib.list_packages()}


def row(lib: Library, filename: str) -> dict | None:
    return lib.db.get_package(filename)
