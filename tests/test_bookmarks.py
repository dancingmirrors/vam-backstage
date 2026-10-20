import json
import time

import pytest

from vambackstage.backend import Backend
from vambackstage.core.bookmarks import (
    bookmark_filenames,
    bookmark_key,
    bookmark_package_names,
    bookmark_resource_id,
    external_link,
    is_bookmark_key,
    is_hub_downloadable,
    match_packages,
)
from vambackstage.hub.client import HubError

from .fake_http import FakeHub, FakeTransport, file_route
from .helpers import by_name, drop, make_lib, var_bytes

PAID = {
    "resource_id": "500",
    "title": "Velvet Look",
    "username": "Ava",
    "user_id": "9",
    "type": "Looks",
    "category": "Paid",
    "version_string": "2.1",
    "tag_line": "A paid look",
    "promotional_link": "https://www.patreon.com/ava",
    "tags": "goth,look",
    "dependency_count": 3,
    "dependencies": {"Ava.Velvet": []},
}


def free_detail(url="https://hub/dl/scene"):
    return {
        "resource_id": "600",
        "title": "Harbor Scene",
        "username": "Bob",
        "user_id": "8",
        "type": "Scenes",
        "category": "Free",
        "hubFiles": [{"filename": "Bob.Harbor.3.var", "urlHosted": url, "file_size": "1234", "licenseType": "CC BY"}],
        "dependencies": {"Bob.Harbor": []},
    }


