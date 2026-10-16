import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from vambackstage.core.unpack import (
    UnpackError,
    backup_path,
    has_backup,
    repack_package,
    restore_original,
    unpack_package,
)
from vambackstage.core.varfile import read_var

from .conftest import build_var
from .helpers import by_name, drop, make_lib, var_bytes

NAME = "Auth.Pkg.1.var"
FILES = {"Saves/scene/A.json": '{"atoms":[]}', "Custom/Atom/Person/Appearance/L.vap": "{}"}


def _files(folder) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, names in os.walk(folder):
        for n in names:
            full = os.path.join(dirpath, n)
            with open(full, "rb") as f:
                out[os.path.relpath(full, folder).replace(os.sep, "/")] = f.read()
    return out


def _no_temp_leftovers(folder) -> bool:
    return not [n for n in os.listdir(folder) if n.startswith(".")]


class TestUnpack:
    def test_extracts_beside_backup(self, vam):
        data = var_bytes(NAME, files=FILES)
        p = vam.addon_packages / NAME
        p.write_bytes(data)
        res = unpack_package(p)
        assert p.is_dir() and res.folder == str(p) and res.files == 3 and res.backup_created
        assert res.bytes == sum(len(b) for b in _files(p).values())
        assert (vam.addon_packages / (NAME + ".orig")).read_bytes() == data
        files = _files(p)
        assert set(files) == {"meta.json", *FILES}
        assert files["Saves/scene/A.json"] == b'{"atoms":[]}'
        assert has_backup(p) and backup_path(p) == str(p) + ".orig"
        assert _no_temp_leftovers(vam.addon_packages)

    def test_restores_entry_timestamps(self, vam):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("meta.json", "{}")
            zf.writestr(zipfile.ZipInfo("Saves/scene/A.json", date_time=(2021, 5, 6, 7, 8, 10)), "{}")
        p = vam.addon_packages / NAME
        p.write_bytes(buf.getvalue())
        unpack_package(p)
        expected = time.mktime((2021, 5, 6, 7, 8, 10, 0, 0, -1))
        assert abs(os.stat(p / "Saves" / "scene" / "A.json").st_mtime - expected) < 2

    def test_refusals_leave_the_archive_alone(self, vam):
        p = vam.addon_packages / NAME
        p.write_bytes(b"not a zip")
        with pytest.raises(UnpackError, match="not a valid package archive"):
            unpack_package(p)
        assert p.read_bytes() == b"not a zip"
        p.write_bytes(build_var(files={"Saves/scene/A.json": "{}"}))
        with pytest.raises(UnpackError, match="meta.json"):
            unpack_package(p)
        assert p.is_file()
        with pytest.raises(UnpackError, match="not on disk"):
            unpack_package(vam.addon_packages / "Gone.Pkg.1.var")
        bad = vam.addon_packages / "Thing.var"
        bad.write_bytes(var_bytes(NAME))
        with pytest.raises(UnpackError, match="not a package name"):
            unpack_package(bad)
        assert _no_temp_leftovers(vam.addon_packages)

    def test_already_unpacked(self, vam):
        p = vam.addon_packages / NAME
        p.write_bytes(var_bytes(NAME))
        unpack_package(p)
        with pytest.raises(UnpackError, match="already unpacked"):
            unpack_package(p)

    @pytest.mark.parametrize("entry", ["../evil.json", "/abs.json", "Saves/../../x.json", "C:/win.json"])
    def test_refuses_entries_escaping_the_folder(self, vam, entry):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("meta.json", "{}")
            zf.writestr(entry, "{}")
        p = vam.addon_packages / NAME
        p.write_bytes(buf.getvalue())
        with pytest.raises(UnpackError, match="escapes the package folder"):
            unpack_package(p)
        assert p.is_file() and not has_backup(p)
        assert not (vam.addon_packages.parent / "evil.json").exists()
        assert _no_temp_leftovers(vam.addon_packages)

    def test_backslash_entries_become_folders(self, vam):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("meta.json", "{}")
            zf.writestr("Saves\\scene\\A.json", "{}")
        p = vam.addon_packages / NAME
        p.write_bytes(buf.getvalue())
        unpack_package(p)
        assert set(_files(p)) == {"meta.json", "Saves/scene/A.json"}

    def test_older_backup_is_kept_as_the_original(self, vam):
        original = var_bytes(NAME, files={"Saves/scene/A.json": "original"})
        current = var_bytes(NAME, files={"Saves/scene/A.json": "edited and repacked"})
        p = vam.addon_packages / NAME
        p.write_bytes(current)
        (vam.addon_packages / (NAME + ".orig")).write_bytes(original)
        res = unpack_package(p)
        assert not res.backup_created
        assert _files(p)["Saves/scene/A.json"] == b"edited and repacked"
        assert (vam.addon_packages / (NAME + ".orig")).read_bytes() == original
        assert _no_temp_leftovers(vam.addon_packages)


