import os

import pytest

from vambackstage.core.storage_state import StorageStateError

from .helpers import by_name, drop, make_lib, row


def marker(vam, name, sub=""):
    return vam.addon_packages / sub / (name + ".disabled")


def test_toggle_enabled_with_cascade(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Shared.S.1.var")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1", "Shared.S.1"])
    drop(vam.addon_packages, "Other.O.1.var", deps=["Shared.S.1"])
    lib.scan()
    events = []
    lib.events.subscribe("packages:updated", lambda ch, p: events.append(ch))
    res = lib.toggle_enabled("Parent.P.1.var")
    assert res[0]["ok"] and res[0]["storage_state"] == "disabled" and res[0]["cascade_count"] == 1
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "disabled"
    assert pk["Child.C.1.var"]["storage_state"] == "disabled"
    assert pk["Shared.S.1.var"]["storage_state"] == "enabled"
    assert marker(vam, "Parent.P.1.var").exists() and marker(vam, "Parent.P.1.var").stat().st_size == 0
    assert marker(vam, "Child.C.1.var").exists()
    assert (vam.addon_packages / "Parent.P.1.var").exists()
    assert events == ["packages:updated"]
    assert lib.status_counts()["disabled"] == 2
    assert lib.stats()["broken_count"] == 0
    res = lib.toggle_enabled(["Parent.P.1.var"])
    assert res[0]["storage_state"] == "enabled" and res[0]["cascade_count"] == 1
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "enabled" and pk["Child.C.1.var"]["storage_state"] == "enabled"
    assert not marker(vam, "Parent.P.1.var").exists() and not marker(vam, "Child.C.1.var").exists()
    assert lib.set_enabled("Parent.P.1.var", True)[0]["unchanged"] is True
    lib.close()


