import sqlite3

import pytest

from vambackstage.core.db import SCHEMA_VERSION, Database, to_int_string
from vambackstage.core.paths import LOCAL_PACKAGE_FILENAME


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def pkg_row(filename, **over):
    parts = filename[:-4].split(".")
    row = {
        "filename": filename,
        "creator": parts[0],
        "package_name": ".".join(parts[:-1]),
        "version": parts[-1],
        "size_bytes": 100,
        "file_mtime": 1.5,
        "type": "Scenes",
        "title": parts[-2],
    }
    row.update(over)
    return row


def test_to_int_string():
    assert to_int_string("123") == "123"
    assert to_int_string(42) == "42"
    assert to_int_string(" 7 ") == "7"
    assert to_int_string(None) is None
    assert to_int_string("null") is None
    assert to_int_string("12a") is None


def test_fresh_schema_and_sentinel(db):
    assert db._schema_version() == SCHEMA_VERSION
    local = db.get_package(LOCAL_PACKAGE_FILENAME)
    assert local and local["is_direct"] == 1 and local["storage_state"] == "enabled"
    db2 = Database(db.path)
    assert db2.get_package(LOCAL_PACKAGE_FILENAME)
    db2.close()


def test_settings(db):
    assert db.get_setting("vam_dir") is None
    db.set_setting("vam_dir", "/x")
    assert db.get_setting("vam_dir") == "/x"
    db.set_setting("vam_dir", None)
    assert db.get_setting("vam_dir") is None
    db.set_setting("auto_hide_deps", "1")
    assert db.get_bool_setting("auto_hide_deps") is True
    assert db.get_bool_setting("nope", default=True) is True


def test_upsert_package_keeps_first_seen(db):
    db.upsert_package(pkg_row("A.B.1.var", first_seen_at=10))
    db.upsert_package(pkg_row("A.B.1.var", first_seen_at=20, size_bytes=200))
    row = db.get_package("A.B.1.var")
    assert row["first_seen_at"] == 10
    assert row["size_bytes"] == 200
    assert row["dep_refs"] == "[]"


def test_tombstones(db):
    db.upsert_package(pkg_row("A.B.1.var"))
    assert db.mark_package_missing("A.B.1.var") == 1
    assert db.mark_package_missing("A.B.1.var") == 0
    assert [p["filename"] for p in db.get_all_packages()] == [LOCAL_PACKAGE_FILENAME]
    info = db.get_package_reconcile_info("A.B.1.var")
    assert info["missing_since"] is not None
    db.set_storage_state("A.B.1.var", "enabled", None, "")
    assert db.get_package_reconcile_info("A.B.1.var")["missing_since"] is None
    db.upsert_package(pkg_row("A.B.1.var"))
    db.mark_packages_missing(["A.B.1.var"])
    assert db.count_missing_packages() == 1
    assert db.forget_deleted_data()["packages"] == 1
    assert db.get_package("A.B.1.var") is None


def test_contents_cascade_and_upsert(db):
    db.upsert_package(pkg_row("A.B.1.var"))
    db.insert_contents(
        [{"package_filename": "A.B.1.var", "internal_path": "Saves/scene/x.json", "display_name": "x", "type": "scene"}]
    )
    db.insert_contents(
        [
            {
                "package_filename": "A.B.1.var",
                "internal_path": "Saves/scene/x.json",
                "display_name": "dup",
                "type": "scene",
            }
        ]
    )
    rows = db.get_all_contents()
    assert len(rows) == 1 and rows[0]["display_name"] == "x"
    cid = rows[0]["id"]
    db.upsert_contents(
        [
            {
                "package_filename": "A.B.1.var",
                "internal_path": "Saves/scene/x.json",
                "display_name": "renamed",
                "type": "scene",
                "file_mtime": 3,
                "size_bytes": 4,
            }
        ]
    )
    rows = db.get_all_contents()
    assert rows[0]["id"] == cid and rows[0]["display_name"] == "renamed" and rows[0]["size_bytes"] == 4
    assert db.get_content_thumbnail_path("A.B.1.var") is None
    db.upsert_contents(
        [
            {
                "package_filename": "A.B.1.var",
                "internal_path": "Custom/Hair/a.vam",
                "display_name": "a",
                "type": "hairItem",
                "thumbnail_path": "Custom/Hair/a.jpg",
            },
            {
                "package_filename": "A.B.1.var",
                "internal_path": "Saves/scene/y.json",
                "display_name": "y",
                "type": "scene",
                "thumbnail_path": "Saves/scene/y.jpg",
            },
        ]
    )
    assert db.get_content_thumbnail_path("A.B.1.var") == "Saves/scene/y.jpg"
    db.delete_contents_for_package_paths("A.B.1.var", ["Saves/scene/y.json"])
    assert len(db.get_all_contents()) == 2
    db.delete_package("A.B.1.var")
    assert db.get_all_contents() == []