class TestRepack:
    def test_builds_archive_from_edited_folder(self, vam):
        p = vam.addon_packages / NAME
        p.write_bytes(var_bytes(NAME, files={**FILES, "Custom/Assets/pic.jpg": b"\xff\xd8jpeg"}))
        unpack_package(p)
        (p / "Saves" / "scene" / "A.json").write_text('{"atoms":[{"id":"x"}]}')
        (p / "Saves" / "scene" / "B.json").write_text("{}")
        (p / "Custom" / "Atom" / "Person" / "Appearance" / "L.vap").unlink()
        res = repack_package(p)
        assert p.is_file() and res.archive == str(p) and res.files == 4 and res.bytes == p.stat().st_size
        assert res.backup_removed and not has_backup(p)
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            assert names[0] == "meta.json"
            assert set(names) == {"meta.json", "Saves/scene/A.json", "Saves/scene/B.json", "Custom/Assets/pic.jpg"}
            assert zf.read("Saves/scene/A.json") == b'{"atoms":[{"id":"x"}]}'
            assert zf.getinfo("Custom/Assets/pic.jpg").compress_type == zipfile.ZIP_STORED
            assert zf.getinfo("Saves/scene/A.json").compress_type == zipfile.ZIP_DEFLATED
            assert zf.testzip() is None
        assert _no_temp_leftovers(vam.addon_packages)

    def test_keep_backup(self, vam):
        p = vam.addon_packages / NAME
        p.write_bytes(var_bytes(NAME, files=FILES))
        unpack_package(p)
        res = repack_package(p, keep_backup=True)
        assert not res.backup_removed and has_backup(p) and p.is_file()

    def test_refusals(self, vam):
        p = vam.addon_packages / NAME
        p.write_bytes(var_bytes(NAME))
        with pytest.raises(UnpackError, match="not an unpacked folder"):
            repack_package(p)
        unpack_package(p)
        (p / "meta.json").unlink()
        with pytest.raises(UnpackError, match="meta.json"):
            repack_package(p)
        assert p.is_dir() and _no_temp_leftovers(vam.addon_packages)


class TestRestore:
    def test_discards_the_folder(self, vam):
        data = var_bytes(NAME, files=FILES)
        p = vam.addon_packages / NAME
        p.write_bytes(data)
        unpack_package(p)
        (p / "Saves" / "scene" / "A.json").write_text("broken edit")
        res = restore_original(p)
        assert res.discarded == "folder" and p.is_file() and p.read_bytes() == data and not has_backup(p)
        assert _no_temp_leftovers(vam.addon_packages)

    def test_replaces_a_repacked_archive(self, vam):
        data = var_bytes(NAME, files=FILES)
        p = vam.addon_packages / NAME
        p.write_bytes(data)
        unpack_package(p)
        (p / "Saves" / "scene" / "A.json").write_text("edited")
        repack_package(p, keep_backup=True)
        assert p.read_bytes() != data
        res = restore_original(p)
        assert res.discarded == "archive" and p.read_bytes() == data and not has_backup(p)

    def test_refusals(self, vam):
        p = vam.addon_packages / NAME
        p.write_bytes(var_bytes(NAME))
        with pytest.raises(UnpackError, match="no Auth.Pkg.1.var.orig"):
            restore_original(p)
        unpack_package(p)
        (vam.addon_packages / (NAME + ".orig")).write_bytes(b"corrupt backup")
        with pytest.raises(UnpackError, match="not a valid package archive"):
            restore_original(p)
        assert p.is_dir() and (p / "meta.json").exists()


