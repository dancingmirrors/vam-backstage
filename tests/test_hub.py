import json
import re
import time

import pytest

from vambackstage.backend import Backend
from vambackstage.core.licenses import canonicalize_license, is_commercial_use_allowed, is_noncommercial_use_allowed
from vambackstage.hub.client import (
    HubClient,
    HubError,
    hub_package_base_name,
    hub_resource_icon_url,
    resource_detail_matches_package_name,
    title_fits_package,
)
from vambackstage.hub.downloads import DownloadError, concrete_dep_filename, ensure_var_ext, is_flexible_filename
from vambackstage.hub.http import TransportError
from vambackstage.hub.packages_json import PackagesJsonIndex, build_indexes, parse_package_entry

from .fake_http import FakeHub, FakeTransport, file_route
from .helpers import by_name, drop, var_bytes


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
    assert hub_package_base_name("Author.Pkg.12.var") == "Author.Pkg"
    assert hub_package_base_name("Author.Pkg.latest") == "Author.Pkg"
    assert hub_package_base_name("Author.Pkg.min5") == "Author.Pkg"
    assert hub_package_base_name("Author.Pkg") == "Author.Pkg"
    assert hub_package_base_name("") == ""
    assert hub_resource_icon_url("66186") == "https://1424104733.rsc.cdn77.org/data/resource_icons/66/66186.jpg"
    assert hub_resource_icon_url("x") is None and hub_resource_icon_url(0) is None
    d = {"resource_id": "1", "hubFiles": [{"filename": "caelryn.Skallet.3.var"}], "dependencies": {"Other.Dep": []}}
    assert resource_detail_matches_package_name(d, "caelryn.skallet")
    assert not resource_detail_matches_package_name(d, "Other.Dep.latest")
    assert resource_detail_matches_package_name(dict(d, title="Dep"), "Other.Dep.latest")
    assert not resource_detail_matches_package_name(d, "Some.Thing")
    assert (
        ensure_var_ext("A.B.1") == "A.B.1.var"
        and ensure_var_ext("A.B.1.var") == "A.B.1.var"
        and ensure_var_ext("") is None
    )
    assert (
        is_flexible_filename("A.B.latest.var")
        and is_flexible_filename("A.B.min3")
        and not is_flexible_filename("A.B.3.var")
    )
    assert concrete_dep_filename({"packageName": "A.B", "latest_version": "7"}) == "A.B.7.var"
    assert concrete_dep_filename({"packageName": "A.B", "latest_version": None}) is None
    assert parse_package_entry("A.B.3.var") == ("A.B", 3) and parse_package_entry("A.B.latest.var") is None
    idx, fn = build_indexes({"A.B.1.var": 1, "A.B.3.var": 2, "C.D.1.var": 3, "junk": 4})
    assert idx["A.B"] == {"version": 3, "filename": "A.B.3.var", "resource_id": 2} and fn["junk"] == 4


def test_licenses():
    assert canonicalize_license(" cc  by-nc ") == "CC BY-NC"
    assert canonicalize_license("CC0") == "Public Domain" and canonicalize_license("pc ea") == "PC EA"
    assert canonicalize_license("Weird") == "Weird" and canonicalize_license("") is None
    assert is_commercial_use_allowed("CC BY") is True and is_commercial_use_allowed("CC BY-NC-SA") is False
    assert is_commercial_use_allowed("PC") is False and is_commercial_use_allowed("Unknown") is None
    assert is_noncommercial_use_allowed("CC BY-NC") is True and is_noncommercial_use_allowed("Questionable") is False


def test_client_filters_search_detail_find(backend):
    hub, fake = backend.hub, backend.hub_fake
    f = hub.get_filters()
    assert f["type"] == ["Scenes", "Looks"]
    hub.get_filters()
    assert len([r for r in fake.requests if r["action"] == "getInfo"]) == 1
    assert json.loads(backend.lib.db.get_setting("hub_filters_json"))["sort"][0] == "Latest Update"

    fake.search_pages[1] = {
        "status": "success",
        "pagination": {"total_found": "3", "total_pages": "2"},
        "resources": [
            {"resource_id": "1", "title": "A", "hubFiles": [{"licenseType": "CC BY"}]},
            {"resource_id": "2", "title": "B", "hubFiles": [{"licenseType": "PC"}]},
            {"resource_id": "3", "title": "C", "licenseType": "CC BY-NC"},
        ],
    }
    r = hub.search_resources({"search": "x", "type": "All", "license": "Commercial use allowed"})
    assert [x["title"] for x in r["resources"]] == ["A"] and r["total_found"] == 3 and r["total_pages"] == 2
    assert fake.requests[-1]["searchall"] == "true" and "type" not in fake.requests[-1]
    r = hub.search_resources({"search": "x", "license": "CC BY-NC"})
    assert [x["title"] for x in r["resources"]] == ["C"]
    hub.search_resources({"search": "x", "license": "CC BY-NC"})
    assert len([q for q in fake.requests if q["action"] == "getResources"]) == 2
    # only resources that survive the client-side license filter are persisted
    assert backend.lib.db.get_hub_resource("1")["search_json"] and backend.lib.db.get_hub_resource("3")["search_json"]
    assert backend.lib.db.get_hub_resource("2") is None

    fake.details["10"] = {
        "resource_id": "10",
        "title": "Ten",
        "type": "Looks",
        "user_id": "5",
        "username": "U",
        "hubFiles": [{"filename": "U.Ten.1.var"}],
        "dependencies": {"U.Ten": []},
    }
    d = hub.get_resource_detail("10")
    assert d["title"] == "Ten" and hub.get_cached_detail(10)["title"] == "Ten"
    assert json.loads(backend.lib.db.get_hub_resource("10")["hub_json"])["title"] == "Ten"
    with pytest.raises(HubError) as ei:
        hub.get_resource_detail("999")
    assert ei.value.not_found
    fake.by_name["U.Ten"] = fake.details["10"]
    fake.by_name["Dep.Only"] = {
        "resource_id": "11",
        "title": "Look that depends",
        "hubFiles": [{"filename": "X.Look.1.var"}],
        "dependencies": {"X.Look": [{"packageName": "Dep.Only"}]},
    }
    assert hub.get_resource_detail_by_name("U.Ten")["resource_id"] == "10"
    assert hub.get_resource_detail_by_name("Dep.Only") is None
    assert hub.get_resource_detail_by_name("Nope.Thing") is None
    assert hub.get_cached_detail("11")["title"] == "Look that depends"

    fake.find["A.B.1"] = {"filename": "A.B.1.var", "resource_id": "7", "downloadUrl": "https://dl/7", "file_size": "10"}
    names = [f"P.K{i}.1" for i in range(60)] + ["A.B.1"]
    res = hub.find_packages(names)
    assert res["A.B.1"]["filename"] == "A.B.1.var" and res["P.K1.1"]["filename"] == "null"
    assert len([q for q in fake.requests if q["action"] == "findPackages"]) == 2
    assert backend.lib.db.get_hub_resource("7")["find_json"]
    hub.invalidate_caches()
    assert backend.lib.db.get_setting("hub_filters_json") is None


