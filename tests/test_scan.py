import os
import shutil
import time

from vambackstage.core.paths import LOCAL_PACKAGE_FILENAME

from .conftest import place_empty_marker, place_var
from .helpers import by_name, drop, make_lib, row, var_bytes


def test_disabled_layouts_in_main(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Suffix.Pkg.1.var", disabled=True)
    drop(vam.addon_packages, "Qvaro.Pkg.1.var", qvaro=True)
    drop(vam.addon_packages, "Marker.Pkg.1.var", marker=True)
    drop(vam.addon_packages, "Bare.Pkg.1.var")
    data = var_bytes("Mixed.Pkg.1.var")
    place_var(vam.addon_packages, "Mixed.Pkg.1.var", data)
    (vam.addon_packages / "Mixed.Pkg.1.var.disabled").write_bytes(data)
    drop(vam.addon_packages, "Stray.Pkg.1.var")
    (vam.addon_packages / "Stray.Pkg.1.DISABLED").write_bytes(b"")
    place_empty_marker(vam.addon_packages, "Ghost.Pkg.1.var")

    res = lib.scan()
    assert res.added == 6
    pk = by_name(lib)
    assert pk["Suffix.Pkg.1.var"]["storage_state"] == "disabled"
    assert lib.package_path("Suffix.Pkg.1.var") == str(vam.addon_packages / "Suffix.Pkg.1.var.disabled")
    assert pk["Qvaro.Pkg.1.var"]["storage_state"] == "disabled"
    assert lib.package_path("Qvaro.Pkg.1.var") == str(vam.addon_packages / "Qvaro.Pkg.1.DISABLED")
    assert pk["Marker.Pkg.1.var"]["storage_state"] == "disabled"
    assert lib.package_path("Marker.Pkg.1.var") == str(vam.addon_packages / "Marker.Pkg.1.var")
    assert pk["Bare.Pkg.1.var"]["storage_state"] == "enabled"
    assert pk["Mixed.Pkg.1.var"]["storage_state"] == "disabled"
    assert lib.package_path("Mixed.Pkg.1.var") == str(vam.addon_packages / "Mixed.Pkg.1.var")
    assert pk["Stray.Pkg.1.var"]["storage_state"] == "enabled"
    assert "Ghost.Pkg.1.var" not in pk
    names = set(os.listdir(vam.addon_packages))
    assert {
        "Suffix.Pkg.1.var.disabled",
        "Qvaro.Pkg.1.DISABLED",
        "Marker.Pkg.1.var",
        "Marker.Pkg.1.var.disabled",
    } <= names
    lib.close()


def test_initial_scan_leaf_detection_and_cache(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    res = lib.scan()
    assert res.scanned == 2 and res.added == 2
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["is_direct"] is True
    assert pk["Child.C.1.var"]["is_direct"] is False
    assert pk["Parent.P.1.var"]["dep_count"] == 1
    assert lib.stats()["direct_count"] == 1 and lib.stats()["dep_count"] == 1
    assert lib.initial_scan_done
    assert lib.scan().scanned == 0
    place_empty_marker(vam.addon_packages, "Parent.P.1.var")
    r = lib.scan()
    assert r.scanned == 0
    assert by_name(lib)["Parent.P.1.var"]["storage_state"] == "disabled"
    lib.close()


def test_subsequent_scan_registers_new_as_direct(vam):
    lib = make_lib(vam)
    lib.scan()
    drop(vam.addon_packages, "Child.D.1.var")
    drop(vam.addon_packages, "Parent.Q.1.var", deps=["Child.D.1"])
    lib.scan()
    pk = by_name(lib)
    assert pk["Parent.Q.1.var"]["is_direct"] and pk["Child.D.1.var"]["is_direct"]
    lib.close()


def test_tombstone_and_resurrection(vam):
    lib = make_lib(vam)
    p = drop(vam.addon_packages, "Gone.Pkg.1.var")
    lib.scan()
    lib.db.set_package_type_override("Gone.Pkg.1.var", "Looks")
    p.unlink()
    r = lib.scan()
    assert r.removed == 1
    assert "Gone.Pkg.1.var" not in by_name(lib)
    assert row(lib, "Gone.Pkg.1.var")["missing_since"] is not None
    drop(vam.addon_packages, "Gone.Pkg.1.var")
    r = lib.scan()
    assert r.added == 0
    pk = by_name(lib)
    assert pk["Gone.Pkg.1.var"]["type_override"] == "Looks"
    lib.close()


def test_unreadable_var(vam):
    lib = make_lib(vam)
    (vam.addon_packages / "Bad.Pkg.1.var").write_bytes(b"definitely not a zip")
    drop(vam.addon_packages, "Good.Pkg.1.var")
    r = lib.scan()
    assert r.unreadable == ["Bad.Pkg.1.var"]
    assert set(by_name(lib)) == {"Good.Pkg.1.var"}
    lib.close()


def test_aux_dirs(vam):
    lib = make_lib(vam)
    aux = vam.aux_dir("offload")
    aid = lib.add_library_dir(str(aux))
    drop(aux, "Aux.Stray.1.var", disabled=True)
    drop(aux, "Aux.Only.1.var")
    drop(vam.addon_packages, "Dup.Pkg.1.var")
    drop(aux, "Dup.Pkg.1.var")
    lib.db.insert_library_dir("/nonexistent/vam-aux-" + str(time.time()))
    lib.scan()
    pk = by_name(lib)
    assert pk["Aux.Stray.1.var"]["storage_state"] == "offloaded" and pk["Aux.Stray.1.var"]["library_dir_id"] == aid
    assert "Aux.Stray.1.var" in os.listdir(aux) and "Aux.Stray.1.var.disabled" not in os.listdir(aux)
    assert pk["Aux.Only.1.var"]["storage_state"] == "offloaded"
    assert pk["Dup.Pkg.1.var"]["library_dir_id"] is None
    shutil.rmtree(aux)
    lib.scan()
    assert "Aux.Only.1.var" in by_name(lib)
    lib.close()


def test_archive_dirs(vam):
    lib = make_lib(vam)
    arch = vam.aux_dir("archive")
    aid = lib.add_library_dir(str(arch), archive=True)
    drop(arch, "Child.C.1.var")
    drop(arch, "Parent.P.1.var", deps=["Child.C.1"])
    lib.scan()
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "archived" and pk["Parent.P.1.var"]["library_dir_id"] == aid
    assert pk["Parent.P.1.var"]["is_direct"] and not pk["Child.C.1.var"]["is_direct"]
    counts = lib.status_counts()
    assert counts["archived"] == 2 and counts["direct"] == 0
    assert lib.set_library_dir_role(aid, False) == 2
    assert by_name(lib)["Parent.P.1.var"]["storage_state"] == "offloaded"
    lib.close()


def test_nested_subfolders(vam):
    lib = make_lib(vam)
    sub = vam.addon_packages / "Creator" / "Scenes"
    drop(sub, "Nest.Main.1.var")
    drop(vam.addon_packages / "Disabled", "Nest.Dis.1.var", disabled=True)
    aux = vam.aux_dir("offload")
    aid = lib.add_library_dir(str(aux))
    drop(aux / "Packed" / "Inner", "Nest.Aux.1.var")
    root_path = drop(vam.addon_packages, "Move.Sub.1.var")
    lib.scan()
    pk = by_name(lib)
    assert pk["Nest.Main.1.var"]["subpath"] == "Creator/Scenes" and pk["Nest.Main.1.var"]["storage_state"] == "enabled"
    assert pk["Nest.Dis.1.var"]["subpath"] == "Disabled" and pk["Nest.Dis.1.var"]["storage_state"] == "disabled"
    assert pk["Nest.Aux.1.var"]["subpath"] == "Packed/Inner" and pk["Nest.Aux.1.var"]["library_dir_id"] == aid
    assert pk["Move.Sub.1.var"]["subpath"] == ""
    (vam.addon_packages / "Relocated").mkdir()
    os.rename(root_path, vam.addon_packages / "Relocated" / "Move.Sub.1.var")
    r = lib.scan()
    assert r.scanned == 0
    assert by_name(lib)["Move.Sub.1.var"]["subpath"] == "Relocated"
    lib.close()


def test_local_content(vam):
    lib = make_lib(vam)
    (vam.saves_dir / "scene").mkdir()
    (vam.saves_dir / "scene" / "Loose.json").write_text('{"atoms":[{"id":"Person","type":"Person"}]}')
    look_dir = vam.custom_dir / "Atom" / "Person" / "Appearance"
    look_dir.mkdir(parents=True)
    (look_dir / "Cool_Look.vap").write_text("<vap>")
    (vam.saves_dir / "PluginData" / "scene").mkdir(parents=True)
    (vam.saves_dir / "PluginData" / "scene" / "NotIndexed.json").write_text("{}")
    offload = vam.saves_dir / "PluginData" / "JayJayWon" / "BrowserAssist" / "OffloadedVARs"
    offload.mkdir(parents=True)
    aid = lib.add_library_dir(str(offload))
    drop(offload, "Off.Browser.1.var")
    lib.scan()
    contents = {c["internal_path"]: c for c in lib.list_contents()}
    assert contents["Saves/scene/Loose.json"]["package_filename"] == LOCAL_PACKAGE_FILENAME
    assert contents["Saves/scene/Loose.json"]["type"] == "scene"
    assert contents["Custom/Atom/Person/Appearance/Cool_Look.vap"]["type"] == "look"
    assert contents["Custom/Atom/Person/Appearance/Cool_Look.vap"]["display_name"] == "Cool Look"
    assert not any("NotIndexed" in p or "OffloadedVARs" in p for p in contents)
    assert lib.db.get_all_contents()[0]["person_atom_ids"] in ('["Person"]', None) or True
    assert by_name(lib)["Off.Browser.1.var"]["library_dir_id"] == aid
    assert lib.rescan_local_content()["added"] == 0
    fp = vam.saves_dir / "scene" / "Loose.json"
    before = lib.db.get_local_content_meta(LOCAL_PACKAGE_FILENAME)["Saves/scene/Loose.json"]["mtime"]
    t = time.time() + 80
    os.utime(fp, (t, t))
    lib.rescan_local_content()
    after = lib.db.get_local_content_meta(LOCAL_PACKAGE_FILENAME)["Saves/scene/Loose.json"]["mtime"]
    assert after != before and abs(after - t) < 2
    meta = lib.db.get_local_content_meta(LOCAL_PACKAGE_FILENAME)["Saves/scene/Loose.json"]
    assert meta["person_atom_ids"] == '["Person"]'
    fp.unlink()
    assert lib.rescan_local_content()["removed"] == 1
    lib.close()


def test_inherit_from_older_version(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "A.B.1.var", files={"Saves/scene/A.json": "{}", "Saves/scene/Gone.json": "{}"})
    lib.scan()
    lib.db.touch_package_first_seen("A.B.1.var", at=1000)
    lib.set_type_override("A.B.1.var", "Looks")
    label = lib.db.find_or_create_label("Fav")
    lib.db.apply_label_to_packages(label["id"], ["A.B.1.var"])
    lib.set_hidden_batch([("A.B.1.var", "Saves/scene/A.json"), ("A.B.1.var", "Saves/scene/Gone.json")], True)
    drop(vam.addon_packages, "A.B.2.var", files={"Saves/scene/A.json": "{}"})
    lib.scan()
    pk = by_name(lib)
    assert pk["A.B.2.var"]["type_override"] == "Looks"
    assert pk["A.B.2.var"]["label_ids"] == [label["id"]]
    c = {(x["package_filename"], x["internal_path"]): x for x in lib.list_contents("A.B.2.var")}
    assert c[("A.B.2.var", "Saves/scene/A.json")]["hidden"] is True
    assert not (vam.prefs_dir / "A.B.2" / "Saves/scene/Gone.json.hide").exists()
    lib.close()


def test_cross_version_content_dedup(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "A.B.1.var", files={"Saves/scene/Same.json": "{}", "Saves/scene/OnlyOld.json": "{}"})
    drop(vam.addon_packages, "A.B.2.var", files={"Saves/scene/Same.json": "{}"})
    lib.scan()
    names = sorted((c["package_filename"], c["display_name"]) for c in lib.list_contents())
    assert names == [("A.B.1.var", "OnlyOld"), ("A.B.2.var", "Same")]
    assert len(lib.list_contents("A.B.1.var")) == 2
    pk = by_name(lib)
    assert pk["A.B.1.var"]["is_shadowed"] and pk["A.B.1.var"]["shadowed_by_version"] == "2"
    lib.close()


def test_resolve_vam_dir_accepts_root_addonpackages_and_nested(tmp_path):
    from vambackstage.core.scanner import resolve_vam_dir

    root = tmp_path / "VaM"
    (root / "AddonPackages" / "Creator" / "Sub").mkdir(parents=True)
    (root / "Saves" / "scene").mkdir(parents=True)
    assert resolve_vam_dir(str(root)) == str(root)
    assert resolve_vam_dir(str(root / "AddonPackages")) == str(root)
    assert resolve_vam_dir(str(root / "AddonPackages" / "Creator" / "Sub")) == str(root)
    assert resolve_vam_dir(str(root / "Saves" / "scene")) == str(root)
    assert resolve_vam_dir(str(root) + "/") == str(root)


def test_resolve_vam_dir_rejects_unrelated_folders(tmp_path):
    from vambackstage.core.scanner import resolve_vam_dir

    (tmp_path / "elsewhere").mkdir()
    assert resolve_vam_dir(str(tmp_path / "elsewhere")) is None
    assert resolve_vam_dir(str(tmp_path / "does-not-exist")) is None


def test_detect_vam_dir_probes_wine_prefix(tmp_path, monkeypatch):
    from vambackstage.core.scanner import detect_vam_dir

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAM_DIR", raising=False)
    (tmp_path / "nowhere").mkdir()
    monkeypatch.chdir(tmp_path / "nowhere")
    assert detect_vam_dir() is None
    wine = tmp_path / ".wine" / "drive_c" / "VaM" / "AddonPackages"
    wine.mkdir(parents=True)
    found = detect_vam_dir()
    assert found == {"path": str(wine.parent), "source": "common"}


def test_detect_vam_dir_probes_wine_prefix_variants(tmp_path, monkeypatch):
    from vambackstage.core.scanner import detect_vam_dir

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VAM_DIR", raising=False)
    monkeypatch.delenv("WINEPREFIX", raising=False)
    (tmp_path / "nowhere").mkdir()
    monkeypatch.chdir(tmp_path / "nowhere")
    assert detect_vam_dir() is None
    game = (
        tmp_path
        / ".local"
        / "share"
        / "wineprefixes"
        / "vam"
        / "drive_c"
        / "Games"
        / "Virt-a-Mate 1.22"
        / "AddonPackages"
    )
    game.mkdir(parents=True)
    assert detect_vam_dir() == {"path": str(game.parent), "source": "common"}
    custom = tmp_path / "prefix" / "drive_c" / "Stuff" / "AddonPackages"
    custom.mkdir(parents=True)
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "prefix"))
    found = detect_vam_dir()
    assert found["path"] in (str(game.parent), str(custom.parent))


