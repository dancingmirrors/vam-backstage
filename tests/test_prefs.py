from vambackstage.core.prefs import (
    copy_sidecars_to_new_version,
    prefs_key,
    read_all_prefs,
    set_favorite,
    set_hidden,
    sidecar_path,
)


def test_sidecar_paths(vam):
    p = sidecar_path(vam.vam_dir, "A.B.1.var", "Custom/Hair/x.vam", ".hide")
    assert p == vam.prefs_dir / "A.B.1" / "Custom/Hair/x.vam.hide"
    p = sidecar_path(vam.vam_dir, "__local__", "Custom/Atom/Person/Appearance/x.vap.disabled", ".fav")
    assert p == vam.vam_dir / "Custom/Atom/Person/Appearance/x.vap.fav"
    assert prefs_key("A.B.1.var", "x.vap.disabled") == "A.B.1.var/x.vap"


def test_set_and_read(vam):
    set_hidden(vam.vam_dir, "A.B.1.var", "Saves/scene/s.json", True)
    set_favorite(vam.vam_dir, "A.B.1.var", "Saves/scene/s.json", True)
    set_hidden(vam.vam_dir, "C.D.2.var", "Custom/Hair/h.vam", True)
    (vam.custom_dir / "Atom/Person/Appearance").mkdir(parents=True)
    (vam.custom_dir / "Atom/Person/Appearance/look.vap.disabled").write_bytes(b"x")
    set_favorite(vam.vam_dir, "__local__", "Custom/Atom/Person/Appearance/look.vap.disabled", True)
    (vam.saves_dir / "scene").mkdir()
    (vam.saves_dir / "scene/orphan.json.hide").write_bytes(b"")
    prefs = read_all_prefs(vam.vam_dir)
    assert prefs["A.B.1.var/Saves/scene/s.json"] == {"hidden": True, "favorite": True}
    assert prefs["C.D.2.var/Custom/Hair/h.vam"] == {"hidden": True, "favorite": False}
    assert prefs["__local__/Custom/Atom/Person/Appearance/look.vap"] == {"hidden": False, "favorite": True}
    assert "__local__/Saves/scene/orphan.json" not in prefs
    set_hidden(vam.vam_dir, "A.B.1.var", "Saves/scene/s.json", False)
    assert read_all_prefs(vam.vam_dir)["A.B.1.var/Saves/scene/s.json"] == {"hidden": False, "favorite": True}
    set_hidden(vam.vam_dir, "A.B.1.var", "nothing/there.json", False)


def test_read_missing_dir(tmp_path):
    assert read_all_prefs(tmp_path / "nope") == {}


def test_copy_sidecars(vam):
    set_hidden(vam.vam_dir, "A.B.1.var", "Saves/scene/s.json", True)
    set_favorite(vam.vam_dir, "A.B.1.var", "Saves/scene/t.json", True)
    n = copy_sidecars_to_new_version(
        vam.vam_dir, "A.B.1.var", "A.B.2.var", ["Saves/scene/s.json", "Saves/scene/gone.json"]
    )
    assert n == 1
    prefs = read_all_prefs(vam.vam_dir)
    assert prefs["A.B.2.var/Saves/scene/s.json"]["hidden"] is True
    assert "A.B.2.var/Saves/scene/t.json" not in prefs
    assert copy_sidecars_to_new_version(vam.vam_dir, "Nope.1.var", "A.B.3.var", ["x"]) == 0
