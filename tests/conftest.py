from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from vambackstage.core.paths import ADDON_PACKAGES, ADDON_PACKAGES_FILE_PREFS


@dataclass
class VamLayout:
    root: Path
    vam_dir: Path
    addon_packages: Path
    prefs_dir: Path
    saves_dir: Path
    custom_dir: Path
    db_path: Path

    def aux_dir(self, name: str = "aux") -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p


@pytest.fixture
def vam(tmp_path: Path) -> VamLayout:
    vam_dir = tmp_path / "VaM"
    layout = VamLayout(
        root=tmp_path,
        vam_dir=vam_dir,
        addon_packages=vam_dir / ADDON_PACKAGES,
        prefs_dir=vam_dir / ADDON_PACKAGES_FILE_PREFS,
        saves_dir=vam_dir / "Saves",
        custom_dir=vam_dir / "Custom",
        db_path=tmp_path / "backstage.db",
    )
    for d in (layout.addon_packages, layout.prefs_dir, layout.saves_dir, layout.custom_dir):
        d.mkdir(parents=True, exist_ok=True)
    return layout


def build_var(
    meta: dict | None = None, files: dict[str, bytes | str] | None = None, meta_raw: str | bytes | None = None
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if meta_raw is not None:
            zf.writestr("meta.json", meta_raw)
        elif meta is not None:
            zf.writestr("meta.json", json.dumps(meta, indent=2))
        for path, content in (files or {}).items():
            zf.writestr(path, content)
    return buf.getvalue()


def place_var(
    dir_path: Path, name: str, data: bytes, *, disabled: bool = False, marker: bool = False, qvaro: bool = False
) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    if marker:
        bare = dir_path / name
        bare.write_bytes(data)
        (dir_path / (name + ".disabled")).write_bytes(b"")
        return bare
    if qvaro:
        p = dir_path / (name[: -len(".var")] + ".DISABLED")
        p.write_bytes(data)
        return p
    p = dir_path / (name + ".disabled" if disabled else name)
    p.write_bytes(data)
    return p


def place_empty_marker(dir_path: Path, name: str) -> Path:
    p = dir_path / (name + ".disabled")
    p.write_bytes(b"")
    return p


def set_mtime(path: Path, seconds: float) -> None:
    os.utime(path, (seconds, seconds))