def test_client_transport_errors(backend):
    t = FakeTransport()
    client = HubClient(backend.lib.db, transport=t)
    with pytest.raises(HubError):
        client.get_filters()
    t.route(r"api\.php", lambda m, u, h, d: (500, {}, b"boom"), method="POST")
    with pytest.raises(HubError):
        client.refresh_filters()


def test_packages_json_fetch_and_updates(backend):
    t = backend.transport_fake
    data = {"A.B.1.var": 1, "A.B.3.var": 2, "Dep.Lib.5.var": 3, "C.D.4.var": 9}
    state = {"hits": 0}

    def handler(method, url, headers, body):
        state["hits"] += 1
        if headers.get("If-None-Match") == '"etag1"':
            return 304, {}, b""
        return 200, {"ETag": '"etag1"'}, json.dumps(data).encode()

    t.route(r"s3cdn\.virtamate\.com/data/packages\.json", handler, method="GET")
    pj = backend.packages_json
    assert pj.index is None
    assert backend.refresh_catalog()
    assert pj.index["A.B"]["version"] == 3 and pj.filename_index["Dep.Lib.5.var"] == 3
    assert backend.lib.db.get_setting("packages_json_etag") == '"etag1"'
    pj.fetch()
    assert state["hits"] == 2
    assert backend.lib.store.hub_filename_index is pj.filename_index
    pj2 = PackagesJsonIndex(backend.lib.db, t)
    assert pj2.load_from_cache() and pj2.index["A.B"]["filename"] == "A.B.3.var"
    assert pj2.age_seconds == float("inf")

    drop(
        backend.vam.addon_packages if hasattr(backend, "vam") else backend.lib.dirs.main_dir_path(),
        "A.B.1.var",
        deps=["Dep.Lib.latest"],
    )
    drop(backend.lib.dirs.main_dir_path(), "Dep.Lib.2.var")
    drop(backend.lib.dirs.main_dir_path(), "C.D.2.var")
    drop(backend.lib.dirs.main_dir_path(), "C.D.4.var")
    backend.lib.scan()
    backend.lib.demote("C.D.4.var")
    updates = backend.check_updates()
    assert updates["A.B.1.var"]["hub_version"] == 3 and updates["A.B.1.var"]["hub_resource_id"] == "2"
    assert updates["Dep.Lib.2.var"]["is_dep_update"] and updates["Dep.Lib.2.var"]["needed_by"] == ["A.B.1.var"]
    assert "C.D.2.var" not in updates and "C.D.4.var" not in updates
    missing = backend.missing_deps()
    assert missing == []
    assert by_name(backend.lib)["Dep.Lib.2.var"]["is_hub_replaceable"] is False
    newer = var_bytes("A.B.3.var", deps=["Dep.Lib.latest"])
    file_route(t, r"hub/dl/ab3$", newer)
    backend.hub_fake.find["A.B.3"] = {
        "filename": "A.B.3.var",
        "resource_id": "2",
        "downloadUrl": "https://hub/dl/ab3",
        "file_size": str(len(newer)),
    }
    backend.hub_fake.find["Paid.Thing.2"] = {
        "filename": "Paid.Thing.2.var",
        "resource_id": "77",
        "downloadUrl": "",
        "file_size": "null",
    }
    probe = backend.probe_updates(
        [updates["A.B.1.var"], {"hub_filename": "Paid.Thing.2.var"}, {"hub_filename": "Nope.Pkg.2.var"}]
    )
    assert probe["A.B.3.var"] == {"found": True, "downloadable": True, "resource_id": "2"}
    assert probe["Paid.Thing.2.var"] == {"found": True, "downloadable": False, "resource_id": "77"}
    assert probe["Nope.Pkg.2.var"] == {"found": False, "downloadable": False, "resource_id": None}
    with pytest.raises(DownloadError, match="offers no download"):
        backend.downloads.enqueue_install_ref({"filename": "Paid.Thing.2.var"})
    with pytest.raises(DownloadError, match="does not serve"):
        backend.downloads.enqueue_install_ref({"filename": "Nope.Pkg.2.var"})
    assert backend.ignore_updates([updates["A.B.1.var"]]) == 1
    assert "A.B.1.var" not in backend.check_updates() and "Dep.Lib.2.var" in backend.check_updates()
    assert backend.clear_ignored_updates() == 1 and "A.B.1.var" in backend.check_updates()
    res = backend.enqueue_updates([updates["A.B.1.var"]])
    assert res == {"queued": 1, "already": 0, "failed": []}
    assert wait_for(
        lambda: any(
            d["package_ref"] == "A.B.3.var" and d["status"] in ("completed", "failed") for d in backend.downloads.list()
        )
    )
    pk = by_name(backend.lib)
    assert pk["A.B.3.var"]["is_direct"] and pk["A.B.1.var"]["is_direct"]
    assert backend.enqueue_updates([updates["A.B.1.var"]]) == {"queued": 0, "already": 1, "failed": []}
    drop(backend.lib.dirs.main_dir_path(), "Dep.Lib.5.var")
    backend.lib.scan()
    backend.lib.demote("Dep.Lib.5.var")
    assert backend.check_updates() == {}


