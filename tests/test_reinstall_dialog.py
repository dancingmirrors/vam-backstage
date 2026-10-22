import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk  # noqa: F401

    _GTK_OK = True
except Exception:  # noqa: BLE001 - a missing or broken PyGObject/typelib means we can't test here
    _GTK_OK = False

if not _GTK_OK:
    pytest.skip("GTK 4 / libadwaita not available", allow_module_level=True)

from vambackstage.ui.reinstall import describe_file, describe_ref, plan_sections, plan_summary  # noqa: E402


def ref_row(**kw) -> dict:
    row = {
        "ref": "A.B.3",
        "needed_by": [],
        "action": "unavailable",
        "kind": None,
        "filename": None,
        "reason": "not_on_hub",
        "size": None,
        "offers": None,
        "stand_in": None,
        "stand_in_kind": None,
    }
    row.update(kw)
    return row


def file_row(filename: str, **kw) -> dict:
    row = {"filename": filename, "title": filename, "is_root": False, "is_placeholder": False, "needed_by": []}
    row.update({"action": "reinstall", "reason": None, "size": 1}, **kw)
    return row


def test_stand_in_wording_follows_whether_the_download_replaces_it():
    same = ref_row(action="install", kind="requested", filename="A.B.3.var", stand_in="A.B.3.var")
    same["stand_in_kind"] = "placeholder"
    assert describe_ref(same, {}) == ("INSTALL V3", "success", "replaces the stand-in A.B.3.var")
    other = dict(same, ref="A.B.min5", filename="A.B.5.var")
    assert describe_ref(other, {})[2] == "used instead of the stand-in A.B.3.var, which stays"
    fallback = ref_row(stand_in="A.B.5.var", stand_in_kind="fallback", offers="A.B.5.var")
    files = {"A.B.5.var": file_row("A.B.5.var")}
    assert describe_ref(fallback, files) == (
        "NOT ON THE HUB",
        "error",
        "the Hub does not have this version (it has v5); v5 stays in use and is reinstalled",
    )
    latest = ref_row(ref="A.B.latest")
    assert describe_ref(latest, {})[2] == "the Hub does not have this package; it stays missing"
    assert describe_file(file_row("A.B.3.var", is_placeholder=True))[0] == "FETCH"


def test_fetching_a_stand_in_is_listed_with_the_missing_versions():
    plan = {
        "refs": [ref_row(action="install", kind="requested", filename="A.B.3.var")],
        "files": [file_row("C.D.1.var", is_placeholder=True), file_row("E.F.2.var")],
        "downloads": [
            {"filename": "A.B.3.var", "reinstall": False, "file_size": 10},
            {"filename": "C.D.1.var", "reinstall": False, "file_size": None},
            {"filename": "E.F.2.var", "reinstall": True, "file_size": 30},
        ],
        "lookup_failed": 2,
        "total_size": 40,
    }
    sections = plan_sections(plan)
    assert [r.get("ref") or r["filename"] for r in sections["requested"]] == ["A.B.3", "C.D.1.var"]
    assert [r["filename"] for r in sections["again"]] == ["E.F.2.var"]
    plan["files"].append(file_row("A.B.3.var", is_placeholder=True, is_root=True))
    named = [r.get("ref") or r["filename"] for r in plan_sections(plan)["requested"]]
    assert named == ["A.B.3", "C.D.1.var"], "a stand-in a reference already replaces is listed once"
    text = plan_summary(plan)
    assert text.startswith("3 downloads, 40 B or more.") and "did not answer for 2 names" in text