def wait_for(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def backend(vam, tmp_path):
    t = FakeTransport()
    b = Backend(db_path=vam.db_path, transport=t, cache_dir=tmp_path / "cache")
    b.lib.set_vam_dir(str(vam.vam_dir))
    b.start()
    b.hub_fake = FakeHub(t)
    b.transport_fake = t
    yield b
    b.close()


def test_helpers():
    assert bookmark_key(12) == "bookmark:12" and is_bookmark_key("bookmark:12") and not is_bookmark_key("A.B.1.var")
    assert bookmark_resource_id("bookmark:12") == "12" and bookmark_resource_id("A.B.1.var") == "A.B.1.var"
    multi = {
        "hubFiles": [{"filename": "Ava.Velvet.2.var"}, {"filename": "null"}, "junk", {"filename": "Ava.Velvet.2"}],
        "dependencies": {"ava.velvet": [], "Ava.Hair.latest": [], "": [], "NoDot": []},
    }
    assert bookmark_package_names(multi) == ["Ava.Velvet", "Ava.Hair"]
    assert bookmark_filenames(multi) == ["Ava.Velvet.2.var"]
    assert bookmark_package_names(PAID) == ["Ava.Velvet"] and bookmark_filenames(PAID) == []
    assert bookmark_package_names({"dependencies": "nope", "hubFiles": None}) == []
    assert external_link(PAID) == "https://www.patreon.com/ava"
    assert external_link({"external_url": " https://discord.gg/x "}) == "https://discord.gg/x"
    assert external_link({"promotional_link": "javascript:alert(1)", "external_url": "patreon.com/x"}) is None
    assert is_hub_downloadable(free_detail()) and not is_hub_downloadable(PAID)
    assert not is_hub_downloadable({"hubFiles": [{"filename": "A.B.1.var", "downloadUrl": "null", "urlHosted": ""}]})
    index = {
        "Ava.Velvet.1.var": {"package_name": "Ava.Velvet", "hub_resource_id": None},
        "Other.Pkg.1.var": {"package_name": "Other.Pkg", "hub_resource_id": "500"},
        "Dummy.Velvet.1.var": {"package_name": "Ava.Velvet", "hub_resource_id": None},
    }
    names = list(index)
    assert match_packages(PAID, "500", names, index, lambda fn: False) == "Other.Pkg.1.var", "the Hub link wins"
    assert match_packages(PAID, "500", names[:1], index, lambda fn: False) == "Ava.Velvet.1.var"
    versions = {f"Ava.Velvet.{v}.var": {"package_name": "Ava.Velvet", "version": str(v)} for v in (1, 10, 3)}
    assert match_packages(PAID, "500", list(versions), versions, lambda fn: False) == "Ava.Velvet.10.var"
    assert match_packages(PAID, "500", ["Dummy.Velvet.1.var"], index, lambda fn: fn.startswith("Dummy")) is None
    assert match_packages(PAID, "501", ["Other.Pkg.1.var", "Nope.1.var"], index, lambda fn: False) is None


def test_add_list_note_and_remove(vam):
    lib = make_lib(vam)
    lib.scan()
    events = []
    lib.events.subscribe("bookmarks:updated", lambda ch, p: events.append(ch))
    res = lib.add_bookmark("500", PAID, note="  tier 2 on patreon ")
    assert res["created"] and res["note"] == "tier 2 on patreon" and res["title"] == "Velvet Look"
    assert res["package_names"] == ["Ava.Velvet"] and not res["is_downloadable"]
    assert res["external_link"] == "https://www.patreon.com/ava" and res["category"] == "Paid"
    assert res["filename"] == bookmark_key("500") and res["installed_filename"] is None
    assert res["is_bookmark"] and res["creator"] == "Ava" and res["type"] == "Looks" and res["dep_count"] == 3
    assert res["created_at"] > 0 and res["first_seen_at"] == res["created_at"]
    again = lib.add_bookmark(500, dict(PAID, title="Velvet Look v3"))
    assert not again["created"] and again["title"] == "Velvet Look v3" and again["note"] == "tier 2 on patreon"
    assert lib.is_bookmarked("500") and not lib.is_bookmarked("501") and lib.bookmarked_resource_ids() == {"500"}
    assert [b["resource_id"] for b in lib.bookmarks()] == ["500"]
    assert lib.set_bookmark_note("500", "  ") and lib.bookmark_detail("500")["note"] == ""
    assert lib.set_bookmark_note("500", "ask in discord") and lib.bookmark_detail("500")["note"] == "ask in discord"
    assert not lib.set_bookmark_note("501", "nobody")
    with pytest.raises(ValueError):
        lib.add_bookmark("null", {"title": "nothing"})
    assert lib.add_bookmark(None, {"resource_id": "77", "title": "From the snapshot"})["resource_id"] == "77"
    assert lib.remove_bookmark("500") and not lib.remove_bookmark("500")
    assert lib.bookmarked_resource_ids() == {"77"} and lib.bookmark_detail("500") is None
    assert events == ["bookmarks:updated"] * 6
    lib.close()


def test_summary_sees_packages_already_in_the_library(vam):
    lib = make_lib(vam)
    drop(vam.addon_packages, "Ava.Velvet.1.var")
    lib.scan()
    b = lib.add_bookmark("500", PAID)
    assert b["installed_filename"] == "Ava.Velvet.1.var"
    assert lib.bookmarks()[0]["installed_filename"] == "Ava.Velvet.1.var"
    f = lib.add_bookmark("600", free_detail())
    assert f["is_downloadable"] and f["hub_filenames"] == ["Bob.Harbor.3.var"] and f["size_bytes"] == 1234
    assert f["license"] == "CC BY" and f["installed_filename"] is None
    assert [x["resource_id"] for x in lib.bookmarks()] == ["600", "500"]
    lib.close()


def test_scan_clears_a_bookmark_when_its_package_arrives(vam):
    lib = make_lib(vam)
    lib.scan()
    lib.add_bookmark("500", PAID, note="bought it")
    lib.add_bookmark("600", free_detail())
    resolved = []
    lib.events.subscribe("bookmarks:resolved", lambda ch, p: resolved.append(p))
    drop(vam.addon_packages, "Ava.Velvet.2.var")
    lib.scan()
    assert lib.bookmarked_resource_ids() == {"600"}
    assert len(resolved) == 1
    gone = resolved[0]["bookmarks"]
    assert [b["installed_filename"] for b in gone] == ["Ava.Velvet.2.var"] and gone[0]["note"] == "bought it"
    drop(vam.addon_packages, "Scene.Needs.1.var", deps=["Bob.Harbor.3"])
    lib.scan()
    assert lib.create_dummies(["Bob.Harbor.3"])["created"] == ["Bob.Harbor.3.var"]
    assert lib.bookmarked_resource_ids() == {"600"}, "a stand-in is not the package"
    drop(vam.addon_packages, "Bob.Harbor.3.var", files={"Saves/scene/Harbor.json": "{}"})
    lib.scan()
    assert lib.bookmarked_resource_ids() == set() and not by_name(lib)["Bob.Harbor.3.var"]["is_dummy"]
    assert lib.restore_bookmarks(gone) == 1
    back = lib.bookmark_detail("500")
    assert back["note"] == "bought it" and back["created_at"] == gone[0]["created_at"]
    assert back["installed_filename"] == "Ava.Velvet.2.var"
    assert lib.restore_bookmarks([{"resource_id": "bogus"}, {"no": "id"}]) == 0
    lib.close()


def test_integrate_new_var_clears_a_bookmark(vam, tmp_path):
    lib = make_lib(vam)
    lib.scan()
    lib.add_bookmark("600", free_detail())
    assert lib.resolve_bookmarks([]) == [] and lib.resolve_bookmarks(["Nope.1.var"]) == []
    p = drop(vam.addon_packages, "Bob.Harbor.3.var")
    lib.integrate_new_var(str(p), is_direct=True)
    assert lib.bookmarks() == []
    lib.close()


def test_bookmark_resource_fetches_the_listing_and_installs_from_it(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.scan()
    scene = var_bytes("Bob.Harbor.3.var")
    file_route(t, r"hub/dl/scene$", scene)
    fake.details["600"] = free_detail()
    fake.details["500"] = PAID
    res = backend.bookmark_resource("600", {"resource_id": "600", "title": "Harbor Scene (search row)"})
    assert res["created"] and res["package_names"] == ["Bob.Harbor"] and res["title"] == "Harbor Scene"
    assert [q["action"] for q in fake.requests] == ["getResourceDetail"]
    backend.bookmark_resource("500", PAID)
    assert len(fake.requests) == 1, "a detail record is stored as given"
    page = backend.bookmark_resource("999", None, "saw it on discord", "Mystery Pack")
    assert page["title"] == "Mystery Pack" and page["note"] == "saw it on discord" and not page["is_downloadable"]
    with pytest.raises(HubError):
        backend.bookmark_resource("998")
    assert backend.lib.bookmarked_resource_ids() == {"600", "500", "999"}
    out = backend.install_bookmarks(["600", "500", "998"])
    assert out["queued"] == 1 and out["already"] == 0
    assert [rid for rid, _why in out["failed"]] == ["500", "998"] and "No downloadable" in out["failed"][0][1]
    assert wait_for(lambda: "600" not in backend.lib.bookmarked_resource_ids())
    pk = by_name(backend.lib)
    assert pk["Bob.Harbor.3.var"]["is_direct"] and pk["Bob.Harbor.3.var"]["hub_resource_id"] == "600"
    assert backend.lib.bookmarked_resource_ids() == {"500", "999"}
    assert backend.install_bookmarks(["600"]) == {"queued": 0, "already": 1, "failed": []}


def test_catalog_link_clears_a_bookmark_that_names_no_package(backend, vam):
    t = backend.transport_fake
    backend.lib.scan()
    catalog = json.dumps({"Ava.Velvet.2.var": 500}).encode()
    t.route(r"packages\.json", lambda m, u, h, d: (200, {"ETag": '"e"'}, catalog), method="GET")
    nameless = dict(PAID, dependencies={"": []})
    backend.hub_fake.details["500"] = nameless
    res = backend.bookmark_resource("500", nameless)
    assert res["package_names"] == [] and res["title"] == "Velvet Look"
    drop(vam.addon_packages, "Ava.Velvet.2.var")
    backend.lib.scan()
    assert wait_for(lambda: backend.lib.bookmarked_resource_ids() == set())
    assert "Ava.Velvet.2.var" in by_name(backend.lib)