def test_local_content_meta(db):
    db.upsert_contents(
        [
            {
                "package_filename": LOCAL_PACKAGE_FILENAME,
                "internal_path": "Saves/scene/s.json",
                "display_name": "s",
                "type": "scene",
                "file_mtime": 1.0,
                "size_bytes": 2,
            }
        ]
    )
    meta = db.get_local_content_meta(LOCAL_PACKAGE_FILENAME)
    assert meta["Saves/scene/s.json"]["mtime"] == 1.0 and meta["Saves/scene/s.json"]["size"] == 2


def test_library_dirs(db):
    db.set_setting("vam_dir", "/v")
    i = db.insert_library_dir("/aux", archive=False)
    assert db.get_library_dir(i)["archive"] == 0
    assert db.get_library_dir_by_path("/aux")["id"] == i
    db.upsert_package(pkg_row("A.B.1.var", storage_state="offloaded", library_dir_id=i))
    assert db.count_packages_in_library_dir(i)["n"] == 1
    assert db.count_packages_in_library_dir(None)["n"] == 0
    assert db.set_library_dir_role(i, True) == 1
    assert db.get_package("A.B.1.var")["storage_state"] == "archived"
    with pytest.raises(sqlite3.IntegrityError):
        db.delete_library_dir(i)
    assert db.remove_library_dir_tombstoning_packages(i) == 1
    assert db.get_library_dir(i) is None
    row = db.get_package("A.B.1.var")
    assert row["missing_since"] is not None and row["library_dir_id"] is None


def test_direct_flags_and_donors(db):
    db.upsert_package(pkg_row("A.B.1.var", first_seen_at=5, type_override="Looks"))
    db.upsert_package(pkg_row("A.B.2.var", first_seen_at=5))
    db.upsert_package(pkg_row("A.B.3.var", first_seen_at=9))
    db.batch_set_direct([("A.B.1.var", True), ("A.B.2.var", False)])
    assert db.get_package("A.B.1.var")["is_direct"] == 1
    donors = db.get_donor_versions_by_package_name("A.B", "A.B.3.var")
    assert [d["filename"] for d in donors] == ["A.B.2.var", "A.B.1.var"]
    assert db.get_donor_versions_by_package_name("A.B", "A.B.2.var") == []


def test_downloads(db):
    assert (
        db.insert_download(
            {"package_ref": "A.B.1", "download_url": "u", "priority": "direct", "hub_resource_id": "null"}
        )
        == 1
    )
    assert db.insert_download({"package_ref": "A.B.1"}) == 0
    d = db.get_download_by_ref("A.B.1")
    assert d["status"] == "queued" and d["hub_resource_id"] is None
    db.update_download_status(d["id"], "active", temp_path="/tmp/x")
    assert db.get_download(d["id"])["temp_path"] == "/tmp/x"
    db.update_download_status(d["id"], "failed", error="boom")
    db.retry_download(d["id"])
    assert db.get_download(d["id"])["status"] == "queued"
    db.update_download_status(d["id"], "completed")
    assert db.get_download(d["id"])["completed_at"] is not None
    db.clear_completed_downloads()
    assert db.get_all_downloads() == []


def test_hub_cache(db):
    db.upsert_hub_resource_detail("12", {"title": "T", "type": "Looks"})
    first = db.get_hub_resource("12")["updated_at"]
    db.upsert_hub_resource_detail("12", {"_unavailable": True})
    assert "T" in db.get_hub_resource("12")["hub_json"]
    db.upsert_hub_resource_find("12", {"downloadUrl": "x"})
    assert db.get_hub_resource("12")["find_json"]
    db.upsert_hub_resource_detail("bogus", {"title": "no"})
    assert db.get_hub_resource("bogus") is None
    db.upsert_package(pkg_row("A.B.1.var"))
    db.set_hub_resource_id("A.B.1.var", "12")
    assert db.set_hub_resource_id("A.B.1.var", "12") == 0
    assert db.set_hub_resource_id("A.B.1.var", "null") == 0
    assert [r["filename"] for r in db.get_packages_needing_hub_detail_apply()] == ["A.B.1.var"]
    db.apply_hub_detail_to_package("A.B.1.var", {"title": "Hub Title", "user_id": "9", "tags": "a,b", "type": "Looks"})
    row = db.get_package("A.B.1.var")
    assert row["hub_display_name"] == "Hub Title" and row["hub_user_id"] == "9" and row["type"] == "Looks"
    assert db.get_packages_needing_hub_detail_apply() == []
    assert first is not None