def test_enable_legacy_suffix_and_qvaro(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Suffix.Pkg.1.var", disabled=True)
    drop(vam.addon_packages, "Qvaro.Pkg.1.var", qvaro=True)
    lib.scan()
    lib.set_enabled(["Suffix.Pkg.1.var", "Qvaro.Pkg.1.var"], True)
    names = set(os.listdir(vam.addon_packages))
    assert names == {"Suffix.Pkg.1.var", "Qvaro.Pkg.1.var"}
    pk = by_name(lib)
    assert pk["Suffix.Pkg.1.var"]["storage_state"] == "enabled" and pk["Qvaro.Pkg.1.var"]["storage_state"] == "enabled"
    lib.close()


def test_disable_behavior_move_to_aux_and_nested(vam):
    lib = make_lib(vam)
    sub = vam.addon_packages / "Creator"
    drop(sub, "Child.C.1.var")
    drop(sub, "Parent.P.1.var", deps=["Child.C.1"])
    aux = vam.aux_dir("offload")
    aid = lib.add_library_dir(str(aux))
    lib.set_setting("disable_behavior", f"move-to:{aid}")
    lib.scan()
    lib.toggle_enabled("Parent.P.1.var")
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "offloaded" and pk["Parent.P.1.var"]["library_dir_id"] == aid
    assert pk["Child.C.1.var"]["storage_state"] == "offloaded"
    assert (aux / "Creator" / "Parent.P.1.var").exists() and (aux / "Creator" / "Child.C.1.var").exists()
    assert not (sub / "Parent.P.1.var").exists()
    assert pk["Parent.P.1.var"]["subpath"] == "Creator"
    lib.toggle_enabled("Parent.P.1.var")
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "enabled" and pk["Parent.P.1.var"]["library_dir_id"] is None
    assert (sub / "Parent.P.1.var").exists() and (sub / "Child.C.1.var").exists()
    lib.remove_library_dir(aid)
    assert lib.get_setting("disable_behavior") == "suffix"
    lib.close()


def test_uninstall_orphans_and_replaceability(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    lib.scan()
    detail = lib.package_detail("Parent.P.1.var")
    assert [d["filename"] for d in detail["removable_deps"]] == ["Child.C.1.var"]
    assert detail["removable_size"] > 0
    res = lib.uninstall("Parent.P.1.var")
    assert res["results"][0]["deleted"] == 1
    assert not (vam.addon_packages / "Parent.P.1.var").exists()
    assert (vam.addon_packages / "Child.C.1.var").exists()
    pk = by_name(lib)
    assert pk["Child.C.1.var"]["is_orphan"] is True
    assert lib.status_counts()["orphan"] == 1
    r = lib.remove_orphans()
    assert r["count"] == 1 and r["freed_bytes"] > 0
    assert not (vam.addon_packages / "Child.C.1.var").exists()
    assert by_name(lib) == {}
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    lib.scan()
    lib.demote("Child.C.1.var")
    lib.store.set_hub_filename_index({"Child.C.1.var": 1, "Parent.P.1.var": 2})
    lib.store.build_from_db()
    assert by_name(lib)["Child.C.1.var"]["is_hub_replaceable"]
    res = lib.uninstall("Parent.P.1.var")
    assert res["results"][0]["deleted"] == 2
    assert by_name(lib) == {}
    lib.close()


def test_uninstall_demotes_when_dependents_remain(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    lib.scan()
    lib.promote("Child.C.1.var")
    assert by_name(lib)["Child.C.1.var"]["is_direct"]
    res = lib.uninstall("Child.C.1.var")
    assert res["results"][0]["demoted"] is True
    pk = by_name(lib)
    assert pk["Child.C.1.var"]["is_direct"] is False and (vam.addon_packages / "Child.C.1.var").exists()
    with pytest.raises(KeyError):
        lib.uninstall("Nope.1.var")
    res = lib.uninstall("Child.C.1.var")
    r = res["results"][0]
    assert r["demoted"] and r["already_dependency"] and r["needed_by"] == ["Parent.P.1.var"]
    assert (vam.addon_packages / "Child.C.1.var").exists()
    assert by_name(lib)["Parent.P.1.var"]["missing_deps"] == 0
    lib.force_remove(["Child.C.1.var"])
    assert by_name(lib)["Parent.P.1.var"]["missing_deps"]
    assert lib.status_counts()["broken"] == 1
    lib.close()


def test_promote_demote_auto_hide_sidecars(vam):
    lib = make_lib(vam)
    lib.set_setting("auto_hide_deps", "1")
    drop(vam.addon_packages, "Child.C.1.var", files={"Saves/scene/c.json": "{}", "Custom/Hair/x.vam": "h"})
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    lib.scan()
    hide = vam.prefs_dir / "Child.C.1" / "Saves/scene/c.json.hide"
    assert not hide.exists()
    lib.apply_auto_hide_rule("deps")
    assert hide.exists() and (vam.prefs_dir / "Child.C.1" / "Custom/Hair/x.vam.hide").exists()
    assert all(c["hidden"] for c in lib.list_contents("Child.C.1.var"))
    assert lib.content_visibility_counts()["hidden"] == 2
    lib.promote("Child.C.1.var")
    assert not hide.exists()
    assert not any(c["hidden"] for c in lib.list_contents("Child.C.1.var"))
    assert by_name(lib)["Child.C.1.var"]["is_direct"]
    lib.demote("Child.C.1.var")
    assert hide.exists()
    lib.promote("Child.C.1.var")
    lib.set_setting("auto_hide_foreign_hair", "1")
    lib.apply_auto_hide_rule("foreign_hair")
    assert (vam.prefs_dir / "Child.C.1" / "Custom/Hair/x.vam.hide").exists() and not hide.exists()
    lib.set_setting("auto_hide_foreign_hair", "0")
    lib.remove_auto_hide_rule("foreign_hair")
    assert not (vam.prefs_dir / "Child.C.1" / "Custom/Hair/x.vam.hide").exists()
    lib.close()


def test_content_flags(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "A.B.1.var", files={"Saves/scene/s.json": "{}", "Saves/scene/t.json": "{}"})
    lib.scan()
    assert lib.toggle_hidden("A.B.1.var", "Saves/scene/s.json") is True
    assert (vam.prefs_dir / "A.B.1" / "Saves/scene/s.json.hide").exists()
    assert lib.toggle_favorite("A.B.1.var", "Saves/scene/s.json") is True
    assert by_name(lib)["A.B.1.var"]["favorite_content_count"] == 1
    assert lib.toggle_hidden("A.B.1.var", "Saves/scene/s.json") is False
    lib.set_favorite_batch([("A.B.1.var", "Saves/scene/t.json")], True)
    assert lib.content_visibility_counts() == {"all": 2, "visible": 2, "hidden": 0, "favorites": 2}
    with pytest.raises(KeyError):
        lib.toggle_hidden("A.B.1.var", "nope")
    lib.close()


def test_archive_store_and_install_from_archive(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    arch = vam.aux_dir("archive")
    aid = lib.add_library_dir(str(arch), archive=True)
    lib.scan()
    preview = lib.archive_preview("Parent.P.1.var")
    assert preview["archive_count"] == 1 and preview["catalog_unavailable"]
    assert preview["store"]["store_count"] == 1 and preview["prune"]["delete_count"] == 0
    res = lib.archive("Parent.P.1.var", dep_mode="store")
    assert res["archived"] == 1 and res["stored_to_archive"] == 1
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "archived" and pk["Child.C.1.var"]["storage_state"] == "archived"
    assert (arch / "Parent.P.1.var").exists() and (arch / "Child.C.1.var").exists()
    assert lib.stats()["missing_dep_count"] == 0 and lib.status_counts()["archived"] == 2
    res = lib.install_from_archive("Parent.P.1.var")
    assert res["count"] == 1
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["storage_state"] == "enabled" and pk["Parent.P.1.var"]["is_direct"]
    assert pk["Child.C.1.var"]["storage_state"] == "enabled" and not pk["Child.C.1.var"]["is_direct"]
    assert (vam.addon_packages / "Parent.P.1.var").exists() and (vam.addon_packages / "Child.C.1.var").exists()
    lib.store.set_hub_filename_index({"Child.C.1.var": 1})
    lib.store.build_from_db()
    assert lib.archive_preview("Parent.P.1.var")["prune"]["delete_count"] == 1
    res = lib.archive("Parent.P.1.var", archive_dir_id=aid, dep_mode="prune")
    assert res["pruned"] == 1
    assert "Child.C.1.var" not in by_name(lib) and not (vam.addon_packages / "Child.C.1.var").exists()
    lib.close()


def test_library_dir_management(vam):
    lib = make_lib(vam)
    with pytest.raises(ValueError):
        lib.add_library_dir(str(vam.addon_packages))
    with pytest.raises(ValueError):
        lib.add_library_dir(str(vam.root / "missing"))
    aux = vam.aux_dir("offload")
    aid = lib.add_library_dir(str(aux))
    with pytest.raises(ValueError):
        lib.add_library_dir(str(aux))
    drop(aux, "Off.Pkg.1.var")
    lib.scan()
    listing = lib.list_library_dirs()
    assert listing["main"]["package_count"] == 0
    assert listing["aux"][0]["package_count"] == 1 and listing["aux"][0]["exists"]
    with pytest.raises(ValueError):
        lib.remove_library_dir(aid)
    assert lib.remove_library_dir(aid, force=True)["tombstoned"] == 1
    assert by_name(lib) == {} and lib.list_library_dirs()["aux"] == []
    lib.close()


def test_integrity_check_and_file_list(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Good.Pkg.1.var")
    p = drop(vam.addon_packages, "Bad.Pkg.1.var")
    lib.scan()
    data = bytearray(p.read_bytes())
    data[45] ^= 0xFF
    data[46] ^= 0xFF
    p.write_bytes(bytes(data))
    progress = []
    res = lib.integrity_check(progress.append)
    assert res["checked"] == 2 and res["total"] == 2 and res["corrupted_files"] == ["Bad.Pkg.1.var"]
    assert "CRC" in res["errors"]["Bad.Pkg.1.var"] or "Bad" in res["errors"]["Bad.Pkg.1.var"]
    assert res["bytes"] > 0 and res["seconds"] >= 0 and res["cancelled"] is False
    assert progress[-1]["status"] == "done" and progress[-1]["step"] == 2
    pk = by_name(lib)
    assert pk["Bad.Pkg.1.var"]["is_corrupted"] and not pk["Good.Pkg.1.var"]["is_corrupted"]
    assert lib.status_counts()["broken"] == 1
    import threading

    stop = threading.Event()
    stop.set()
    res2 = lib.integrity_check(cancel=stop)
    assert res2["cancelled"] and res2["checked"] == 0 and res2["total"] == 2
    assert by_name(lib)["Bad.Pkg.1.var"]["is_corrupted"]
    drop(vam.addon_packages, "Bad.Pkg.1.var")
    assert lib.integrity_check()["corrupted"] == 0
    assert not by_name(lib)["Bad.Pkg.1.var"]["is_corrupted"]
    fl = lib.file_list("Good.Pkg.1.var")
    assert ("meta.json", fl["file_list"][0][1]) == fl["file_list"][0]
    with pytest.raises(KeyError):
        lib.file_list("Nope.1.var")
    lib.close()


def test_type_override_and_mark_recent(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "A.B.1.var")
    lib.scan()
    lib.set_type_override("A.B.1.var", "Looks")
    assert by_name(lib)["A.B.1.var"]["type"] == "Looks" and row(lib, "A.B.1.var")["type_override"] == "Looks"
    lib.set_type_override(["A.B.1.var"], None)
    assert by_name(lib)["A.B.1.var"]["type"] == "Scenes"
    with pytest.raises(ValueError):
        lib.set_type_override("A.B.1.var", "Bogus")
    lib.db.touch_package_first_seen("A.B.1.var", at=1000)
    lib.store.build_from_db()
    lib.mark_recent("A.B.1.var")
    assert by_name(lib)["A.B.1.var"]["first_seen_at"] > 1000
    assert lib.scan().scanned == 0
    lib.close()


def test_force_remove_and_missing_deps(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Missing.M.3", "Child.C.latest"])
    drop(vam.addon_packages, "Child.C.1.var")
    drop(vam.addon_packages, "Old.O.1.var", deps=["Child.C.5"])
    lib.scan()
    missing = {m["ref"]: m for m in lib.missing_deps()}
    assert set(missing) == {"Missing.M.3", "Child.C.5"}
    assert missing["Child.C.5"]["is_fallback"] and missing["Child.C.5"]["fallback_version"] == "1"
    assert missing["Missing.M.3"]["needed_by"][0]["filename"] == "Parent.P.1.var"
    enriched = lib.missing_deps(
        {"Child.C": {"filename": "Child.C.7.var", "resource_id": 9, "version": 7}}, {"Missing.M.3.var": 4}
    )
    e = {m["ref"]: m for m in enriched}
    assert e["Missing.M.3"]["hub"]["is_exact"] and e["Missing.M.3"]["hub"]["resource_id"] == "4"
    assert e["Child.C.5"]["hub"]["hub_version"] == 7 and not e["Child.C.5"]["hub"]["installed_locally"]
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["missing_deps"] == 1 and lib.stats()["missing_dep_count"] == 2
    assert pk["Old.O.1.var"]["missing_deps"] == 1
    assert (
        lib.status_counts()["missing_unique"] == 2
        and lib.status_counts()["broken"] == 2
        and lib.status_counts()["orphan"] == 0
    )
    assert lib.package_detail("Child.C.1.var")["dependents"]
    detail = lib.package_detail("Parent.P.1.var")
    assert {d["ref"]: d["resolution"] for d in detail["deps"]} == {"Missing.M.3": "missing", "Child.C.latest": "latest"}
    lib.set_strict_versions(False)
    pk = by_name(lib)
    assert pk["Old.O.1.var"]["missing_deps"] == 0 and lib.stats()["missing_dep_count"] == 1
    assert (
        lib.status_counts()["missing_unique"] == 1
        and lib.status_counts()["broken"] == 1
        and lib.status_counts()["orphan"] == 0
    )
    assert {m["ref"] for m in lib.missing_deps()} == {"Missing.M.3"}
    assert lib.get_setting("strict_versions") == "0"
    lib.set_strict_versions(True)
    assert by_name(lib)["Old.O.1.var"]["missing_deps"] == 1
    lib.force_remove(["Parent.P.1.var", "Old.O.1.var"])
    assert set(by_name(lib)) == {"Child.C.1.var"}
    lib.close()


def test_integrate_new_var_lands_at_dependents_tier(vam):
    lib = make_lib(vam)
    lib.set_setting("auto_hide_deps", "1")
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Child.C.1"])
    lib.scan()
    lib.toggle_enabled("Parent.P.1.var")
    p = drop(vam.addon_packages, "Child.C.1.var")
    res = lib.integrate_new_var(str(p), is_direct=False)
    assert res["landed"] == "disabled"
    pk = by_name(lib)
    assert pk["Child.C.1.var"]["storage_state"] == "disabled" and not pk["Child.C.1.var"]["is_direct"]
    assert marker(vam, "Child.C.1.var").exists()
    assert all(c["hidden"] for c in lib.list_contents("Child.C.1.var"))
    assert pk["Parent.P.1.var"]["missing_deps"] == 0
    lib.close()


def test_apply_storage_state_guards(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "A.B.1.var")
    lib.scan()
    from vambackstage.core.storage_state import StorageTarget, apply_storage_state

    with pytest.raises(StorageStateError):
        apply_storage_state(lib, "A.B.1.var", StorageTarget("offloaded", None))
    with pytest.raises(StorageStateError):
        apply_storage_state(lib, "A.B.1.var", StorageTarget("enabled", 5))
    with pytest.raises(StorageStateError):
        apply_storage_state(lib, "Nope.1.var", StorageTarget("disabled", None))
    marker(vam, "A.B.1.var").write_bytes(b"real bytes")
    with pytest.raises(StorageStateError):
        apply_storage_state(lib, "A.B.1.var", StorageTarget("disabled", None))
    lib.close()


def test_create_dummies_satisfies_missing_refs(vam):
    from vambackstage.core.varfile import read_var, verify_package_full

    lib = make_lib(vam)
    drop(
        vam.addon_packages,
        "Parent.P.1.var",
        deps=["Gone.Pkg.3", "Later.Thing.latest", "Atleast.Min.min4", "Other.Have.2"],
    )
    drop(vam.addon_packages, "Other.Have.5.var")
    lib.scan()
    assert by_name(lib)["Parent.P.1.var"]["missing_deps"] == 4
    res = lib.create_dummies(["Gone.Pkg.3", "Later.Thing.latest", "Atleast.Min.min4", "Other.Have.2", "nonsense"])
    assert res["created"] == ["Gone.Pkg.3.var", "Later.Thing.1.var", "Atleast.Min.4.var"]
    reasons = dict(res["skipped"])
    assert "shadow" in reasons["Other.Have.2"] and "nonsense" in reasons
    folder = vam.addon_packages / "Dummies"
    for fn in res["created"]:
        verify_package_full(folder / fn)
        meta = read_var(str(folder / fn)).meta
        assert meta["vamBackstageDummy"] is True and meta["contentList"] == [] and meta["dependencies"] == {}
        assert meta["creatorName"] == fn.split(".")[0] and meta["packageName"] == fn.split(".")[1]
    pk = by_name(lib)
    assert pk["Parent.P.1.var"]["missing_deps"] == 1
    d = pk["Gone.Pkg.3.var"]
    assert d["is_dummy"] and not d["is_direct"] and d["storage_state"] == "enabled" and d["subpath"]
    assert not pk["Parent.P.1.var"]["is_dummy"]
    assert lib.create_dummies(["Gone.Pkg.3"])["skipped"] == [("Gone.Pkg.3", "already installed")]
    lib.scan()
    assert by_name(lib)["Later.Thing.1.var"]["is_dummy"]
    lib.force_remove(["Parent.P.1.var"])
    assert lib.status_counts()["orphan"] == 4
    lib.remove_orphans()
    assert not (folder / "Gone.Pkg.3.var").exists() and "Gone.Pkg.3.var" not in by_name(lib)
    lib.close()


def test_loose_scene_references_are_dependencies(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Acid.Timeline.5.var")
    drop(vam.addon_packages, "Other.Dep.1.var")
    lib.scan()
    lib.demote(["Acid.Timeline.5.var", "Other.Dep.1.var"])
    assert lib.status_counts()["orphan"] == 2
    scene_dir = vam.saves_dir / "scene"
    scene_dir.mkdir(parents=True, exist_ok=True)
    scene = scene_dir / "my scene.json"
    scene.write_text(
        '{"atoms":[{"storables":[{"id":"plugin#0","plugins":{"plugin#0":"Acid.Timeline.5:/Custom/Scripts/Timeline.cslist"}}]}],'
        '"look":"Missing.Look.LATEST:/Custom/Atom/Person/Appearance/x.vap","self":"SELF:/Custom/foo.json","v":"Acid.Timeline.5:/again"}'
    )
    lib.scan()
    pk = by_name(lib)
    assert not pk["Acid.Timeline.5.var"]["is_orphan"] and pk["Other.Dep.1.var"]["is_orphan"]
    assert lib.status_counts()["orphan"] == 1
    missing = {m["ref"]: m for m in lib.missing_deps()}
    assert list(missing) == ["Missing.Look.latest"]
    assert missing["Missing.Look.latest"]["needed_by"] == [{"filename": "__local__", "name": "Local content"}]
    assert lib.stats()["missing_dep_count"] == 1 and lib.status_counts()["missing_unique"] == 1
    detail = lib.package_detail("Acid.Timeline.5.var")
    assert detail["dependents"][0]["filename"] == "__local__" and "Local content" in detail["dependents"][0]["display"]
    local = lib.local_demand_summary()
    assert local["is_local"] and local["missing_deps"] == 1 and local["content_count"] == 1 and local["is_direct"]
    ldetail = lib.package_detail("__local__")
    assert ldetail["is_local"] and ldetail["missing_deps_total"] == 1 and ldetail["dependents"] == []
    assert {n["ref"] for n in ldetail["deps"]} == {"Acid.Timeline.5", "Missing.Look.latest"}
    assert ldetail["files_missing"] == [{"path": "Saves/scene/my scene.json", "missing": ["Missing.Look.latest"]}]
    item = next(c for c in lib.list_contents("__local__") if c["internal_path"] == "Saves/scene/my scene.json")
    assert item["missing_refs"] == ["Missing.Look.latest"]
    assert {n["ref"]: n["missing"] for n in item["refs"]} == {"Acid.Timeline.5": False, "Missing.Look.latest": True}
    assert lib.missing_deps()[0]["local_files"] == ["Saves/scene/my scene.json"]
    assert lib.db.get_local_refs_meta()["Saves/scene/my scene.json"]["refs"]
    scene.write_text('{"nothing": "SELF:/x"}')
    lib.scan()
    assert by_name(lib)["Acid.Timeline.5.var"]["is_orphan"] and lib.missing_deps() == []
    assert lib.local_demand_summary() is None
    scene.unlink()
    lib.scan()
    assert "Saves/scene/my scene.json" not in lib.db.get_local_refs_meta()
    lib.close()


def test_shadowing_dummy_keeps_installed_version_needed_and_alias_links_it(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Scene.Needs.1.var", deps=["Acid.CUA.10"])
    real = drop(vam.addon_packages, "Acid.CUA.12.var", files={"Custom/Scripts/cua.cs": "real"})
    lib.scan()
    lib.demote("Acid.CUA.12.var")
    pk = by_name(lib)
    assert pk["Scene.Needs.1.var"]["missing_deps"] == 1 and not pk["Acid.CUA.12.var"]["is_orphan"]
    assert lib.create_dummies(["Acid.CUA.10"])["created"] == []
    res = lib.create_dummies(["Acid.CUA.10"], allow_shadow=True)
    assert res["created"] == ["Acid.CUA.10.var"]
    pk = by_name(lib)
    assert pk["Scene.Needs.1.var"]["missing_deps"] == 0
    assert not pk["Acid.CUA.12.var"]["is_orphan"] and pk["Acid.CUA.10.var"]["dep_count"] == 1
    assert lib.package_detail("Acid.CUA.12.var")["dependents"][0]["filename"] == "Acid.CUA.10.var"
    lib.force_remove(["Acid.CUA.10.var"])
    res = lib.create_aliases(["Acid.CUA.10", "Nope.Missing.3", "Acid.CUA.10"])
    assert [c["filename"] for c in res["created"]] == ["Acid.CUA.10.var"] and res["created"][0][
        "source"
    ] == "Acid.CUA.12.var"
    assert dict(res["skipped"]) == {
        "Nope.Missing.3": "no version of this package is installed to alias",
        "Acid.CUA.10": "already installed",
    }
    alias = vam.addon_packages / "Aliases" / "Acid.CUA.10.var"
    assert alias.read_bytes() == real.read_bytes() and (
        not res["created"][0]["linked"] or alias.stat().st_ino == real.stat().st_ino
    )
    pk = by_name(lib)
    assert (
        pk["Scene.Needs.1.var"]["missing_deps"] == 0
        and pk["Acid.CUA.10.var"]["content_count"] == pk["Acid.CUA.12.var"]["content_count"]
    )
    assert lib.package_detail("Scene.Needs.1.var")["deps"][0]["resolution"] == "exact"
    lib.close()


def test_rescan_keeps_demoted_packages_and_orphans(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Solo.Pkg.1.var", files={"Saves/scene/s.json": "{}"})
    lib.scan()
    lib.set_setting("auto_hide_deps", "1")
    lib.demote("Solo.Pkg.1.var")
    assert not by_name(lib)["Solo.Pkg.1.var"]["is_direct"] and lib.status_counts()["orphan"] == 1
    drop(vam.addon_packages, "Other.Thing.1.var")
    lib.scan()
    pk = by_name(lib)
    assert not pk["Solo.Pkg.1.var"]["is_direct"] and pk["Other.Thing.1.var"]["is_direct"]
    assert lib.status_counts()["orphan"] == 1
    lib.close()


def test_qvaro_disabled_package_is_deleted_on_removal(vam):

    lib = make_lib(vam)
    from .conftest import place_var
    from .helpers import var_bytes

    place_var(vam.addon_packages, "Qvaro.Pkg.1.var", var_bytes("Qvaro.Pkg.1.var"), qvaro=True)
    place_var(vam.addon_packages, "Legacy.Pkg.1.var", var_bytes("Legacy.Pkg.1.var"), disabled=True)
    lib.scan()
    assert by_name(lib)["Qvaro.Pkg.1.var"]["storage_state"] == "disabled"
    lib.force_remove(["Qvaro.Pkg.1.var", "Legacy.Pkg.1.var"])
    assert (
        not (vam.addon_packages / "Qvaro.Pkg.1.DISABLED").exists()
        and not (vam.addon_packages / "Legacy.Pkg.1.var.disabled").exists()
    )
    lib.scan()
    assert by_name(lib) == {}
    lib.close()


def test_integrity_check_survives_a_garbled_stream(vam, monkeypatch):
    import zlib

    import vambackstage.core.scanner as scanner

    lib = make_lib(vam)
    drop(vam.addon_packages, "Good.Pkg.1.var")
    drop(vam.addon_packages, "Bad.Pkg.1.var")
    lib.scan()
    real = scanner.verify_package_full

    def flaky(path):
        if "Bad.Pkg" in str(path):
            raise zlib.error("Error -3 while decompressing data: invalid distance too far back")
        return real(path)

    monkeypatch.setattr(scanner, "verify_package_full", flaky)
    import vambackstage.core.varfile as varfile
    from vambackstage.core.varfile import IntegrityError, verify_package_full

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def testzip(self):
            raise zlib.error("garbled")

    monkeypatch.setattr(varfile.zipfile, "ZipFile", lambda *a, **k: Boom())
    try:
        verify_package_full(vam.addon_packages / "Good.Pkg.1.var")
    except IntegrityError as err:
        assert "garbled" in str(err)
    else:
        raise AssertionError("zlib.error should become IntegrityError")
    lib.close()


def test_library_dir_with_tombstones_can_be_removed(vam):
    lib = make_lib(vam)
    aux = vam.aux_dir("offload")
    lib.add_library_dir(str(aux))
    aid = lib.list_library_dirs()["aux"][0]["id"]
    p = drop(aux, "Parked.Pkg.1.var")
    lib.scan()
    assert by_name(lib)["Parked.Pkg.1.var"]["storage_state"] == "offloaded"
    p.unlink()
    lib.scan()
    assert "Parked.Pkg.1.var" not in by_name(lib)
    lib.remove_library_dir(aid)
    assert lib.list_library_dirs()["aux"] == []
    lib.close()


def test_cascade_disable_spares_direct_packages_and_rescan_clears_corruption(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Look.L.1.var")
    drop(vam.addon_packages, "Scene.S.1.var", deps=["Look.L.1"])
    lib.scan()
    lib.promote("Look.L.1.var")
    lib.set_enabled(["Scene.S.1.var"], False)
    pk = by_name(lib)
    assert pk["Scene.S.1.var"]["storage_state"] == "disabled" and pk["Look.L.1.var"]["storage_state"] == "enabled"
    lib.db.set_package_corrupted("Look.L.1.var", True)
    lib.store.build_from_db()
    assert by_name(lib)["Look.L.1.var"]["is_corrupted"]
    drop(vam.addon_packages, "Look.L.1.var", files={"a.txt": "different bytes now"})
    lib.scan()
    assert not by_name(lib)["Look.L.1.var"]["is_corrupted"]
    lib.close()


def test_missing_count_matches_the_missing_list(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Scene.One.1.var", deps=["Gone.Pkg.1"])
    drop(vam.addon_packages, "Scene.Two.1.var", deps=["Gone.Pkg.2", "Gone.Pkg.1"])
    lib.scan()
    rows = lib.missing_deps()
    assert sorted(r["ref"] for r in rows) == ["Gone.Pkg.1", "Gone.Pkg.2"]
    assert lib.status_counts()["missing_unique"] == len(rows) == 2
    lib.close()


def test_packaged_content_reports_its_package_missing_refs(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Scene.Needs.1.var", deps=["Gone.Pkg.3"], files={"Saves/scene/Needy.json": "{}"})
    drop(vam.addon_packages, "Fine.Pkg.1.var", files={"Saves/scene/Fine.json": "{}"})
    lib.scan()
    items = {c["internal_path"]: c for c in lib.list_contents()}
    assert items["Saves/scene/Needy.json"]["missing_refs"] == ["Gone.Pkg.3"]
    assert items["Saves/scene/Fine.json"]["missing_refs"] == []
    lib.close()


def test_real_package_supersedes_dummy_and_alias(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Scene.Needs.1.var", deps=["Gone.Pkg.3", "Have.Pkg.2"])
    drop(vam.addon_packages, "Have.Pkg.5.var")
    lib.scan()
    assert lib.create_dummies(["Gone.Pkg.3"])["created"] == ["Gone.Pkg.3.var"]
    assert lib.create_aliases(["Have.Pkg.2"])["created"][0]["filename"] == "Have.Pkg.2.var"
    pk = by_name(lib)
    assert (
        pk["Gone.Pkg.3.var"]["is_dummy"] and pk["Have.Pkg.2.var"]["is_alias"] and not pk["Have.Pkg.5.var"]["is_alias"]
    )
    assert lib.placeholder_counts() == {"dummies": 1, "aliases": 1}
    assert pk["Scene.Needs.1.var"]["missing_deps"] == 0
    drop(vam.addon_packages, "Gone.Pkg.3.var", files={"Saves/scene/Real.json": "{}"})
    drop(vam.addon_packages, "Have.Pkg.2.var", files={"Saves/scene/Old.json": "{}"})
    summary = lib.scan()
    assert sorted(summary.replaced) == ["Gone.Pkg.3.var", "Have.Pkg.2.var"]
    assert not (vam.addon_packages / "Dummies" / "Gone.Pkg.3.var").exists()
    assert not (vam.addon_packages / "Aliases" / "Have.Pkg.2.var").exists()
    pk = by_name(lib)
    assert not pk["Gone.Pkg.3.var"]["is_dummy"] and pk["Gone.Pkg.3.var"]["content_count"] == 1
    assert not pk["Have.Pkg.2.var"]["is_alias"] and pk["Have.Pkg.2.var"]["content_count"] == 1
    assert lib.placeholder_counts() == {"dummies": 0, "aliases": 0}
    assert (vam.addon_packages / "Have.Pkg.5.var").exists()
    lib.close()


def test_replace_placeholder_with_the_real_file(vam, tmp_path):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Scene.Needs.1.var", deps=["Gone.Pkg.3"])
    lib.scan()
    lib.create_dummies(["Gone.Pkg.3"])
    label = lib.db.find_or_create_label("Hunted")
    lib.db.apply_label_to_packages(label["id"], ["Gone.Pkg.3.var"])
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    real = drop(downloads, "Gone.Pkg.3.var", files={"Saves/scene/Real.json": "{}"})
    wrong = drop(downloads, "Gone.Pkg.4.var")
    with pytest.raises(ValueError):
        lib.replace_placeholder("Gone.Pkg.3.var", str(wrong))
    with pytest.raises(ValueError):
        lib.replace_placeholder("Scene.Needs.1.var", str(real))
    res = lib.replace_placeholder("Gone.Pkg.3.var", str(real))
    assert res["replaced"] == "dummy" and res["is_direct"] is False
    pk = by_name(lib)
    assert (
        not pk["Gone.Pkg.3.var"]["is_dummy"]
        and pk["Gone.Pkg.3.var"]["content_count"] == 1
        and not pk["Gone.Pkg.3.var"]["is_direct"]
    )
    assert pk["Gone.Pkg.3.var"]["label_ids"] == [label["id"]]
    assert (vam.addon_packages / "Gone.Pkg.3.var").exists() and not (
        vam.addon_packages / "Dummies" / "Gone.Pkg.3.var"
    ).exists()
    assert real.exists()
    assert pk["Scene.Needs.1.var"]["missing_deps"] == 0
    lib.close()


def test_same_package_under_another_creator_name_can_be_aliased(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Scene.Uses.1.var", deps=["Dnaddr.Raikou_v2.1"])
    drop(vam.addon_packages, "源赖光.Raikou_v2.1.var", files={"Custom/Atom/Person/Appearance/raikou.vap": "{}"})
    lib.scan()
    rows = lib.missing_deps()
    assert [r["ref"] for r in rows] == ["Dnaddr.Raikou_v2.1"]
    assert rows[0]["near_matches"] == ["源赖光.Raikou_v2.1.var"] and not rows[0]["is_fallback"]
    res = lib.create_aliases(["Dnaddr.Raikou_v2.1"], {"Dnaddr.Raikou_v2.1": "源赖光.Raikou_v2.1.var"})
    assert res["created"] == [
        {
            "filename": "Dnaddr.Raikou_v2.1.var",
            "source": "源赖光.Raikou_v2.1.var",
            "linked": res["created"][0]["linked"],
        }
    ]
    assert lib.missing_deps() == []
    pk = by_name(lib)
    assert pk["Dnaddr.Raikou_v2.1.var"]["is_alias"] and pk["Scene.Uses.1.var"]["missing_deps"] == 0
    assert lib.create_aliases(["Nobody.Raikou_v2.1"], {"Nobody.Raikou_v2.1": "Nope.Raikou_v2.9.var"})["skipped"][0][
        1
    ].startswith("Nope.Raikou_v2.9.var is not")
    lib.close()


def test_old_versions_nothing_names_exactly_are_superseded(vam):
    lib = make_lib(vam)
    for v in (1, 2, 3):
        drop(vam.addon_packages, f"Acid.Tool.{v}.var")
    drop(vam.addon_packages, "Scene.Old.1.var", deps=["Acid.Tool.1"])
    drop(vam.addon_packages, "Scene.New.1.var", deps=["Acid.Tool.latest"])
    lib.scan()
    pk = by_name(lib)
    assert pk["Acid.Tool.2.var"]["is_superseded"] and pk["Acid.Tool.2.var"]["superseded_by"] == "Acid.Tool.3.var"
    assert not pk["Acid.Tool.1.var"]["is_superseded"] and not pk["Acid.Tool.3.var"]["is_superseded"]
    assert lib.status_counts()["superseded"] == 1
    res = lib.remove_superseded()
    assert res["count"] == 1 and not (vam.addon_packages / "Acid.Tool.2.var").exists()
    pk = by_name(lib)
    assert "Acid.Tool.2.var" not in pk and lib.status_counts()["superseded"] == 0
    assert pk["Scene.Old.1.var"]["missing_deps"] == 0 and pk["Scene.New.1.var"]["missing_deps"] == 0
    lib.toggle_enabled("Acid.Tool.3.var")
    drop(vam.addon_packages, "Acid.Tool.2.var")
    lib.scan()
    assert lib.status_counts()["superseded"] == 0
    lib.close()
