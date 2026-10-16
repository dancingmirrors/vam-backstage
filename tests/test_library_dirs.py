import os

from vambackstage.core.db import Database
from vambackstage.core.library_dirs import (
    LibraryDirs,
    classify_main_var,
    classify_main_var_on_disk,
    is_path_inside,
    library_rel_subpath,
)

from .conftest import build_var, place_var


def test_classify_main_var():
    assert classify_main_var(None, None).present is False
    assert classify_main_var(10, None) == classify_main_var(10, None)
    c = classify_main_var(10, None)
    assert (c.present, c.storage_state, c.content_in_disabled) == (True, "enabled", False)
    c = classify_main_var(10, 0)
    assert (c.present, c.storage_state, c.content_in_disabled) == (True, "disabled", False)
    c = classify_main_var(None, 10)
    assert (c.present, c.storage_state, c.content_in_disabled) == (True, "disabled", True)
    c = classify_main_var(0, 10)
    assert (c.present, c.storage_state, c.content_in_disabled) == (True, "disabled", True)
    assert classify_main_var(None, 0).present is False
    assert classify_main_var(0, 0).present is False
    c = classify_main_var(0, None)
    assert (c.present, c.storage_state) == (True, "enabled")


def test_classify_on_disk(vam):
    data = build_var(files={"a.txt": "a"})
    bare = place_var(vam.addon_packages, "A.B.1.var", data)
    r = classify_main_var_on_disk(str(bare))
    assert (r.present, r.storage_state, r.content_path) == (True, "enabled", str(bare))
    place_var(vam.addon_packages, "A.B.2.var", data, marker=True)
    r = classify_main_var_on_disk(str(vam.addon_packages / "A.B.2.var"))
    assert (r.storage_state, r.content_path) == ("disabled", str(vam.addon_packages / "A.B.2.var"))
    place_var(vam.addon_packages, "A.B.3.var", data, disabled=True)
    r = classify_main_var_on_disk(str(vam.addon_packages / "A.B.3.var"))
    assert (r.storage_state, r.content_path) == ("disabled", str(vam.addon_packages / "A.B.3.var.disabled"))
    place_var(vam.addon_packages, "A.B.4.var", data, qvaro=True)
    r = classify_main_var_on_disk(str(vam.addon_packages / "A.B.4.var"))
    assert (r.storage_state, r.content_path) == ("disabled", str(vam.addon_packages / "A.B.4.DISABLED"))
    assert classify_main_var_on_disk(str(vam.addon_packages / "A.B.5.var")).present is False
    r = classify_main_var_on_disk(str(vam.addon_packages / "A.B.2.var"), disabled_known_absent=True)
    assert r.storage_state == "enabled"


def test_rel_subpath_and_inside():
    assert library_rel_subpath("/lib", "/lib/A.1.var") == ""
    assert library_rel_subpath("/lib", "/lib/sub/deep/A.1.var") == "sub/deep"
    assert library_rel_subpath("/lib", "/other/A.1.var") == ""
    assert library_rel_subpath("", "/x") == ""
    assert is_path_inside("/a/b", "/a")
    assert is_path_inside("/a", "/a/")
    assert not is_path_inside("/ab", "/a")


def test_registry_and_resolution(vam):
    db = Database(vam.db_path)
    db.set_setting("vam_dir", str(vam.vam_dir))
    dirs = LibraryDirs(db)
    assert dirs.main_dir_path() == str(vam.addon_packages)
    aux = vam.aux_dir("offload")
    arch = vam.aux_dir("archive")
    i1 = db.insert_library_dir(str(aux))
    i2 = db.insert_library_dir(str(arch), archive=True)
    dirs.refresh()
    assert [d["id"] for d in dirs.all_library_dirs()] == [None, i1, i2]
    assert (
        dirs.is_archive_library_dir(i2)
        and not dirs.is_archive_library_dir(i1)
        and not dirs.is_archive_library_dir(None)
    )
    assert [d["id"] for d in dirs.archive_library_dirs()] == [i2]
    pkg = {"filename": "A.B.1.var", "library_dir_id": i1, "subpath": "sub", "storage_state": "offloaded"}
    assert dirs.pkg_var_path(pkg) == os.path.join(str(aux), "sub", "A.B.1.var")
    assert dirs.resolve_content_path(pkg) == dirs.pkg_var_path(pkg)
    data = build_var(files={"a": "a"})
    place_var(vam.addon_packages, "C.D.1.var", data, disabled=True)
    pkg = {"filename": "C.D.1.var", "library_dir_id": None, "subpath": "", "storage_state": "disabled"}
    assert dirs.resolve_content_path(pkg) == str(vam.addon_packages / "C.D.1.var.disabled")
    pkg = {"filename": "Gone.1.var", "library_dir_id": None, "subpath": "", "storage_state": "disabled"}
    assert dirs.resolve_content_path(pkg) == str(vam.addon_packages / "Gone.1.var")
    assert dirs.pkg_var_path({"filename": "x", "library_dir_id": 999}) is None
    assert dirs.validate_new_aux_dir_path(str(vam.addon_packages)) is not None
    assert dirs.validate_new_aux_dir_path(str(vam.addon_packages / "nested")) is not None
    assert dirs.validate_new_aux_dir_path(str(vam.saves_dir / "scene")) is not None
    assert dirs.validate_new_aux_dir_path(str(vam.saves_dir / "PluginData" / "Offload")) is None
    assert dirs.validate_new_aux_dir_path(str(aux / "inner")) is not None
    assert dirs.validate_new_aux_dir_path(str(vam.root / "fresh")) is None
    assert dirs.same_filesystem_as_main(str(aux))
    db.close()
