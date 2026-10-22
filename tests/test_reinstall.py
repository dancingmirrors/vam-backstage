import os
import re
import time

import pytest

from vambackstage.backend import Backend
from vambackstage.hub import downloads as dl_mod
from vambackstage.hub.client import HubError
from vambackstage.hub.downloads import DownloadError
from vambackstage.hub.http import TransportError

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


def serve(
    backend, filename: str, deps=None, key: str | None = None, downloadable: bool = True, data: bytes | None = None
) -> bytes:
    if data is None:
        data = var_bytes(filename, deps=deps, files={"Custom/Scripts/fresh.cs": f"// fresh {filename}"})
    url = f"https://hub/dl/{filename}"
    file_route(backend.transport_fake, re.escape(url) + "$", data)
    backend.hub_fake.find[key or filename[:-4]] = {
        "filename": filename,
        "resource_id": "500",
        "downloadUrl": url if downloadable else "",
        "file_size": str(len(data)),
    }
    return data


def settled(backend, n: int) -> bool:
    rows = backend.downloads.list()
    return len(rows) == n and all(r["status"] in ("completed", "failed", "cancelled") for r in rows)


def content_ids(backend, filename: str) -> set[int]:
    return {c["id"] for c in backend.lib.db.get_all_contents() if c["package_filename"] == filename}


def toasts_of(backend) -> list[str]:
    seen: list[str] = []
    backend.events.subscribe("toast", lambda _c, p: seen.append(p["message"]))
    return seen


def test_dependencies_come_back_at_the_versions_the_scene_asks_for(backend, vam):
    main = vam.addon_packages
    drop(
        main,
        "Author.Scene.1.var",
        deps=["Dep.Lib.3", "Plug.In.latest", "Morph.Pack.min2", "Gone.Thing.1", "Miss.Ing.2"],
    )
    drop(main, "Dep.Lib.5.var", deps=["Deep.Core.1"])
    drop(main, "Deep.Core.1.var")
    drop(main, "Plug.In.2.var")
    drop(main, "Morph.Pack.4.var")
    gone = drop(main, "Gone.Thing.1.var")
    backend.lib.scan()
    gone_bytes = gone.read_bytes()
    scene = serve(
        backend,
        "Author.Scene.1.var",
        deps=["Dep.Lib.3", "Plug.In.latest", "Morph.Pack.min2", "Gone.Thing.1", "Miss.Ing.2"],
    )
    lib3 = serve(backend, "Dep.Lib.3.var")
    serve(backend, "Dep.Lib.5.var", deps=["Deep.Core.1"])
    serve(backend, "Deep.Core.1.var")
    plug = serve(backend, "Plug.In.2.var")
    morph = serve(backend, "Morph.Pack.4.var")
    serve(backend, "Miss.Ing.4.var", key="Miss.Ing.2")

    plan = backend.downloads.plan_reinstall("Author.Scene.1.var")
    files = {r["filename"]: r for r in plan["files"]}
    refs = {r["ref"]: r for r in plan["refs"]}
    assert files["Author.Scene.1.var"]["action"] == "reinstall" and files["Author.Scene.1.var"]["is_root"]
    assert files["Plug.In.2.var"]["action"] == "reinstall" and files["Morph.Pack.4.var"]["action"] == "reinstall"
    assert files["Gone.Thing.1.var"]["action"] == "keep" and files["Gone.Thing.1.var"]["reason"] == "not_on_hub"
    assert (
        "Dep.Lib.5.var" not in files and "Deep.Core.1.var" not in files
    ), "the stand-in is not what the scene asks for"
    assert refs["Dep.Lib.3"]["action"] == "install" and refs["Dep.Lib.3"]["kind"] == "requested"
    assert refs["Dep.Lib.3"]["stand_in"] == "Dep.Lib.5.var" and refs["Dep.Lib.3"]["stand_in_kind"] == "fallback"
    assert refs["Miss.Ing.2"]["kind"] == "substitute" and refs["Miss.Ing.2"]["filename"] == "Miss.Ing.4.var"
    assert set(refs) == {"Dep.Lib.3", "Miss.Ing.2"}
    order = [s["filename"] for s in plan["downloads"]]
    assert order == ["Dep.Lib.3.var", "Miss.Ing.4.var", "Author.Scene.1.var", "Plug.In.2.var", "Morph.Pack.4.var"]
    assert plan["total_size"] == sum(s["file_size"] for s in plan["downloads"]) and plan["lookup_failed"] == 0
    assert all(
        s["parent_ref"] == "Author.Scene.1.var" for s in plan["downloads"] if s["filename"] != "Author.Scene.1.var"
    )

    toasts = toasts_of(backend)
    res = backend.downloads.enqueue_reinstall_plan(plan)
    assert res["queued"] == order and res["skipped"] == []
    assert wait_for(lambda: settled(backend, 5), 15)
    rows = {r["package_ref"]: r for r in backend.downloads.list()}
    assert all(r["status"] == "completed" for r in rows.values()), [r.get("error") for r in rows.values()]
    assert rows["Plug.In.2.var"]["reinstall"] == 1 and rows["Dep.Lib.3.var"]["reinstall"] == 0
    assert (main / "Dep.Lib.3.var").read_bytes() == lib3 and (main / "Dep.Lib.5.var").exists()
    assert (main / "Author.Scene.1.var").read_bytes() == scene
    assert (main / "Plug.In.2.var").read_bytes() == plug and (main / "Morph.Pack.4.var").read_bytes() == morph
    assert (main / "Gone.Thing.1.var").read_bytes() == gone_bytes
    pk = by_name(backend.lib)
    assert "Miss.Ing.4.var" in pk and not pk["Dep.Lib.3.var"]["is_direct"]
    edges = {d.ref: d for d in backend.lib.store.forward_deps["Author.Scene.1.var"]}
    assert edges["Dep.Lib.3"].resolved == "Dep.Lib.3.var" and edges["Dep.Lib.3"].resolution == "exact"
    assert wait_for(lambda: any("Reinstall finished: 5 package(s) downloaded" in m for m in toasts), 5)
    assert not any(m.startswith("Download failed") for m in toasts)