def test_enrichment_links_and_caches(backend):
    t, fake = backend.transport_fake, backend.hub_fake
    data = {"Author.Scene.1.var": 100, "Author.Scene.2.var": 100, "Gone.Pkg.1.var": 300}
    t.route(r"packages\.json", lambda m, u, h, d: (200, {}, json.dumps(data).encode()), method="GET")
    fake.details["100"] = {
        "resource_id": "100",
        "title": "Scene Title",
        "type": "Scenes",
        "user_id": "9",
        "username": "Author",
        "tags": "a,b",
        "promotional_link": "https://p",
        "hubFiles": [{"filename": "Author.Scene.2.var"}],
    }
    fake.by_name["Paid.Look"] = {
        "resource_id": "200",
        "title": "Paid Look",
        "type": "Looks",
        "user_id": "9",
        "hubFiles": [{"filename": "Paid.Look.1.var"}],
        "dependencies": {"Paid.Look": []},
    }
    main = backend.lib.dirs.main_dir_path()
    drop(main, "Author.Scene.1.var")
    drop(main, "Paid.Look.1.var")
    drop(main, "Offhub.Own.1.var")
    drop(main, "Gone.Pkg.1.var")
    backend.lib.scan()
    progress = []
    res = backend.enrich_hub(progress.append)
    assert (
        res["total"] == 4
        and res["found"] == 2
        and res["queued"] == 2
        and res["name_checked"] == 2
        and res["name_hits"] == 1
    )
    pk = by_name(backend.lib)
    assert (
        pk["Author.Scene.1.var"]["hub_resource_id"] == "100"
        and pk["Author.Scene.1.var"]["hub_display_name"] == "Scene Title"
    )
    assert pk["Author.Scene.1.var"]["hub_user_id"] == "9" and pk["Author.Scene.1.var"]["hub_tags"] == "a,b"
    assert (
        pk["Paid.Look.1.var"]["hub_resource_id"] == "200" and pk["Paid.Look.1.var"]["hub_display_name"] == "Paid Look"
    )
    assert pk["Offhub.Own.1.var"]["hub_resource_id"] is None
    row = backend.lib.db.get_package("Offhub.Own.1.var")
    assert row["hub_name_checked_at"] is not None
    gone = json.loads(backend.lib.db.get_hub_resource("300")["hub_json"])
    assert gone["_unavailable"] and "not found" in gone["_error"].lower()
    assert any(p.get("phase") == "hub-finalize" for p in progress)
    before = len(fake.requests)
    res2 = backend.enrich_hub()
    assert res2["queued"] == 0 and res2["name_checked"] == 0
    assert len([q for q in fake.requests[before:] if q["action"] == "getResourceDetail"]) == 0
    drop(main, "Author.Scene.2.var")
    backend.lib.scan()
    assert wait_for(lambda: by_name(backend.lib)["Author.Scene.2.var"]["hub_display_name"] == "Scene Title", 5)
    assert by_name(backend.lib)["Author.Scene.2.var"]["hub_resource_id"] == "100"
    assert backend.lib.db.get_hub_user_by_username("Author")["user_id"] == "9"


def test_thumbnails(backend):
    t = backend.transport_fake
    main = backend.lib.dirs.main_dir_path()
    jpg = b"\xff\xd8fakejpg"
    drop(
        main,
        "Author.Scene.1.var",
        files={
            "Saves/scene/S.json": "{}",
            "Saves/scene/S.jpg": jpg,
            "Custom/Hair/h.vam": "h",
            "Custom/Hair/h.jpg": b"hair",
        },
    )
    drop(main, "Plain.Pkg.1.var", files={"Custom/Scripts/x.cs": "//"})
    backend.lib.scan()
    th = backend.thumbs
    assert th.get("pkg:Author.Scene.1.var") == jpg
    assert (th.cache_dir / "Author.Scene.1.var__").parent.exists()
    assert th.get("ct:Author.Scene.1.var\0Custom/Hair/h.jpg") == b"hair"
    many = th.get_many(
        [
            "ct:Author.Scene.1.var\0Saves/scene/S.jpg",
            "ct:Author.Scene.1.var\0Custom/Hair/h.jpg",
            "pkg:Plain.Pkg.1.var",
            "pkg:__local__",
        ]
    )
    assert (
        many["ct:Author.Scene.1.var\0Saves/scene/S.jpg"] == jpg
        and many["pkg:Plain.Pkg.1.var"] is None
        and many["pkg:__local__"] is None
    )
    (backend.lib.dirs.main_dir_path() + "/Plain.Pkg.1.jpg")
    import pathlib

    pathlib.Path(main, "Plain.Pkg.1.jpg").write_bytes(b"companion")
    th.invalidate(["pkg:Plain.Pkg.1.var"])
    assert th.get("pkg:Plain.Pkg.1.var") == b"companion"
    t.route(r"resource_icons/0/100\.jpg", lambda m, u, h, d: (200, {}, b"icon100"), method="GET")
    backend.lib.db.set_hub_resource_id("Author.Scene.1.var", "100")
    backend.lib.store.build_from_db()
    events = []
    backend.events.subscribe("thumbnails:updated", lambda c, p: events.append(p))
    assert backend.resolve_thumbnails() == 1
    assert events and "pkg:Author.Scene.1.var" in events[0]["keys"]
    assert backend.lib.db.get_package("Author.Scene.1.var")["image_url"].endswith("/100.jpg")
    assert th.get("hub-icon:100") == b"icon100"
    assert th.get("pkg:Author.Scene.1.var") == b"icon100"
    assert backend.resolve_thumbnails() == 0
    t.route(r"avatars/9\.jpg", lambda m, u, h, d: (200, {}, b"av9"), method="GET")
    n = backend.avatars.cache_from_resources(
        [{"user_id": "9", "avatar_date": "123", "icon_url": "https://cdn/avatars/9.jpg"}]
    )
    assert n == 1 and backend.avatars.get("9") == b"av9" and backend.avatars.get("8") is None
    assert (
        backend.avatars.cache_from_resources(
            [{"user_id": "9", "avatar_date": "123", "icon_url": "https://cdn/avatars/9.jpg"}]
        )
        == 0
    )