def test_library_unpack_edit_repack(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Dep.Lib.1.var")
    drop(vam.addon_packages, NAME, deps=["Dep.Lib.1"], files={"Saves/scene/A.json": '{"atoms":[]}'})
    lib.scan()
    events = []
    lib.events.subscribe("packages:updated", lambda ch, p: events.append(ch))
    res = lib.unpack_package(NAME)
    assert res["files"] == 2 and res["backup_created"] and res["folder"] == str(vam.addon_packages / NAME)
    assert events == ["packages:updated"]
    pk = by_name(lib)[NAME]
    assert pk["is_unpacked"] and pk["is_direct"] and pk["dep_count"] == 1
    d = lib.package_detail(NAME)
    assert d["is_unpacked"] and d["has_backup"] and d["package_path"] == str(vam.addon_packages / NAME)
    folder = Path(lib.unpacked_folder(NAME))
    assert folder == vam.addon_packages / NAME
    with pytest.raises(ValueError, match="already unpacked"):
        lib.unpack_package(NAME)
    with pytest.raises(ValueError, match="not unpacked"):
        lib.unpacked_folder("Dep.Lib.1.var")

    meta = json.loads((folder / "meta.json").read_text())
    meta["dependencies"] = {}
    (folder / "meta.json").write_text(json.dumps(meta))
    (folder / "Saves" / "scene" / "B.json").write_text('{"atoms":[]}')
    assert lib.reindex_package(NAME)["filename"] == NAME
    pk = by_name(lib)
    assert pk[NAME]["dep_count"] == 0 and pk[NAME]["content_count"] == 2 and pk["Dep.Lib.1.var"]["is_orphan"]

    res = lib.repack_package(NAME)
    assert res["backup_removed"] and res["files"] == 3
    p = vam.addon_packages / NAME
    assert p.is_file() and not has_backup(p)
    pk = by_name(lib)[NAME]
    assert not pk["is_unpacked"] and pk["content_count"] == 2 and pk["dep_count"] == 0 and pk["is_direct"]
    info = read_var(p)
    assert info.meta["dependencies"] == {} and "Saves/scene/B.json" in [f.path for f in info.file_list]
    assert lib.package_detail(NAME)["has_backup"] is False
    with pytest.raises(ValueError, match="not unpacked"):
        lib.repack_package(NAME)
    with pytest.raises(UnpackError):
        lib.restore_original(NAME)
    assert lib.scan().scanned == 0
    lib.close()


def test_library_restore_discards_edits(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Dep.Lib.1.var")
    drop(vam.addon_packages, NAME, deps=["Dep.Lib.1"])
    lib.scan()
    lib.unpack_package(NAME)
    folder = Path(lib.unpacked_folder(NAME))
    (folder / "meta.json").write_text("{}")
    lib.reindex_package(NAME)
    assert by_name(lib)[NAME]["dep_count"] == 0
    res = lib.restore_original(NAME)
    assert res["discarded"] == "folder"
    pk = by_name(lib)[NAME]
    assert not pk["is_unpacked"] and pk["dep_count"] == 1 and (vam.addon_packages / NAME).is_file()
    assert not lib.package_detail(NAME)["has_backup"]
    lib.close()


def test_library_repack_keeping_backup_then_restore(vam):
    lib = make_lib(vam)
    data = drop(vam.addon_packages, NAME).read_bytes()
    lib.scan()
    lib.unpack_package(NAME)
    (Path(lib.unpacked_folder(NAME)) / "Saves" / "scene" / "extra.json").write_text("{}")
    res = lib.repack_package(NAME, keep_backup=True)
    assert not res["backup_removed"]
    d = lib.package_detail(NAME)
    assert d["has_backup"] and not d["is_unpacked"] and d["content_count"] == 2
    res = lib.restore_original(NAME)
    assert res["discarded"] == "archive" and (vam.addon_packages / NAME).read_bytes() == data
    assert by_name(lib)[NAME]["content_count"] == 1
    lib.close()


def test_library_unpack_disabled_and_dummy(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Marker.Pkg.1.var", marker=True)
    drop(vam.addon_packages, "Suffix.Pkg.1.var", disabled=True)
    lib.scan()
    lib.create_dummies(["Gone.Pkg.1"])
    lib.unpack_package("Marker.Pkg.1.var")
    pk = by_name(lib)["Marker.Pkg.1.var"]
    assert pk["is_unpacked"] and pk["storage_state"] == "disabled"
    assert (vam.addon_packages / "Marker.Pkg.1.var").is_dir()
    assert (vam.addon_packages / "Marker.Pkg.1.var.disabled").stat().st_size == 0
    with pytest.raises(ValueError, match="enable it first"):
        lib.unpack_package("Suffix.Pkg.1.var")
    with pytest.raises(ValueError, match="dummy"):
        lib.unpack_package("Gone.Pkg.1.var")
    with pytest.raises(KeyError):
        lib.unpack_package("Nope.Pkg.1.var")
    lib.close()


def test_backup_follows_the_package_between_folders(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, NAME)
    aux = vam.aux_dir("offload")
    aid = lib.add_library_dir(str(aux))
    lib.set_setting("disable_behavior", f"move-to:{aid}")
    lib.scan()
    lib.unpack_package(NAME)
    lib.toggle_enabled(NAME)
    assert (aux / NAME).is_dir() and (aux / (NAME + ".orig")).is_file()
    assert not (vam.addon_packages / (NAME + ".orig")).exists()
    d = lib.package_detail(NAME)
    assert d["storage_state"] == "offloaded" and d["has_backup"] and d["package_path"] == str(aux / NAME)
    lib.toggle_enabled(NAME)
    assert (vam.addon_packages / NAME).is_dir() and (vam.addon_packages / (NAME + ".orig")).is_file()
    assert lib.restore_original(NAME)["discarded"] == "folder"
    assert (vam.addon_packages / NAME).is_file()
    lib.close()