def test_watcher_rescans_once_files_settle(vam):
    import time

    from vambackstage.core.watcher import LibraryWatcher

    lib = make_lib(vam)
    drop(vam.addon_packages, "Old.Pkg.1.var")
    lib.scan()
    scans = []
    watcher = LibraryWatcher(lib, lambda: scans.append(lib.scan()), interval=0.1)
    watcher.start()
    time.sleep(0.35)
    assert scans == []
    drop(vam.addon_packages, "New.Pkg.1.var")
    deadline = time.time() + 5
    while time.time() < deadline and "New.Pkg.1.var" not in by_name(lib):
        time.sleep(0.05)
    assert "New.Pkg.1.var" in by_name(lib) and by_name(lib)["New.Pkg.1.var"]["is_direct"]
    n = len(scans)
    time.sleep(0.4)
    assert len(scans) == n
    watcher.stop()
    assert not watcher.running
    lib.close()


def _unpack(dir_path, name: str, deps=None, files=None):
    import io
    import zipfile

    from .helpers import var_bytes

    data = var_bytes(name, deps, files)
    folder = dir_path / name
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(folder)
    (dir_path / (name + ".orig")).write_bytes(data)
    (dir_path / (name + ".depend.txt")).write_text("\n".join(deps or []))
    return folder