def test_download_install_with_dependencies(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.set_setting("auto_hide_deps", "1")
    backend.lib.scan()
    scene = var_bytes("Author.Scene.1.var", deps=["Dep.Lib.2"])
    lib_var = var_bytes("Dep.Lib.2.var", deps=["Deep.Core.1"])
    core = var_bytes("Deep.Core.1.var")
    file_route(t, r"hub/dl/scene$", scene)
    file_route(t, r"hub/dl/lib$", lib_var)
    file_route(t, r"hub/dl/core$", core)
    fake.details["100"] = {
        "resource_id": "100",
        "title": "The Scene",
        "type": "Scenes",
        "user_id": "9",
        "username": "Author",
        "hubFiles": [
            {"filename": "Author.Scene.1.var", "urlHosted": "https://hub/dl/scene", "file_size": str(len(scene))}
        ],
        "dependencies": {
            "Author.Scene": [
                {
                    "packageName": "Dep.Lib",
                    "filename": "Dep.Lib.2",
                    "latest_version": "2",
                    "resource_id": "200",
                    "downloadUrl": "https://hub/dl/lib",
                    "file_size": str(len(lib_var)),
                }
            ]
        },
    }
    fake.find["Deep.Core.1"] = {
        "filename": "Deep.Core.1.var",
        "resource_id": "300",
        "downloadUrl": "https://hub/dl/core",
        "file_size": str(len(core)),
    }
    progress = []
    backend.events.subscribe("download:progress", lambda c, p: progress.append(p))
    res = backend.downloads.enqueue_install(resource_id="100")
    assert res["inserted"] == 1 and res["unresolved_deps"] == []
    assert wait_for(
        lambda: all(d["status"] == "completed" for d in backend.downloads.list())
        and len(backend.downloads.list()) == 3,
        15,
    )
    pk = by_name(backend.lib)
    assert set(pk) == {"Author.Scene.1.var", "Dep.Lib.2.var", "Deep.Core.1.var"}
    assert pk["Author.Scene.1.var"]["is_direct"] and pk["Author.Scene.1.var"]["hub_resource_id"] == "100"
    assert pk["Author.Scene.1.var"]["hub_display_name"] == "The Scene"
    assert not pk["Dep.Lib.2.var"]["is_direct"] and not pk["Deep.Core.1.var"]["is_direct"]
    assert pk["Author.Scene.1.var"]["missing_deps"] == 0 and pk["Author.Scene.1.var"]["dep_count"] == 2
    assert (vam.addon_packages / "Deep.Core.1.var").exists() and not (
        vam.addon_packages / "Deep.Core.1.var.tmp"
    ).exists()
    rows = {d["package_ref"]: d for d in backend.downloads.list()}
    assert (
        rows["Dep.Lib.2.var"]["priority"] == "dependency"
        and rows["Dep.Lib.2.var"]["parent_ref"] == "Author.Scene.1.var"
    )
    assert rows["Deep.Core.1.var"]["parent_ref"] == "Author.Scene.1.var"
    assert any(p["progress"] == 100 for p in progress)
    assert all(c["hidden"] for c in backend.lib.list_contents("Dep.Lib.2.var"))
    assert not any(c["hidden"] for c in backend.lib.list_contents("Author.Scene.1.var"))
    again = backend.downloads.enqueue_install(resource_id="100", hub_detail=fake.details["100"])
    assert again["inserted"] == 0 and again["already_local"] == 1
    backend.downloads.clear_completed()
    assert backend.downloads.list() == []


def test_download_resume_pause_cancel_and_failure(backend, vam):
    t = backend.transport_fake
    backend.lib.scan()
    data = var_bytes("Big.Pkg.1.var")
    file_route(t, r"hub/dl/big$", data)
    (vam.addon_packages / "Big.Pkg.1.var.tmp").write_bytes(data[:100])
    backend.downloads.enqueue_install(
        hub_detail={
            "resource_id": "1",
            "title": "Big",
            "hubFiles": [
                {"filename": "Big.Pkg.1.var", "downloadUrl": "https://hub/dl/big", "file_size": str(len(data))}
            ],
        }
    )
    assert wait_for(lambda: backend.downloads.list()[0]["status"] == "completed", 10)
    assert any(h.get("Range") == "bytes=100-" for _, u, h, _ in t.calls if u.endswith("/big"))
    assert (vam.addon_packages / "Big.Pkg.1.var").read_bytes() == data
    assert "Big.Pkg.1.var" in by_name(backend.lib)

    backend.downloads.pause_all()
    assert backend.downloads.paused and backend.lib.db.get_setting("downloads_paused") == "1"
    file_route(t, r"hub/dl/two$", var_bytes("Two.Pkg.1.var"))
    backend.downloads.enqueue_install_ref(
        {"filename": "Two.Pkg.1", "downloadUrl": "https://hub/dl/two"}, as_dependency=False
    )
    time.sleep(0.2)
    items = {d["package_ref"]: d for d in backend.downloads.list()}
    assert items["Two.Pkg.1.var"]["status"] == "queued"
    two_id = items["Two.Pkg.1.var"]["id"]
    backend.downloads.cancel(two_id)
    assert (
        backend.downloads.list()[-1]["status"] == "cancelled" if backend.downloads.list()[-1]["id"] == two_id else True
    )
    assert {d["status"] for d in backend.downloads.list() if d["id"] == two_id} == {"cancelled"}
    backend.downloads.retry(two_id)
    backend.downloads.resume_all()
    assert not backend.downloads.paused

    t.route(r"hub/dl/bad$", lambda m, u, h, d: (404, {}, b""), method="GET")
    backend.downloads.enqueue_install_ref({"filename": "Bad.Pkg.1", "downloadUrl": "https://hub/dl/bad"})
    assert wait_for(
        lambda: any(d["package_ref"] == "Bad.Pkg.1.var" and d["status"] == "failed" for d in backend.downloads.list()),
        10,
    )
    bad = next(d for d in backend.downloads.list() if d["package_ref"] == "Bad.Pkg.1.var")
    assert "404" in bad["error"]
    backend.downloads.remove_failed(bad["id"])
    assert not any(d["package_ref"] == "Bad.Pkg.1.var" for d in backend.downloads.list())


def test_install_missing_and_all_missing(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    drop(vam.addon_packages, "Parent.P.1.var", deps=["Need.Me.3", "Builtin.Thing.1"])
    backend.lib.scan()
    need = var_bytes("Need.Me.3.var")
    file_route(t, r"hub/dl/need$", need)
    fake.find["Need.Me.3"] = {
        "filename": "Need.Me.3.var",
        "resource_id": "5",
        "downloadUrl": "https://hub/dl/need",
        "file_size": str(len(need)),
    }
    res = backend.downloads.enqueue_install_missing("Parent.P.1.var")
    assert res["queued"] == 1 and res["unresolved_deps"] == ["Builtin.Thing.1"]
    assert wait_for(lambda: "Need.Me.3.var" in by_name(backend.lib), 10)
    pk = by_name(backend.lib)
    assert pk["Parent.P.1.var"]["missing_deps"] == 1 and not pk["Need.Me.3.var"]["is_direct"]
    res = backend.downloads.enqueue_install_all_missing()
    assert res["queued"] == 0 and res["unresolved_deps"] == ["Builtin.Thing.1"]
    backend.lib.toggle_enabled("Parent.P.1.var")
    fake.find["Builtin.Thing.1"] = {
        "filename": "Builtin.Thing.1.var",
        "resource_id": "6",
        "downloadUrl": "https://hub/dl/builtin",
        "file_size": "1",
    }
    file_route(t, r"hub/dl/builtin$", var_bytes("Builtin.Thing.1.var"))
    backend.downloads.enqueue_install_all_missing()
    assert wait_for(lambda: by_name(backend.lib).get("Builtin.Thing.1.var", {}).get("storage_state") == "disabled", 10)
    assert (vam.addon_packages / "Builtin.Thing.1.var.disabled").exists()


def test_downloads_init_recovery(vam, tmp_path):
    t = FakeTransport()
    b = Backend(db_path=vam.db_path, transport=t, cache_dir=tmp_path / "c")
    b.lib.set_vam_dir(str(vam.vam_dir))
    tmp = vam.addon_packages / "X.Y.1.var.tmp"
    tmp.write_bytes(b"partial")
    b.lib.db.insert_download({"package_ref": "X.Y.1.var", "download_url": "u"})
    row = b.lib.db.get_download_by_ref("X.Y.1.var")
    b.lib.db.update_download_status(row["id"], "active", temp_path=str(tmp))
    b.start()
    assert b.lib.db.get_download_by_ref("X.Y.1.var")["status"] == "failed" and not tmp.exists()
    b.close()
    b2 = Backend(db_path=vam.db_path, transport=t, cache_dir=tmp_path / "c")
    tmp.write_bytes(b"partial")
    b2.lib.db.retry_download(row["id"])
    b2.lib.db.update_download_status(row["id"], "active", temp_path=str(tmp))
    b2.lib.db.set_setting("downloads_paused", "1")
    b2.start()
    assert b2.downloads.paused and b2.lib.db.get_download_by_ref("X.Y.1.var")["status"] == "queued" and tmp.exists()
    b2.close()


def _flaky_detail(backend, rid: str, times: int, transient: bool) -> dict:
    t, fake = backend.transport_fake, backend.hub_fake
    state = {"left": times}

    def handler(method, url, headers, data):
        body = json.loads(data.decode("utf-8"))
        if body.get("action") == "getResourceDetail" and str(body.get("resource_id")) == rid and state["left"] > 0:
            state["left"] -= 1
            raise TransportError("EOF occurred in violation of protocol", transient=transient)
        return fake._handle(method, url, headers, data)

    rx = re.compile(r"hub\.virtamate\.com/citizenx/api\.php")
    t.routes.insert(0, (lambda m, u: m == "POST" and rx.search(u) is not None, handler))
    return state


def _one_hub_package(backend):
    t, fake = backend.transport_fake, backend.hub_fake
    t.route(
        r"packages\.json", lambda m, u, h, d: (200, {}, json.dumps({"Author.Scene.1.var": 100}).encode()), method="GET"
    )
    fake.details["100"] = {
        "resource_id": "100",
        "title": "Scene Title",
        "type": "Scenes",
        "user_id": "9",
        "username": "Author",
    }
    drop(backend.lib.dirs.main_dir_path(), "Author.Scene.1.var")
    backend.lib.scan()


def test_dropped_connection_is_retried_once(backend):
    _one_hub_package(backend)
    backend.hub.retry_delay = 0
    state = _flaky_detail(backend, "100", times=1, transient=True)
    res = backend.enrich_hub()
    assert state["left"] == 0 and res["failed"] == 0 and res["not_found"] == 0
    assert by_name(backend.lib)["Author.Scene.1.var"]["hub_display_name"] == "Scene Title"


def test_detail_failure_is_not_cached_and_retried_later(backend):
    _one_hub_package(backend)
    state = _flaky_detail(backend, "100", times=99, transient=False)
    res = backend.enrich_hub()
    assert res["failed"] == 1
    row = backend.lib.db.get_hub_resource("100")
    assert row is None or row["hub_json"] is None
    assert [r["filename"] for r in backend.lib.db.get_packages_needing_hub_detail_fetch()] == ["Author.Scene.1.var"]
    assert by_name(backend.lib)["Author.Scene.1.var"]["hub_display_name"] is None
    state["left"] = 0
    out = backend.retry_pending_hub_details()
    assert out == {"pending": 1, "fetched": 1, "not_found": 0, "failed": 0}
    assert by_name(backend.lib)["Author.Scene.1.var"]["hub_display_name"] == "Scene Title"
    assert backend.retry_pending_hub_details()["pending"] == 0


def test_stub_cached_for_a_network_failure_is_retried(backend):
    _one_hub_package(backend)
    backend.lib.db.set_hub_resource_id("Author.Scene.1.var", "100")
    backend.lib.db.upsert_hub_resource_detail("100", {"_unavailable": True, "_error": "Hub unreachable: EOF"})
    backend.lib.db.apply_hub_detail_to_package("Author.Scene.1.var", {"_unavailable": True})
    assert backend.lib.db.get_packages_needing_hub_detail_fetch() == []
    out = backend.retry_pending_hub_details()
    assert out["fetched"] == 1
    assert by_name(backend.lib)["Author.Scene.1.var"]["hub_display_name"] == "Scene Title"
    backend.lib.db.upsert_hub_resource_detail("300", {"_unavailable": True, "_error": "Resource not found."})
    assert backend.retry_pending_hub_details()["pending"] == 0
    assert json.loads(backend.lib.db.get_hub_resource("300")["hub_json"])["_unavailable"]


def test_reinstall_replaces_file_in_place_keeping_state(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    sub = vam.addon_packages / "Creators"
    sub.mkdir()
    p = drop(sub, "Author.Scene.1.var", files={"Custom/Scripts/x.cs": "old"}, marker=True)
    backend.lib.scan()
    before = by_name(backend.lib)["Author.Scene.1.var"]
    assert before["storage_state"] == "disabled"
    data = bytearray(p.read_bytes())
    data[len(data) // 2] ^= 0xFF
    p.write_bytes(bytes(data))
    assert backend.lib.integrity_check()["corrupted_files"] == ["Author.Scene.1.var"]
    fresh = var_bytes("Author.Scene.1.var", files={"Custom/Scripts/x.cs": "new"})
    file_route(t, r"hub/dl/fresh$", fresh)
    fake.find["Author.Scene.1"] = {
        "filename": "Author.Scene.1.var",
        "resource_id": "100",
        "downloadUrl": "https://hub/dl/fresh",
        "file_size": str(len(fresh)),
    }
    assert backend.downloads.enqueue_reinstall("Author.Scene.1.var")["queued"]
    assert wait_for(lambda: backend.downloads.list()[0]["status"] in ("completed", "failed"), 10)
    row = backend.downloads.list()[0]
    assert row["status"] == "completed" and row["reinstall"] == 1, row.get("error")
    assert p.read_bytes() == fresh and not p.with_name(p.name + ".tmp").exists()
    assert (sub / "Author.Scene.1.var.disabled").exists()
    after = by_name(backend.lib)["Author.Scene.1.var"]
    assert after["storage_state"] == "disabled" and after["subpath"] == before["subpath"] and not after["is_corrupted"]
    assert after["is_direct"] == before["is_direct"]
    assert len(by_name(backend.lib)) == 1 and not (vam.addon_packages / "Author.Scene.1.var").exists()


def test_reinstall_keeps_local_file_when_download_is_bad(backend, vam):
    import pytest

    from vambackstage.hub.downloads import DownloadError

    t, fake = backend.transport_fake, backend.hub_fake
    p = drop(vam.addon_packages, "Author.Scene.1.var")
    backend.lib.scan()
    original = p.read_bytes()
    truncated = var_bytes("Author.Scene.1.var", files={"a.txt": "x" * 500})[:-30]
    file_route(t, r"hub/dl/bad$", truncated)
    fake.find["Author.Scene.1"] = {
        "filename": "Author.Scene.1.var",
        "resource_id": "100",
        "downloadUrl": "https://hub/dl/bad",
        "file_size": str(len(truncated)),
    }
    backend.downloads.enqueue_reinstall("Author.Scene.1.var")
    assert wait_for(lambda: backend.downloads.list()[0]["status"] in ("completed", "failed"), 10)
    row = backend.downloads.list()[0]
    assert row["status"] == "failed" and (
        "verification" in (row["error"] or "") or "not a valid package" in (row["error"] or "")
    )
    assert p.read_bytes() == original and not p.with_name(p.name + ".tmp").exists()
    drop(vam.addon_packages, "Offhub.Own.1.var")
    backend.lib.scan()
    with pytest.raises(DownloadError):
        backend.downloads.enqueue_reinstall("Nope.Missing.1.var")
    res = backend.downloads.enqueue_reinstall_many(["Offhub.Own.1.var"])
    assert res["unavailable"] == ["Offhub.Own.1.var"] and res["queued"] == []


# ------------------------------------------------------ review follow-ups


def test_download_that_is_not_a_zip_is_rejected_even_with_matching_size(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.scan()
    junk = b"this is not a zip file" * 40
    file_route(t, r"hub/dl/junk$", junk)
    fake.details["100"] = {
        "resource_id": "100",
        "title": "Junk",
        "type": "Scenes",
        "user_id": "9",
        "username": "Author",
        "hubFiles": [
            {"filename": "Author.Junk.1.var", "urlHosted": "https://hub/dl/junk", "file_size": str(len(junk))}
        ],
    }
    backend.downloads.enqueue_install(resource_id="100")
    assert wait_for(lambda: backend.downloads.list()[0]["status"] in ("completed", "failed"), 10)
    row = backend.downloads.list()[0]
    assert row["status"] == "failed" and "not a valid package" in (row["error"] or "")
    assert (
        not (vam.addon_packages / "Author.Junk.1.var").exists()
        and not (vam.addon_packages / "Author.Junk.1.var.tmp").exists()
    )


def test_hub_names_are_never_used_as_paths(backend):
    assert backend.downloads._insert("../Evil.Pkg.1.var", url="https://x") is False
    assert backend.downloads._insert("sub/Evil.Pkg.1.var", url="https://x") is False
    assert backend.downloads._insert("not-a-package", url="https://x") is False
    assert backend.downloads.list() == []


def test_dropped_connection_while_reading_the_body_is_retried(backend):
    import http.client

    from vambackstage.hub.http import HttpResponse

    t, fake = backend.transport_fake, backend.hub_fake
    fake.details["100"] = {
        "resource_id": "100",
        "title": "Scene Title",
        "type": "Scenes",
        "user_id": "9",
        "username": "Author",
    }
    backend.hub.retry_delay = 0
    real = t.request
    state = {"left": 1}

    class Dropped:
        def read(self):
            raise http.client.IncompleteRead(b"")

    def flaky(method, url, **kw):
        res = real(method, url, **kw)
        if "api.php" in url and state["left"] > 0:
            state["left"] -= 1
            return HttpResponse(200, {}, url, Dropped())
        return res

    t.request = flaky
    detail = backend.hub.get_resource_detail("100")
    assert detail["title"] == "Scene Title" and state["left"] == 0


def _find_entry(url: str, filename: str, size: str) -> dict:
    return {"filename": filename, "resource_id": "200", "downloadUrl": url, "file_size": size}


def _row(backend, ref: str) -> dict | None:
    return next((d for d in backend.downloads.list() if d["package_ref"] == ref), None)


def _settled(backend, ref: str) -> bool:
    row = _row(backend, ref)
    return bool(row and row["status"] in ("completed", "failed"))


def test_download_holds_to_the_server_length_not_hub_metadata(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.scan()
    data = var_bytes("Dep.Lib.5.var")
    file_route(t, r"hub/dl/lib$", data)
    fake.find["Dep.Lib.5"] = _find_entry("https://hub/dl/lib", "Dep.Lib.5.var", str(len(data) // 3))
    assert backend.downloads.enqueue_install_refs(["Dep.Lib.5"])["queued"] == 1
    assert wait_for(lambda: _settled(backend, "Dep.Lib.5.var"))
    row = _row(backend, "Dep.Lib.5.var")
    assert row["status"] == "completed", row.get("error")
    assert row["file_size"] == len(data)
    assert (vam.addon_packages / "Dep.Lib.5.var").read_bytes() == data


def test_download_served_under_another_name_is_kept_under_that_name(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.scan()
    data = var_bytes("Dep.Lib.7.var")
    t.route(
        r"hub/dl/lib$",
        lambda m, u, h, d: (
            200,
            {"Content-Length": str(len(data)), "Content-Disposition": 'filename="Dep.Lib.7.var";'},
            data,
        ),
        method="GET",
    )
    fake.find["Dep.Lib.5"] = _find_entry("https://hub/dl/lib", "Dep.Lib.5.var", "1")
    toasts = []
    backend.events.subscribe("toast", lambda c, p: toasts.append(p["message"]))
    assert backend.downloads.enqueue_install_refs(["Dep.Lib.5"])["queued"] == 1
    assert wait_for(lambda: _settled(backend, "Dep.Lib.7.var"))
    row = _row(backend, "Dep.Lib.7.var")
    assert row["status"] == "completed", row.get("error")
    assert _row(backend, "Dep.Lib.5.var") is None
    assert (vam.addon_packages / "Dep.Lib.7.var").read_bytes() == data and not (
        vam.addon_packages / "Dep.Lib.5.var"
    ).exists()
    pk = by_name(backend.lib)
    assert "Dep.Lib.7.var" in pk and not pk["Dep.Lib.7.var"]["is_direct"]
    assert any("Dep.Lib.7.var instead of Dep.Lib.5.var" in m for m in toasts)
    assert backend.downloads.enqueue_install_refs(["Dep.Lib.5"])["queued"] == 1
    assert wait_for(lambda: _settled(backend, "Dep.Lib.5.var"))
    row = _row(backend, "Dep.Lib.5.var")
    assert row["status"] == "failed" and "already installed" in row["error"]
    assert (
        not (vam.addon_packages / "Dep.Lib.5.var.tmp").exists() and not (vam.addon_packages / "Dep.Lib.5.var").exists()
    )
    assert _row(backend, "Dep.Lib.7.var")["status"] == "completed"


def test_download_of_a_web_page_fails_clearly(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.scan()
    t.route(
        r"hub/dl/gone$",
        lambda m, u, h, d: (
            200,
            {"Content-Type": "text/html; charset=utf-8", "Content-Length": "18"},
            b"<html>login</html>",
        ),
        method="GET",
    )
    fake.find["Dep.Lib.5"] = _find_entry("https://hub/dl/gone", "Dep.Lib.5.var", "18")
    assert backend.downloads.enqueue_install_refs(["Dep.Lib.5"])["queued"] == 1
    assert wait_for(lambda: _settled(backend, "Dep.Lib.5.var"))
    row = _row(backend, "Dep.Lib.5.var")
    assert row["status"] == "failed" and "web page" in row["error"]
    assert not (vam.addon_packages / "Dep.Lib.5.var").exists()


def test_download_cut_short_resumes_where_it_stopped(backend, vam, monkeypatch):
    from vambackstage.hub import downloads as dl_mod

    monkeypatch.setattr(dl_mod, "RETRY_BASE_DELAY", 0.01)
    t, fake = backend.transport_fake, backend.hub_fake
    backend.lib.scan()
    data = var_bytes("Dep.Lib.5.var")
    cut = len(data) - 100

    def handler(method, url, headers, body):
        rng = headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return (
                206,
                {
                    "Content-Length": str(len(data) - start),
                    "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}",
                },
                data[start:],
            )
        return 200, {"Content-Length": str(len(data))}, data[:cut]

    t.route(r"hub/dl/lib$", handler, method="GET")
    fake.find["Dep.Lib.5"] = _find_entry("https://hub/dl/lib", "Dep.Lib.5.var", str(len(data)))
    assert backend.downloads.enqueue_install_refs(["Dep.Lib.5"])["queued"] == 1
    assert wait_for(lambda: _settled(backend, "Dep.Lib.5.var"))
    row = _row(backend, "Dep.Lib.5.var")
    assert row["status"] == "completed", row.get("error")
    assert (vam.addon_packages / "Dep.Lib.5.var").read_bytes() == data
    assert t.count(r"hub/dl/lib") == 2


def test_hub_download_replaces_a_stand_in(backend, vam):
    t, fake = backend.transport_fake, backend.hub_fake
    drop(vam.addon_packages, "Author.Scene.1.var", deps=["Dep.Lib.2"])
    backend.lib.scan()
    assert backend.lib.create_dummies(["Dep.Lib.2", "Author.Thing.1", "Deep.Core.1"])["created"] == [
        "Dep.Lib.2.var",
        "Author.Thing.1.var",
        "Deep.Core.1.var",
    ]
    real = var_bytes("Dep.Lib.2.var", files={"Custom/Scripts/real.cs": "x"})
    file_route(t, r"hub/dl/lib$", real)
    fake.find["Dep.Lib.2"] = {
        "filename": "Dep.Lib.2.var",
        "resource_id": "200",
        "downloadUrl": "https://hub/dl/lib",
        "file_size": str(len(real)),
    }
    res = backend.downloads.enqueue_install_refs(["Dep.Lib.2"])
    assert res["queued"] == 1 and res["already_installed"] == []
    assert wait_for(lambda: _settled(backend, "Dep.Lib.2.var"))
    row = _row(backend, "Dep.Lib.2.var")
    assert row["status"] == "completed", row.get("error")
    pk = by_name(backend.lib)
    assert not pk["Dep.Lib.2.var"]["is_dummy"] and not pk["Dep.Lib.2.var"]["is_direct"]
    assert (vam.addon_packages / "Dep.Lib.2.var").read_bytes() == real
    assert (
        not (vam.addon_packages / "Dummies" / "Dep.Lib.2.var").exists()
        and not (vam.addon_packages / "Dep.Lib.2.var.tmp").exists()
    )
    assert pk["Author.Scene.1.var"]["missing_deps"] == 0
    thing = var_bytes("Author.Thing.1.var", deps=["Deep.Core.1"])
    core = var_bytes("Deep.Core.1.var")
    file_route(t, r"hub/dl/thing$", thing)
    file_route(t, r"hub/dl/core$", core)
    fake.details["300"] = {
        "resource_id": "300",
        "title": "Thing",
        "type": "Scenes",
        "user_id": "9",
        "username": "Author",
        "hubFiles": [
            {"filename": "Author.Thing.1.var", "urlHosted": "https://hub/dl/thing", "file_size": str(len(thing))}
        ],
        "dependencies": {
            "Author.Thing": [
                {
                    "packageName": "Deep.Core",
                    "filename": "Deep.Core.1",
                    "latest_version": "1",
                    "resource_id": "400",
                    "downloadUrl": "https://hub/dl/core",
                    "file_size": str(len(core)),
                }
            ]
        },
    }
    res = backend.downloads.enqueue_install(resource_id="300")
    assert res["inserted"] == 1 and res["already_local"] == 0
    assert wait_for(lambda: _settled(backend, "Author.Thing.1.var") and _settled(backend, "Deep.Core.1.var"))
    assert (
        _row(backend, "Author.Thing.1.var")["status"] == "completed"
        and _row(backend, "Deep.Core.1.var")["status"] == "completed"
    )
    pk = by_name(backend.lib)
    assert pk["Author.Thing.1.var"]["is_direct"] and not pk["Author.Thing.1.var"]["is_dummy"]
    assert not pk["Deep.Core.1.var"]["is_dummy"] and not pk["Deep.Core.1.var"]["is_direct"]
    assert backend.lib.placeholder_counts() == {"dummies": 0, "aliases": 0}
    assert pk["Author.Thing.1.var"]["missing_deps"] == 0


def test_name_lookup_rejects_the_hubs_echo_of_the_query():
    dress = {
        "resource_id": "1",
        "title": "Bayonetta Dress",
        "hubFiles": [{"filename": "Skynet.BayonettaDress.1.var"}],
        "dependencies": {
            "Skynet.Bayonetta.latest": [{"packageName": "Skynet.Bayonetta", "filename": "Skynet.Bayonetta.1"}]
        },
    }
    assert resource_detail_matches_package_name(dress, "Skynet.BayonettaDress") is True
    assert resource_detail_matches_package_name(dress, "Skynet.Bayonetta") is False
    paid = {"resource_id": "2", "title": "Bayonetta", "dependencies": {"Skynet.Bayonetta.latest": []}}
    assert resource_detail_matches_package_name(paid, "Skynet.Bayonetta") is True
    assert resource_detail_matches_package_name(dict(paid, title="Bayonetta Dress"), "Skynet.Bayonetta") is False
    assert (
        resource_detail_matches_package_name(
            {"resource_id": "3", "title": "x", "dependencies": {"Skynet.Bayonetta": []}}, "Skynet.Bayonetta"
        )
        is True
    )
    assert title_fits_package("Bayonetta Dress", "Skynet.BayonettaDress") and not title_fits_package(
        "Bayonetta Dress", "Skynet.Bayonetta"
    )


def test_multi_file_resource_title_names_only_the_package_it_fits(backend, vam):
    t = backend.transport_fake
    data = {"Skynet.BayonettaDress.1.var": 7, "Skynet.Bayonetta.1.var": 7}
    t.route(
        r"s3cdn\.virtamate\.com/data/packages\.json",
        lambda m, u, h, b: (200, {"ETag": '"e"'}, json.dumps(data).encode()),
        method="GET",
    )
    backend.hub_fake.details["7"] = {
        "resource_id": "7",
        "title": "Bayonetta Dress",
        "type": "Clothing",
        "user_id": "5",
        "username": "Skynet",
        "hubFiles": [{"filename": "Skynet.BayonettaDress.1.var"}, {"filename": "Skynet.Bayonetta.1.var"}],
        "dependencies": {"Skynet.BayonettaDress": [], "Skynet.Bayonetta": []},
    }
    drop(vam.addon_packages, "Skynet.BayonettaDress.1.var")
    drop(vam.addon_packages, "Skynet.Bayonetta.1.var", files={"Saves/scene/Bayonetta.json": "{}"})
    backend.lib.scan()
    backend.enrich_hub()
    pk = by_name(backend.lib)
    assert (
        pk["Skynet.BayonettaDress.1.var"]["hub_resource_id"] == "7"
        and pk["Skynet.Bayonetta.1.var"]["hub_resource_id"] == "7"
    )
    assert pk["Skynet.BayonettaDress.1.var"]["hub_display_name"] == "Bayonetta Dress"
    assert pk["Skynet.Bayonetta.1.var"]["hub_display_name"] is None


def test_mismatched_hub_links_are_dropped_and_looked_up_again(backend, vam):
    drop(vam.addon_packages, "Skynet.Bayonetta.1.var")
    backend.lib.scan()
    dress = {
        "resource_id": "1",
        "title": "Bayonetta Dress",
        "type": "Clothing",
        "user_id": "5",
        "username": "Skynet",
        "hubFiles": [{"filename": "Skynet.BayonettaDress.1.var"}],
        "dependencies": {"Skynet.Bayonetta.latest": []},
    }
    backend.lib.db.upsert_hub_resource_detail("1", dress)
    backend.lib.db.set_hub_resource_id("Skynet.Bayonetta.1.var", "1")
    backend.lib.db.apply_hub_detail_to_package("Skynet.Bayonetta.1.var", dress)
    backend.lib.load()
    assert by_name(backend.lib)["Skynet.Bayonetta.1.var"]["hub_resource_id"] == "1"
    backend.hub_fake.by_name["Skynet.Bayonetta"] = {
        "resource_id": "2",
        "title": "Bayonetta",
        "type": "Looks",
        "user_id": "5",
        "username": "Skynet",
        "dependencies": {"Skynet.Bayonetta.latest": []},
    }
    assert backend.retry_pending_hub_details()["pending"] == 0
    pk = by_name(backend.lib)
    assert (
        pk["Skynet.Bayonetta.1.var"]["hub_resource_id"] is None
        and pk["Skynet.Bayonetta.1.var"]["hub_display_name"] is None
    )
    t = backend.transport_fake
    t.route(
        r"s3cdn\.virtamate\.com/data/packages\.json",
        lambda m, u, h, b: (200, {"ETag": '"e"'}, b'{"Other.Pkg.1.var": 9}'),
        method="GET",
    )
    backend.enrich_hub()
    pk = by_name(backend.lib)
    assert (
        pk["Skynet.Bayonetta.1.var"]["hub_resource_id"] == "2"
        and pk["Skynet.Bayonetta.1.var"]["hub_display_name"] == "Bayonetta"
    )