def test_labels(db):
    db.upsert_package(pkg_row("A.B.1.var"))
    db.upsert_package(pkg_row("A.B.2.var"))
    fav = db.find_or_create_label("Fav")
    assert fav["created"] and fav["color"] == -1
    assert db.find_or_create_label("fav")["created"] is False
    db.apply_label_to_packages(fav["id"], ["A.B.1.var"])
    db.apply_label_to_contents(fav["id"], [("A.B.1.var", "Saves/scene/x.json")])
    db.copy_package_labels("A.B.1.var", "A.B.2.var")
    db.copy_content_labels_for_paths("A.B.1.var", "A.B.2.var", ["Saves/scene/x.json", "other"])
    assert len(db.get_all_label_packages()) == 2
    assert len(db.get_all_label_contents()) == 2
    with pytest.raises(ValueError):
        db.rename_label(fav["id"], "")
    db.delete_package("A.B.1.var")
    assert len(db.get_all_label_packages()) == 1
    db.delete_label(fav["id"])
    assert db.get_all_label_packages() == []
    db.find_or_create_label("Unused")
    assert db.gc_orphan_labels() == 1


def test_bookmarks(db):
    assert db.count_bookmarks() == 0 and db.get_bookmark("1") is None and db.get_bookmark("null") is None
    assert db.upsert_bookmark("12", {"title": "T"}, note="n") is True
    assert db.upsert_bookmark(12, {"title": "T2"}) is False
    row = db.get_bookmark("12")
    assert row["note"] == "n" and '"T2"' in row["hub_json"] and row["created_at"] > 0
    assert db.upsert_bookmark("13", {"title": "Old"}, created_at=5) and db.get_bookmark("13")["created_at"] == 5
    assert [r["resource_id"] for r in db.list_bookmarks()] == ["12", "13"]
    assert db.set_bookmark_note("12", "  ") == 1 and db.get_bookmark("12")["note"] is None
    assert db.set_bookmark_note("12", "x") == 1 and db.set_bookmark_note("99", "x") == 0
    assert db.set_bookmark_note("null", "x") == 0 and db.delete_bookmark("null") == 0
    with pytest.raises(ValueError):
        db.upsert_bookmark("null", {})
    assert db.count_bookmarks() == 2 and db.delete_bookmark("12") == 1 and db.delete_bookmark("12") == 0
    assert db.count_bookmarks() == 1


def test_bookmarks_table_migrates_from_v5(tmp_path):
    from vambackstage.core.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.db"
    db = Database(path)
    db._conn.execute("DROP TABLE bookmarks")
    db._conn.execute("PRAGMA user_version = 5")
    db.close()
    db = Database(path)
    assert db._schema_version() == SCHEMA_VERSION
    assert db.upsert_bookmark("1", {"title": "after"}) and db.count_bookmarks() == 1
    db.close()


def test_packages_keep_version_column_migrates_from_v6(tmp_path):
    from vambackstage.core.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.db"
    db = Database(path)
    db._conn.execute("ALTER TABLE packages DROP COLUMN keep_version")
    db._conn.execute("PRAGMA user_version = 6")
    db.close()
    db = Database(path)
    assert db._schema_version() == SCHEMA_VERSION
    db.upsert_package(pkg_row("A.B.1.var"))
    assert db.get_package("A.B.1.var")["keep_version"] == 0
    db.set_package_keep_version("A.B.1.var", True)
    db.upsert_package(pkg_row("A.B.1.var", size_bytes=5))
    assert db.get_package("A.B.1.var")["keep_version"] == 1, "a rescan keeps the flag"
    db.close()


def test_transaction_rollback(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.upsert_package(pkg_row("A.B.1.var"))
            raise RuntimeError("abort")
    assert db.get_package("A.B.1.var") is None


def test_downloads_reinstall_column_migrates_from_v1(tmp_path):
    from vambackstage.core.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.db"
    db = Database(path)
    db._conn.execute("ALTER TABLE downloads DROP COLUMN reinstall")
    db._conn.execute("PRAGMA user_version = 1")
    db.close()
    db = Database(path)
    assert db._schema_version() == SCHEMA_VERSION
    db.insert_download({"package_ref": "A.B.1.var", "download_url": "https://x", "reinstall": 1})
    assert db.get_download_by_ref("A.B.1.var")["reinstall"] == 1
    db.insert_download({"package_ref": "C.D.1.var", "download_url": "https://y"})
    assert db.get_download_by_ref("C.D.1.var")["reinstall"] == 0
    db.close()


def test_packages_is_dummy_column_migrates_from_v2(tmp_path):
    from vambackstage.core.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.db"
    db = Database(path)
    db._conn.execute("ALTER TABLE packages DROP COLUMN is_dummy")
    db._conn.execute("PRAGMA user_version = 2")
    db.close()
    db = Database(path)
    assert db._schema_version() == SCHEMA_VERSION
    db.upsert_package(
        {
            "filename": "A.B.1.var",
            "creator": "A",
            "package_name": "A.B",
            "version": "1",
            "size_bytes": 1,
            "file_mtime": 1.0,
            "is_dummy": 1,
        }
    )
    assert db.get_package("A.B.1.var")["is_dummy"] == 1
    db.close()