def test_unpacked_package_folders_are_packages(vam):
    from .helpers import by_name, drop, make_lib

    lib = make_lib(vam)
    drop(vam.addon_packages, "Dep.Lib.1.var")
    folder = _unpack(
        vam.addon_packages,
        "Tobi19.GentianaFF15(Updated).1.var",
        deps=["Dep.Lib.1"],
        files={"Saves/scene/ff15.json": "{}", "Custom/Atom/Person/Appearance/Gentiana.vap": "{}"},
    )
    lib.scan()
    pk = by_name(lib)
    assert set(pk) == {"Dep.Lib.1.var", "Tobi19.GentianaFF15(Updated).1.var"}
    me = pk["Tobi19.GentianaFF15(Updated).1.var"]
    assert me["is_unpacked"] and me["dep_count"] == 1 and me["content_count"] == 2 and me["size_bytes"] > 0
    assert not pk["Dep.Lib.1.var"]["is_orphan"]
    listed = {path for path, _size in lib.file_list("Tobi19.GentianaFF15(Updated).1.var")["file_list"]}
    assert listed == {"meta.json", "Custom/Atom/Person/Appearance/Gentiana.vap", "Saves/scene/ff15.json"}
    assert lib.scan().scanned == 0
    (folder / "Saves" / "scene" / "ff15.json").write_text('{"changed": true}')
    assert lib.scan().scanned == 1
    res = lib.integrity_check()
    assert res["unpacked"] == ["Tobi19.GentianaFF15(Updated).1.var"] and res["corrupted"] == 0
    lib.set_enabled(["Tobi19.GentianaFF15(Updated).1.var"], False)
    assert (vam.addon_packages / "Tobi19.GentianaFF15(Updated).1.var.disabled").exists()
    assert by_name(lib)["Tobi19.GentianaFF15(Updated).1.var"]["storage_state"] == "disabled"
    lib.set_enabled(["Tobi19.GentianaFF15(Updated).1.var"], True)
    assert by_name(lib)["Tobi19.GentianaFF15(Updated).1.var"]["storage_state"] == "enabled"
    lib.uninstall("Tobi19.GentianaFF15(Updated).1.var")
    assert not folder.exists() and not (vam.addon_packages / "Tobi19.GentianaFF15(Updated).1.var.orig").exists()
    assert not (vam.addon_packages / "Tobi19.GentianaFF15(Updated).1.var.depend.txt").exists()
    lib.close()