def test_a_stand_in_is_reinstalled_and_followed_when_the_asked_version_is_gone(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Dep.Lib.3"])
    drop(main, "Dep.Lib.5.var", deps=["Deep.Core.1"])
    drop(main, "Deep.Core.1.var")
    backend.lib.scan()
    serve(backend, "Dep.Lib.5.var", deps=["Deep.Core.1"])
    serve(backend, "Deep.Core.1.var")
    backend.hub_fake.find["Dep.Lib.3"] = backend.hub_fake.find["Dep.Lib.5"]

    plan = backend.downloads.plan_reinstall(["Author.Scene.1.var"])
    files = {r["filename"]: r for r in plan["files"]}
    (ref,) = plan["refs"]
    assert ref["ref"] == "Dep.Lib.3" and ref["action"] == "unavailable" and ref["reason"] == "not_on_hub"
    assert ref["offers"] == "Dep.Lib.5.var" and ref["stand_in"] == "Dep.Lib.5.var"
    assert files["Author.Scene.1.var"]["action"] == "keep" and files["Author.Scene.1.var"]["reason"] == "not_on_hub"
    assert files["Dep.Lib.5.var"]["action"] == "reinstall" and files["Deep.Core.1.var"]["action"] == "reinstall"
    assert files["Deep.Core.1.var"]["needed_by"] == ["Dep.Lib.5.var"]
    assert [s["filename"] for s in plan["downloads"]] == ["Dep.Lib.5.var", "Deep.Core.1.var"]


def test_flexible_references(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Morph.Pack.min3", "Plug.In.latest", "Skin.Set.min2"])
    drop(main, "Morph.Pack.2.var")
    backend.lib.scan()
    serve(backend, "Morph.Pack.3.var")
    serve(backend, "Morph.Pack.6.var", key="Morph.Pack.min3")
    serve(backend, "Plug.In.7.var", key="Plug.In.latest")
    serve(backend, "Skin.Set.5.var", key="Skin.Set.min2")
    refs = {r["ref"]: r for r in backend.downloads.plan_reinstall("Author.Scene.1.var")["refs"]}
    assert refs["Morph.Pack.min3"]["filename"] == "Morph.Pack.3.var", "the minimum asked for, not the newest"
    assert refs["Morph.Pack.min3"]["stand_in"] == "Morph.Pack.2.var"
    assert refs["Plug.In.latest"]["filename"] == "Plug.In.7.var" and refs["Plug.In.latest"]["kind"] == "requested"
    assert refs["Skin.Set.min2"]["filename"] == "Skin.Set.5.var" and refs["Skin.Set.min2"]["kind"] == "requested"
    del backend.hub_fake.find["Morph.Pack.3"]
    refs = {r["ref"]: r for r in backend.downloads.plan_reinstall("Author.Scene.1.var")["refs"]}
    assert refs["Morph.Pack.min3"]["filename"] == "Morph.Pack.6.var" and refs["Morph.Pack.min3"]["kind"] == "requested"


def test_paid_and_unknown_references_are_explained(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Paid.Look.2", "No.Where.1", "Paid.Plugin.latest", "Gone.Version.3"])
    drop(main, "Gone.Version.1.var")
    backend.lib.scan()
    serve(backend, "Paid.Look.2.var", downloadable=False)
    serve(backend, "Paid.Plugin.4.var", key="Paid.Plugin.latest", downloadable=False)
    serve(backend, "Gone.Version.9.var", key="Gone.Version.3")
    refs = {r["ref"]: r for r in backend.downloads.plan_reinstall("Author.Scene.1.var")["refs"]}
    assert refs["Paid.Look.2"]["action"] == "unavailable" and refs["Paid.Look.2"]["reason"] == "no_download"
    assert refs["Paid.Plugin.latest"]["reason"] == "no_download"
    assert refs["No.Where.1"]["reason"] == "not_on_hub" and refs["No.Where.1"]["offers"] is None
    gone = refs["Gone.Version.3"]
    assert (
        gone["action"] == "unavailable" and gone["offers"] == "Gone.Version.9.var"
    ), "a stand-in exists: no substitute"


def test_a_failed_lookup_batch_is_skipped_not_fatal(backend, vam, monkeypatch):
    monkeypatch.setattr(dl_mod, "PLAN_LOOKUP_BATCH", 1)
    backend.hub.retry_delay = 0
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Dep.Lib.1", "Flaky.Pkg.1"])
    drop(main, "Dep.Lib.1.var")
    drop(main, "Flaky.Pkg.1.var")
    backend.lib.scan()
    serve(backend, "Author.Scene.1.var", deps=["Dep.Lib.1", "Flaky.Pkg.1"])
    serve(backend, "Dep.Lib.1.var")
    t, fake = backend.transport_fake, backend.hub_fake

    def handler(method, url, headers, data):
        if b"Flaky.Pkg" in data:
            raise TransportError("connection reset", transient=False)
        return fake._handle(method, url, headers, data)

    rx = re.compile(r"hub\.virtamate\.com/citizenx/api\.php")
    t.routes.insert(0, (lambda m, u: m == "POST" and rx.search(u) is not None, handler))
    progress = []
    plan = backend.downloads.plan_reinstall("Author.Scene.1.var", on_progress=progress.append)
    files = {r["filename"]: r for r in plan["files"]}
    assert files["Flaky.Pkg.1.var"]["reason"] == "lookup_failed" and plan["lookup_failed"] == 1
    assert files["Dep.Lib.1.var"]["action"] == "reinstall" and files["Author.Scene.1.var"]["action"] == "reinstall"
    assert progress[-1] == {"status": "lookup", "step": 3, "total": 3}

    t.routes.insert(0, (lambda m, u: m == "POST" and rx.search(u) is not None, lambda *a: (500, {}, b"down")))
    with pytest.raises(HubError):
        backend.downloads.plan_reinstall("Author.Scene.1.var")
    with pytest.raises(DownloadError):
        backend.downloads.plan_reinstall("Not.There.1.var")
    cancel = type("Cancel", (), {"is_set": lambda self: True})()
    assert backend.downloads.plan_reinstall("Author.Scene.1.var", cancel=cancel) == {"cancelled": True}


def test_many_roots_share_one_plan_and_unpacked_edits_are_kept(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Dep.Lib.1"])
    drop(main, "Other.Scene.1.var", deps=["Dep.Lib.1"])
    drop(main, "Dep.Lib.1.var")
    backend.lib.scan()
    for fn in ("Author.Scene.1.var", "Other.Scene.1.var"):
        serve(backend, fn, deps=["Dep.Lib.1"])
    serve(backend, "Dep.Lib.1.var")
    backend.lib.unpack_package("Dep.Lib.1.var")
    plan = backend.downloads.plan_reinstall(["Author.Scene.1.var", "Other.Scene.1.var", "Dep.Lib.1.var"])
    files = {r["filename"]: r for r in plan["files"]}
    assert files["Dep.Lib.1.var"]["action"] == "keep" and files["Dep.Lib.1.var"]["reason"] == "unpacked"
    assert files["Dep.Lib.1.var"]["is_root"] and files["Dep.Lib.1.var"]["needed_by"] == [
        "Author.Scene.1.var",
        "Other.Scene.1.var",
    ]
    assert [s["filename"] for s in plan["downloads"]] == ["Author.Scene.1.var", "Other.Scene.1.var"]
    assert all(s["parent_ref"] is None and s["priority"] == "dependency" for s in plan["downloads"])
    alone = backend.downloads.plan_reinstall("Dep.Lib.1.var")
    assert [s["filename"] for s in alone["downloads"]] == ["Dep.Lib.1.var"], "picked on its own, the folder goes"


def test_loose_scene_references(backend, vam):
    scene = vam.saves_dir / "scene" / "mine.json"
    scene.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text('{"atoms":[{"id":"Dep.Lib.3:/Custom/Atom/Person/Appearance/x.vap"},{"p":"Plug.In.latest:/x.cs"}]}')
    drop(vam.addon_packages, "Dep.Lib.5.var")
    drop(vam.addon_packages, "Plug.In.2.var")
    backend.lib.scan()
    serve(backend, "Dep.Lib.3.var")
    serve(backend, "Plug.In.2.var")
    plan = backend.downloads.plan_reinstall(local_paths=["Saves/scene/mine.json.disabled"])
    refs = {r["ref"]: r for r in plan["refs"]}
    assert refs["Dep.Lib.3"]["filename"] == "Dep.Lib.3.var" and refs["Dep.Lib.3"]["needed_by"] == [
        "Saves/scene/mine.json"
    ]
    assert [r["filename"] for r in plan["files"]] == ["Plug.In.2.var"]
    assert {s["parent_ref"] for s in plan["downloads"]} == {"Saves/scene/mine.json"}
    everything = backend.downloads.plan_reinstall("__local__")
    assert {s["filename"] for s in everything["downloads"]} == {"Dep.Lib.3.var", "Plug.In.2.var"}
    assert {s["parent_ref"] for s in everything["downloads"]} == {"Local content"}


def test_placeholders_are_replaced_and_the_queue_keeps_moving(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Dep.Lib.2", "Other.Pkg.1"])
    backend.lib.scan()
    assert backend.lib.create_dummies(["Dep.Lib.2"])["created"] == ["Dep.Lib.2.var"]
    real = serve(backend, "Dep.Lib.2.var")
    serve(backend, "Other.Pkg.1.var")
    backend.downloads.max_concurrent = 1
    plan = backend.downloads.plan_reinstall("Author.Scene.1.var")
    refs = {r["ref"]: r for r in plan["refs"]}
    assert refs["Dep.Lib.2"]["stand_in_kind"] == "placeholder" and refs["Dep.Lib.2"]["kind"] == "requested"
    assert [s["filename"] for s in plan["downloads"]] == ["Dep.Lib.2.var", "Other.Pkg.1.var"]
    toasts = toasts_of(backend)
    backend.downloads.enqueue_reinstall_plan(plan)
    assert wait_for(lambda: settled(backend, 2), 15), backend.downloads.list()
    assert (main / "Dep.Lib.2.var").read_bytes() == real and backend.lib.placeholder_counts()["dummies"] == 0
    assert "Other.Pkg.1.var" in by_name(backend.lib)
    assert wait_for(lambda: "Reinstall finished: 2 package(s) downloaded" in toasts, 5), toasts
    assert not any("replaced the dummy" in m for m in toasts)


def test_only_missing_and_quiet_failures(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Dep.Lib.3", "Plug.In.2"])
    drop(main, "Dep.Lib.5.var")
    drop(main, "Plug.In.2.var")
    backend.lib.scan()
    serve(backend, "Plug.In.2.var")
    backend.hub_fake.find["Dep.Lib.3"] = {
        "filename": "Dep.Lib.3.var",
        "resource_id": "7",
        "downloadUrl": "https://hub/dl/broken",
        "file_size": "10",
    }
    backend.transport_fake.route(r"hub/dl/broken$", lambda m, u, h, d: (404, {}, b""), method="GET")
    plan = backend.downloads.plan_reinstall("Author.Scene.1.var")
    assert [s["filename"] for s in plan["downloads"]] == ["Dep.Lib.3.var", "Plug.In.2.var"]
    toasts = toasts_of(backend)
    res = backend.downloads.enqueue_reinstall_plan(plan, only_missing=True)
    assert res["queued"] == ["Dep.Lib.3.var"]
    assert wait_for(lambda: settled(backend, 1), 10)
    assert backend.downloads.list()[0]["status"] == "failed" and "404" in backend.downloads.list()[0]["error"]
    assert wait_for(lambda: any("1 failed" in m for m in toasts), 5), toasts
    assert not any(m.startswith("Download failed") for m in toasts)
    backend.downloads.retry(backend.downloads.list()[0]["id"])
    assert wait_for(lambda: any(m.startswith("Download failed") for m in toasts), 10), "a retry by hand is not quiet"


def test_files_already_in_the_queue_are_skipped(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Plug.In.2"])
    drop(main, "Plug.In.2.var")
    backend.lib.scan()
    serve(backend, "Author.Scene.1.var", deps=["Plug.In.2"])
    serve(backend, "Plug.In.2.var")
    backend.downloads.pause_all()
    backend.downloads.enqueue_reinstall("Plug.In.2.var")
    res = backend.downloads.enqueue_reinstall_plan(backend.downloads.plan_reinstall("Author.Scene.1.var"))
    assert res == {"queued": ["Author.Scene.1.var"], "skipped": ["Plug.In.2.var"], "paused": True}
    quiet = {r["package_ref"]: r["quiet"] for r in backend.downloads.list()}
    assert quiet == {"Plug.In.2.var": 0, "Author.Scene.1.var": 1}, "the download already queued stays as it was"
    assert backend.lib.db.has_pending_quiet_downloads()
    backend.downloads.cancel_all()
    assert not backend.lib.db.has_pending_quiet_downloads()


def test_the_summary_waits_for_every_quiet_download(backend):
    mgr = backend.downloads
    mgr.pause_all()
    with mgr._lock:
        assert mgr._settle_quiet({"id": 7, "quiet": 1}, ok=True) and not mgr._settle_quiet({"id": 9}, ok=False)
        mgr._quiet_running.add(8)
        assert mgr._take_quiet_summary() is None, "download 8 is still downloading or being indexed"
        mgr._quiet_running.discard(8)
    mgr._insert("Late.Dep.1.var", url="https://hub/x", quiet=True)
    with mgr._lock:
        mgr._settle_quiet({"id": 8, "quiet": 1, "substituted_for": "A.B.3.var"}, ok=True)
        assert mgr._take_quiet_summary() is None, "a dependency it queued is still waiting"
    toasts = toasts_of(backend)
    mgr.cancel(mgr.list()[0]["id"])
    assert toasts == ["Reinstall finished: 2 package(s) downloaded, 1 served as another version than the one asked for"]
    with mgr._lock:
        assert mgr._take_quiet_summary() is None


def test_fetching_a_stand_in_brings_its_dependencies_quietly(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var")
    backend.lib.scan()
    assert backend.lib.create_dummies(["Foo.Bar.3"])["created"] == ["Foo.Bar.3.var"]
    real = serve(backend, "Foo.Bar.3.var", deps=["Baz.Qux.2", "Bad.Dep.1"])
    serve(backend, "Author.Scene.1.var")
    serve(backend, "Baz.Qux.2.var")
    backend.hub_fake.find["Bad.Dep.1"] = {
        "filename": "Bad.Dep.1.var",
        "resource_id": "9",
        "downloadUrl": "https://hub/dl/bad",
        "file_size": "10",
    }
    backend.transport_fake.route(r"hub/dl/bad$", lambda m, u, h, d: (404, {}, b""), method="GET")
    plan = backend.downloads.plan_reinstall(["Foo.Bar.3.var", "Author.Scene.1.var"])
    spec = {s["filename"]: s for s in plan["downloads"]}["Foo.Bar.3.var"]
    assert spec["reinstall"] is False and spec["display_name"] is None
    toasts = toasts_of(backend)
    res = backend.downloads.enqueue_reinstall_plan(plan, only_missing=True)
    assert res["queued"] == ["Foo.Bar.3.var"], "the real package is what is missing, so it counts"
    assert wait_for(lambda: settled(backend, 3), 15), backend.downloads.list()
    assert (main / "Foo.Bar.3.var").read_bytes() == real and "Baz.Qux.2.var" in by_name(backend.lib)
    assert wait_for(lambda: any(m.startswith("Reinstall finished") for m in toasts), 5), toasts
    summary = next(m for m in toasts if m.startswith("Reinstall finished"))
    assert summary.startswith("Reinstall finished: 2 downloaded, 1 failed"), summary
    assert not any(m.startswith("Download failed") or "replaced the dummy" in m for m in toasts), toasts


def test_a_different_version_served_is_counted_not_toasted(backend, vam):
    drop(vam.addon_packages, "Author.Scene.1.var", deps=["Miss.Ing.2"])
    backend.lib.scan()
    data = var_bytes("Miss.Ing.4.var")
    backend.transport_fake.route(
        r"hub/dl/missing$",
        lambda m, u, h, d: (
            200,
            {"Content-Length": str(len(data)), "Content-Disposition": 'filename="Miss.Ing.4.var";'},
            data,
        ),
        method="GET",
    )
    backend.hub_fake.find["Miss.Ing.2"] = {
        "filename": "Miss.Ing.2.var",
        "resource_id": "9",
        "downloadUrl": "https://hub/dl/missing",
        "file_size": str(len(data)),
    }
    toasts = toasts_of(backend)
    backend.downloads.enqueue_reinstall_plan(backend.downloads.plan_reinstall("Author.Scene.1.var"))
    assert wait_for(lambda: settled(backend, 1), 10)
    assert "Miss.Ing.4.var" in by_name(backend.lib)
    expected = "Reinstall finished: 1 package(s) downloaded, 1 served as another version than the one asked for"
    assert wait_for(lambda: expected in toasts, 5), toasts
    assert not any(m.startswith("The Hub served") for m in toasts)


def test_stop_is_honoured_after_the_last_lookup(backend, vam):
    import threading

    drop(vam.addon_packages, "Author.Scene.1.var", deps=["Dep.Lib.1"])
    drop(vam.addon_packages, "Dep.Lib.1.var")
    backend.lib.scan()
    serve(backend, "Author.Scene.1.var", deps=["Dep.Lib.1"])
    cancel = threading.Event()
    t, fake = backend.transport_fake, backend.hub_fake

    def stop_while_asking(method, url, headers, data):
        cancel.set()
        return fake._handle(method, url, headers, data)

    rx = re.compile(r"hub\.virtamate\.com/citizenx/api\.php")
    t.routes.insert(0, (lambda m, u: m == "POST" and rx.search(u) is not None, stop_while_asking))
    assert backend.downloads.plan_reinstall("Author.Scene.1.var", cancel=cancel) == {"cancelled": True}


def test_reinstall_falls_back_to_the_resource_listing(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Dep.Lib.1"])
    drop(main, "Dep.Lib.1.var")
    backend.lib.scan()
    for fn, rid in (("Author.Scene.1.var", "100"), ("Dep.Lib.1.var", "200")):
        backend.lib.db.set_hub_resource_id(fn, rid)
        data = var_bytes(fn, deps=["Dep.Lib.1"] if fn.startswith("Author") else None)
        file_route(backend.transport_fake, rf"hub/res/{rid}$", data)
        backend.hub_fake.details[rid] = {
            "resource_id": rid,
            "title": fn,
            "hubFiles": [{"filename": fn, "urlHosted": f"https://hub/res/{rid}", "file_size": str(len(data))}],
        }
    backend.lib.load()
    backend.hub_fake.find["Dep.Lib.1"] = {
        "filename": "Dep.Lib.1.var",
        "resource_id": "200",
        "downloadUrl": "https://hub/x?file=",
    }
    plan = backend.downloads.plan_reinstall("Author.Scene.1.var")
    assert {s["filename"]: s["url"] for s in plan["downloads"]} == {
        "Author.Scene.1.var": "https://hub/res/100",
        "Dep.Lib.1.var": "https://hub/res/200",
    }
    del backend.hub_fake.details["200"]
    backend.hub.invalidate_caches()
    plan = backend.downloads.plan_reinstall("Author.Scene.1.var")
    files = {r["filename"]: r for r in plan["files"]}
    assert files["Dep.Lib.1.var"]["action"] == "keep" and files["Dep.Lib.1.var"]["reason"] == "no_download"


def test_an_identical_download_leaves_the_local_file_alone(backend, vam):
    main = vam.addon_packages
    drop(main, "Author.Scene.1.var", deps=["Plug.In.2"])
    plug = drop(main, "Plug.In.2.var")
    os.utime(plug, (1_600_000_000, 1_600_000_000))
    backend.lib.scan()
    ids = content_ids(backend, "Plug.In.2.var")
    scene = serve(backend, "Author.Scene.1.var", deps=["Plug.In.2"])
    serve(backend, "Plug.In.2.var", data=plug.read_bytes())
    toasts = toasts_of(backend)
    backend.downloads.enqueue_reinstall_plan(backend.downloads.plan_reinstall("Author.Scene.1.var"))
    assert wait_for(lambda: settled(backend, 2), 10)
    assert all(r["status"] == "completed" for r in backend.downloads.list())
    assert (main / "Author.Scene.1.var").read_bytes() == scene
    assert plug.stat().st_mtime == 1_600_000_000 and not (main / "Plug.In.2.var.tmp").exists()
    assert content_ids(backend, "Plug.In.2.var") == ids, "the identical copy is not rescanned"
    assert wait_for(lambda: toasts, 5)
    assert toasts == ["Reinstall finished: 2 package(s) downloaded, 1 identical to the copy you had"]


def test_reinstalling_one_identical_package_says_so(backend, vam):
    plug = drop(vam.addon_packages, "Plug.In.2.var")
    backend.lib.scan()
    ids = content_ids(backend, "Plug.In.2.var")
    serve(backend, "Plug.In.2.var", data=plug.read_bytes())
    toasts = toasts_of(backend)
    backend.downloads.enqueue_reinstall("Plug.In.2.var")
    assert wait_for(lambda: settled(backend, 1), 10)
    assert backend.downloads.list()[0]["status"] == "completed"
    assert toasts == ["Plug.In.2.var already matched the Hub's file, so it was left as it is"]
    assert content_ids(backend, "Plug.In.2.var") == ids


def test_a_copy_flagged_corrupt_is_reinstalled_even_when_identical(backend, vam):
    plug = drop(vam.addon_packages, "Plug.In.2.var")
    backend.lib.scan()
    backend.lib.db.set_package_corrupted("Plug.In.2.var", True)
    backend.lib.load()
    ids = content_ids(backend, "Plug.In.2.var")
    serve(backend, "Plug.In.2.var", data=plug.read_bytes())
    toasts = toasts_of(backend)
    backend.downloads.enqueue_reinstall("Plug.In.2.var")
    assert wait_for(lambda: settled(backend, 1), 10)
    assert backend.downloads.list()[0]["status"] == "completed"
    assert not by_name(backend.lib)["Plug.In.2.var"]["is_corrupted"]
    assert content_ids(backend, "Plug.In.2.var") != ids, "it went through the full reinstall"
    assert not any("already matched" in m for m in toasts)


def test_a_stale_record_of_the_local_file_gets_the_full_reinstall(backend, vam):
    plug = drop(vam.addon_packages, "Plug.In.2.var")
    backend.lib.scan()
    ids = content_ids(backend, "Plug.In.2.var")
    os.utime(plug, (1_700_000_000, 1_700_000_000))
    serve(backend, "Plug.In.2.var", data=plug.read_bytes())
    toasts = toasts_of(backend)
    backend.downloads.enqueue_reinstall("Plug.In.2.var")
    assert wait_for(lambda: settled(backend, 1), 10)
    assert backend.downloads.list()[0]["status"] == "completed"
    assert content_ids(backend, "Plug.In.2.var") != ids, "the file changed since the library read it"
    assert not any("already matched" in m for m in toasts)


def test_an_identical_copy_still_takes_the_hub_details(backend, vam):
    plug = drop(vam.addon_packages, "Plug.In.2.var")
    backend.lib.scan()
    serve(backend, "Plug.In.2.var", data=plug.read_bytes())
    backend.hub_fake.details["500"] = {
        "resource_id": "500",
        "title": "Shiny Plugin",
        "tags": "tools",
        "hubFiles": [{"filename": "Plug.In.2.var"}],
    }
    backend.hub.get_resource_detail("500")
    backend.downloads.enqueue_reinstall("Plug.In.2.var")
    assert wait_for(lambda: settled(backend, 1), 10)
    pkg = by_name(backend.lib)["Plug.In.2.var"]
    assert (pkg["hub_display_name"], pkg["hub_tags"]) == ("Shiny Plugin", "tools")
