from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .paths import LOCAL_PACKAGE_DISPLAY_NAME, LOCAL_PACKAGE_FILENAME

SCHEMA_VERSION = 8

_INT_RE = re.compile(r"[0-9]+")


def to_int_string(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if _INT_RE.fullmatch(s) else None


def now() -> int:
    return int(time.time())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_dirs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  path       TEXT UNIQUE NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  archive    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS packages (
  filename              TEXT PRIMARY KEY,
  creator               TEXT NOT NULL,
  package_name          TEXT NOT NULL,
  version               TEXT NOT NULL,
  type                  TEXT,
  title                 TEXT,
  description           TEXT,
  license               TEXT,
  size_bytes            INTEGER NOT NULL,
  file_mtime            REAL NOT NULL,
  is_direct             INTEGER NOT NULL DEFAULT 0,
  storage_state         TEXT NOT NULL DEFAULT 'enabled',
  library_dir_id        INTEGER NULL REFERENCES library_dirs(id) ON DELETE RESTRICT,
  subpath               TEXT NOT NULL DEFAULT '',
  hub_resource_id       TEXT,
  hub_user_id           TEXT,
  hub_display_name      TEXT,
  hub_tags              TEXT,
  promotional_link      TEXT,
  image_url             TEXT,
  type_override         TEXT,
  is_corrupted          INTEGER NOT NULL DEFAULT 0,
  is_dummy              INTEGER NOT NULL DEFAULT 0,
  is_unpacked           INTEGER NOT NULL DEFAULT 0,
  keep_version          INTEGER NOT NULL DEFAULT 0,
  dep_refs              TEXT NOT NULL DEFAULT '[]',
  hub_detail_applied_at INTEGER,
  hub_name_checked_at   INTEGER,
  missing_since         INTEGER,
  first_seen_at         INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  scanned_at            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_packages_package_name ON packages(package_name);
CREATE INDEX IF NOT EXISTS idx_packages_creator ON packages(creator);
CREATE INDEX IF NOT EXISTS idx_packages_missing_since ON packages(missing_since);

CREATE TABLE IF NOT EXISTS contents (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  package_filename TEXT NOT NULL REFERENCES packages(filename) ON DELETE CASCADE,
  internal_path    TEXT NOT NULL,
  display_name     TEXT NOT NULL,
  type             TEXT NOT NULL,
  thumbnail_path   TEXT,
  person_atom_ids  TEXT,
  file_mtime       REAL NOT NULL DEFAULT 0,
  size_bytes       INTEGER NOT NULL DEFAULT 0,
  UNIQUE(package_filename, internal_path)
);
CREATE INDEX IF NOT EXISTS idx_contents_package ON contents(package_filename);
CREATE INDEX IF NOT EXISTS idx_contents_type ON contents(type);

CREATE TABLE IF NOT EXISTS downloads (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  package_ref     TEXT NOT NULL UNIQUE,
  hub_resource_id TEXT,
  download_url    TEXT,
  file_size       INTEGER,
  priority        TEXT NOT NULL DEFAULT 'dependency',
  parent_ref      TEXT,
  display_name    TEXT,
  status          TEXT NOT NULL DEFAULT 'queued',
  temp_path       TEXT,
  error           TEXT,
  created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  completed_at    INTEGER,
  auto_queue_deps INTEGER NOT NULL DEFAULT 1,
  reinstall       INTEGER NOT NULL DEFAULT 0,
  quiet           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hub_resources (
  resource_id TEXT PRIMARY KEY,
  hub_json    TEXT,
  search_json TEXT,
  find_json   TEXT,
  updated_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS hub_users (
  user_id    TEXT PRIMARY KEY,
  username   TEXT,
  hub_json   TEXT,
  updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_hub_users_username ON hub_users(username);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS labels (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  color      INTEGER,
  created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS label_packages (
  label_id         INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  package_filename TEXT NOT NULL REFERENCES packages(filename) ON DELETE CASCADE,
  PRIMARY KEY (label_id, package_filename)
);
CREATE INDEX IF NOT EXISTS idx_label_packages_pkg ON label_packages(package_filename);

CREATE TABLE IF NOT EXISTS label_contents (
  label_id         INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  package_filename TEXT NOT NULL REFERENCES packages(filename) ON DELETE CASCADE,
  internal_path    TEXT NOT NULL,
  PRIMARY KEY (label_id, package_filename, internal_path)
);
CREATE INDEX IF NOT EXISTS idx_label_contents_pkgpath ON label_contents(package_filename, internal_path);

CREATE TABLE IF NOT EXISTS local_refs (
  internal_path TEXT PRIMARY KEY,
  file_mtime    REAL NOT NULL,
  size_bytes    INTEGER NOT NULL,
  refs          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookmarks (
  resource_id TEXT PRIMARY KEY,
  hub_json    TEXT NOT NULL,
  note        TEXT,
  created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
"""

_LOCAL_REFS_TABLE = """CREATE TABLE IF NOT EXISTS local_refs (
  internal_path TEXT PRIMARY KEY,
  file_mtime    REAL NOT NULL,
  size_bytes    INTEGER NOT NULL,
  refs          TEXT NOT NULL
)"""

_BOOKMARKS_TABLE = """CREATE TABLE IF NOT EXISTS bookmarks (
  resource_id TEXT PRIMARY KEY,
  hub_json    TEXT NOT NULL,
  note        TEXT,
  created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
)"""


def _add_column(table: str, column: str, decl: str):

    def apply(conn) -> None:
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    return apply


MIGRATIONS: list[tuple[int, Any]] = [
    (2, _add_column("downloads", "reinstall", "INTEGER NOT NULL DEFAULT 0")),
    (3, _add_column("packages", "is_dummy", "INTEGER NOT NULL DEFAULT 0")),
    (4, lambda conn: conn.execute(_LOCAL_REFS_TABLE)),
    (5, _add_column("packages", "is_unpacked", "INTEGER NOT NULL DEFAULT 0")),
    (6, lambda conn: conn.execute(_BOOKMARKS_TABLE)),
    (7, _add_column("packages", "keep_version", "INTEGER NOT NULL DEFAULT 0")),
    (8, _add_column("downloads", "quiet", "INTEGER NOT NULL DEFAULT 0")),
]


def display_name_for_package(filename: str, detail: dict) -> str | None:
    from ..hub.client import hub_package_base_name, title_fits_package

    title = (detail.get("title") or "").strip() or None
    if not title:
        return None
    base = hub_package_base_name(filename)
    bases = {
        hub_package_base_name(f["filename"]).lower()
        for f in detail.get("hubFiles") or []
        if isinstance(f, dict) and f.get("filename") and f.get("filename") != "null"
    }
    keys = {hub_package_base_name(k).lower() for k in (detail.get("dependencies") or {}) if k}
    packages = bases or keys
    if len(packages) <= 1 or title_fits_package(title, base):
        return title
    return None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _run(self, sql: str, params: Iterable | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: Iterable | dict = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, params: Iterable | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        with self._lock:
            current = self._schema_version()
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"Database schema {current} is newer than this app supports ({SCHEMA_VERSION}).")
            if current == 0:
                self._conn.executescript(f"BEGIN;\n{_SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
            else:
                for version, apply in MIGRATIONS:
                    if current < version:
                        if callable(apply):
                            with self.transaction():
                                apply(self._conn)
                                self._conn.execute(f"PRAGMA user_version = {version}")
                        else:
                            self._conn.executescript(f"BEGIN;\n{apply}\nPRAGMA user_version = {version};\nCOMMIT;")
            self.ensure_local_package()

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self._run("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self._run("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

    def get_bool_setting(self, key: str, default: bool = False) -> bool:
        v = self.get_setting(key)
        return default if v is None else v == "1"

    def ensure_local_package(self) -> None:
        self._run(
            """INSERT OR IGNORE INTO packages (
                 filename, creator, package_name, version, type, title, description, license,
                 size_bytes, file_mtime, is_direct, storage_state, library_dir_id, dep_refs)
               VALUES (?, '', '', '', NULL, ?, NULL, NULL, 0, 0, 1, 'enabled', NULL, '[]')""",
            (LOCAL_PACKAGE_FILENAME, LOCAL_PACKAGE_DISPLAY_NAME),
        )

    def upsert_package(self, pkg: dict) -> None:
        row = {
            "subpath": "",
            "first_seen_at": now(),
            "scanned_at": now(),
            "type": None,
            "title": None,
            "description": None,
            "license": None,
            "is_direct": 0,
            "storage_state": "enabled",
            "library_dir_id": None,
            "dep_refs": "[]",
            "is_dummy": 0,
            "is_unpacked": 0,
        }
        row.update(pkg)
        self._run(
            """INSERT INTO packages (filename, creator, package_name, version, type, title, description,
                 license, size_bytes, file_mtime, is_direct, storage_state, library_dir_id, subpath,
                 dep_refs, first_seen_at, scanned_at, is_dummy, is_unpacked)
               VALUES (:filename, :creator, :package_name, :version, :type, :title, :description,
                 :license, :size_bytes, :file_mtime, :is_direct, :storage_state, :library_dir_id,
                 :subpath, :dep_refs, :first_seen_at, :scanned_at, :is_dummy, :is_unpacked)
               ON CONFLICT(filename) DO UPDATE SET
                 creator = excluded.creator, package_name = excluded.package_name,
                 version = excluded.version, type = excluded.type, title = excluded.title,
                 description = excluded.description, license = excluded.license,
                 size_bytes = excluded.size_bytes, file_mtime = excluded.file_mtime,
                 storage_state = excluded.storage_state, library_dir_id = excluded.library_dir_id,
                 subpath = excluded.subpath, dep_refs = excluded.dep_refs,
                 scanned_at = excluded.scanned_at, is_dummy = excluded.is_dummy,
                 is_unpacked = excluded.is_unpacked, missing_since = NULL, is_corrupted = 0""",
            row,
        )

    def get_package(self, filename: str) -> dict | None:
        return self._one("SELECT * FROM packages WHERE filename = ?", (filename,))

    def get_all_packages(self) -> list[dict]:
        return self._all("SELECT * FROM packages WHERE missing_since IS NULL")

    def delete_package(self, filename: str) -> None:
        self._run("DELETE FROM packages WHERE filename = ?", (filename,))

    def mark_package_missing(self, filename: str) -> int:
        return self._run(
            "UPDATE packages SET missing_since = ? WHERE filename = ? AND missing_since IS NULL",
            (now(), filename),
        ).rowcount

    def mark_packages_missing(self, filenames: Iterable[str]) -> None:
        with self.transaction():
            for fn in filenames:
                self.mark_package_missing(fn)

    def set_package_direct(self, filename: str, is_direct: bool) -> None:
        self._run("UPDATE packages SET is_direct = ? WHERE filename = ?", (1 if is_direct else 0, filename))

    def batch_set_direct(self, entries: Iterable[tuple[str, bool]]) -> None:
        with self.transaction():
            for fn, direct in entries:
                self.set_package_direct(fn, direct)

    def touch_package_first_seen(self, filename: str, at: int | None = None) -> int:
        at = now() if at is None else at
        self._run("UPDATE packages SET first_seen_at = ? WHERE filename = ?", (at, filename))
        return at

    def set_package_type_override(self, filename: str, type_override: str | None) -> None:
        v = None if type_override in (None, "") else str(type_override)
        self._run("UPDATE packages SET type_override = ? WHERE filename = ?", (v, filename))

    def set_package_keep_version(self, filename: str, keep: bool) -> None:
        self._run("UPDATE packages SET keep_version = ? WHERE filename = ?", (1 if keep else 0, filename))

    def set_package_type_from_hub(self, filename: str, hub_type: str | None) -> None:
        t = (hub_type or "").strip()
        if t:
            self._run("UPDATE packages SET type = ? WHERE filename = ?", (t, filename))

    def set_storage_state(
        self, filename: str, storage_state: str, library_dir_id: int | None, subpath: str | None = None
    ) -> None:
        if subpath is None:
            self._run(
                "UPDATE packages SET storage_state = ?, library_dir_id = ?, missing_since = NULL WHERE filename = ?",
                (storage_state, library_dir_id, filename),
            )
        else:
            self._run(
                "UPDATE packages SET storage_state = ?, library_dir_id = ?, subpath = ?, missing_since = NULL WHERE filename = ?",
                (storage_state, library_dir_id, subpath, filename),
            )

    def get_package_reconcile_info(self, filename: str) -> dict | None:
        return self._one(
            "SELECT file_mtime, size_bytes, storage_state, library_dir_id, subpath, missing_since FROM packages WHERE filename = ?",
            (filename,),
        )

    def get_donor_versions_by_package_name(self, package_name: str, filename: str) -> list[dict]:
        return self._all(
            """SELECT filename, version, type_override FROM packages
               WHERE package_name = ? AND filename != ? AND package_name != '' AND missing_since IS NULL
                 AND first_seen_at < (SELECT first_seen_at FROM packages WHERE filename = ?)
               ORDER BY CAST(version AS INTEGER) DESC""",
            (package_name, filename, filename),
        )

    def get_all_db_filenames_with_dir(self) -> list[dict]:
        return self._all("SELECT filename, library_dir_id FROM packages WHERE missing_since IS NULL")

    def set_package_corrupted(self, filename: str, is_corrupted: bool) -> None:
        self._run("UPDATE packages SET is_corrupted = ? WHERE filename = ?", (1 if is_corrupted else 0, filename))

    def batch_set_corrupted(self, entries: Iterable[tuple[str, bool]]) -> None:
        with self.transaction():
            for fn, c in entries:
                self.set_package_corrupted(fn, c)

    def clear_all_corrupted(self) -> None:
        self._run("UPDATE packages SET is_corrupted = 0 WHERE is_corrupted = 1")

    def set_hub_resource_id(self, filename: str, resource_id: Any) -> int:
        rid = to_int_string(resource_id)
        if rid is None:
            return 0
        return self._run(
            "UPDATE packages SET hub_resource_id = ? WHERE filename = ? AND hub_resource_id IS NOT ?",
            (rid, filename, rid),
        ).rowcount

    def set_hub_user_id(self, filename: str, user_id: Any) -> None:
        uid = to_int_string(user_id)
        if uid is not None:
            self._run("UPDATE packages SET hub_user_id = ? WHERE filename = ?", (uid, filename))

    def set_hub_display_name(self, filename: str, display_name: str | None) -> None:
        self._run("UPDATE packages SET hub_display_name = ? WHERE filename = ?", (display_name, filename))

    def mark_hub_name_checked(self, filename: str) -> None:
        self._run("UPDATE packages SET hub_name_checked_at = ? WHERE filename = ?", (now(), filename))

    def unlink_hub_resource(self, filename: str) -> None:
        self._run(
            """UPDATE packages SET hub_resource_id = NULL, hub_display_name = NULL, hub_tags = NULL,
                 promotional_link = NULL, image_url = NULL, hub_detail_applied_at = NULL, hub_name_checked_at = NULL
               WHERE filename = ?""",
            (filename,),
        )

    def set_package_thumbnail(self, filename: str, image_url: str | None) -> None:
        self._run("UPDATE packages SET image_url = ? WHERE filename = ?", (image_url, filename))

    def get_packages_needing_thumbnail(self) -> list[dict]:
        return self._all(
            "SELECT filename, package_name, hub_resource_id FROM packages WHERE image_url IS NULL AND missing_since IS NULL AND filename != ?",
            (LOCAL_PACKAGE_FILENAME,),
        )

    def apply_hub_detail_to_package(self, filename: str, detail: dict | None) -> None:
        if not detail or detail.get("_unavailable"):
            self._run("UPDATE packages SET hub_detail_applied_at = ? WHERE filename = ?", (now(), filename))
            return
        hub_type = detail.get("type") if isinstance(detail.get("type"), str) else ""
        self._run(
            """UPDATE packages SET
                 hub_user_id = COALESCE(?, hub_user_id),
                 hub_display_name = ?,
                 hub_tags = ?, promotional_link = ?,
                 type = COALESCE(?, type),
                 hub_detail_applied_at = ?
               WHERE filename = ?""",
            (
                to_int_string(detail.get("user_id")),
                display_name_for_package(filename, detail),
                detail.get("tags") or None,
                detail.get("promotional_link") or None,
                (hub_type or "").strip() or None,
                now(),
                filename,
            ),
        )

    def count_packages_in_library_dir(self, dir_id: int | None) -> dict:
        if dir_id is None:
            return self._one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes FROM packages WHERE library_dir_id IS NULL AND missing_since IS NULL AND filename != ?",
                (LOCAL_PACKAGE_FILENAME,),
            ) or {"n": 0, "bytes": 0}
        return self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes FROM packages WHERE library_dir_id = ? AND missing_since IS NULL",
            (dir_id,),
        ) or {"n": 0, "bytes": 0}

    def count_missing_packages(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM packages WHERE missing_since IS NOT NULL")["n"]

    def forget_deleted_data(self) -> dict:
        with self.transaction():
            packages = self._run("DELETE FROM packages WHERE missing_since IS NOT NULL").rowcount
            content_labels = self._run(
                """DELETE FROM label_contents WHERE NOT EXISTS (
                     SELECT 1 FROM contents c
                     WHERE c.package_filename = label_contents.package_filename
                       AND (c.internal_path = label_contents.internal_path
                            OR (label_contents.package_filename = ?
                                AND c.internal_path = label_contents.internal_path || '.disabled')))""",
                (LOCAL_PACKAGE_FILENAME,),
            ).rowcount
        return {"packages": packages, "content_labels": content_labels}

    def list_library_dirs(self) -> list[dict]:
        return self._all("SELECT id, path, created_at, archive FROM library_dirs ORDER BY created_at ASC, id ASC")

    def get_library_dir(self, dir_id: int) -> dict | None:
        return self._one("SELECT id, path, created_at, archive FROM library_dirs WHERE id = ?", (dir_id,))

    def get_library_dir_by_path(self, path: str) -> dict | None:
        return self._one("SELECT id, path, created_at, archive FROM library_dirs WHERE path = ?", (path,))

    def insert_library_dir(self, path: str, archive: bool = False) -> int:
        cur = self._run("INSERT INTO library_dirs (path, archive) VALUES (?, ?)", (path, 1 if archive else 0))
        return int(cur.lastrowid)

    def set_library_dir_role(self, dir_id: int, archive: bool) -> int:
        with self.transaction():
            self._run("UPDATE library_dirs SET archive = ? WHERE id = ?", (1 if archive else 0, dir_id))
            return self._run(
                "UPDATE packages SET storage_state = ? WHERE library_dir_id = ? AND missing_since IS NULL",
                ("archived" if archive else "offloaded", dir_id),
            ).rowcount

    def delete_library_dir(self, dir_id: int) -> None:
        self._run("DELETE FROM library_dirs WHERE id = ?", (dir_id,))

    def remove_library_dir_tombstoning_packages(self, dir_id: int) -> int:
        with self.transaction():
            n = self._run(
                "UPDATE packages SET missing_since = ?, library_dir_id = NULL WHERE library_dir_id = ? AND missing_since IS NULL",
                (now(), dir_id),
            ).rowcount
            self._run("UPDATE packages SET library_dir_id = NULL WHERE library_dir_id = ?", (dir_id,))
            self._run("DELETE FROM library_dirs WHERE id = ?", (dir_id,))
            return n

    _CONTENT_COLUMNS = (
        "package_filename",
        "internal_path",
        "display_name",
        "type",
        "thumbnail_path",
        "person_atom_ids",
        "file_mtime",
        "size_bytes",
    )

    def _content_row(self, item: dict) -> dict:
        row = {"thumbnail_path": None, "person_atom_ids": None, "file_mtime": 0, "size_bytes": 0}
        row.update(item)
        return row

    def insert_contents(self, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO contents (package_filename, internal_path, display_name, type,
                     thumbnail_path, person_atom_ids, file_mtime, size_bytes)
                   VALUES (:package_filename, :internal_path, :display_name, :type, :thumbnail_path,
                     :person_atom_ids, :file_mtime, :size_bytes)""",
                [self._content_row(r) for r in rows],
            )

    def upsert_contents(self, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany(
                """INSERT INTO contents (package_filename, internal_path, display_name, type,
                     thumbnail_path, person_atom_ids, file_mtime, size_bytes)
                   VALUES (:package_filename, :internal_path, :display_name, :type, :thumbnail_path,
                     :person_atom_ids, :file_mtime, :size_bytes)
                   ON CONFLICT(package_filename, internal_path) DO UPDATE SET
                     display_name = excluded.display_name, type = excluded.type,
                     thumbnail_path = excluded.thumbnail_path, person_atom_ids = excluded.person_atom_ids,
                     file_mtime = excluded.file_mtime, size_bytes = excluded.size_bytes""",
                [self._content_row(r) for r in rows],
            )

    def delete_contents_for_package(self, filename: str) -> None:
        self._run("DELETE FROM contents WHERE package_filename = ?", (filename,))

    def delete_contents_for_package_paths(self, filename: str, paths: Iterable[str]) -> None:
        paths = list(paths)
        if not paths:
            return
        with self.transaction() as conn:
            conn.executemany(
                "DELETE FROM contents WHERE package_filename = ? AND internal_path = ?",
                [(filename, p) for p in paths],
            )

    def get_all_contents(self) -> list[dict]:
        return self._all(
            "SELECT c.* FROM contents c JOIN packages p ON p.filename = c.package_filename WHERE p.missing_since IS NULL"
        )

    def get_local_refs_meta(self) -> dict[str, dict]:
        return {
            r["internal_path"]: r
            for r in self._all("SELECT internal_path, file_mtime, size_bytes, refs FROM local_refs")
        }

    def upsert_local_refs(self, rows: Iterable[dict]) -> None:
        with self.transaction():
            for r in rows:
                self._run(
                    """INSERT INTO local_refs (internal_path, file_mtime, size_bytes, refs) VALUES (:internal_path, :file_mtime, :size_bytes, :refs)
                       ON CONFLICT(internal_path) DO UPDATE SET file_mtime = excluded.file_mtime, size_bytes = excluded.size_bytes, refs = excluded.refs""",
                    r,
                )

    def delete_local_refs(self, paths: Iterable[str]) -> None:
        with self.transaction():
            for p in paths:
                self._run("DELETE FROM local_refs WHERE internal_path = ?", (p,))

    def set_package_dep_refs(self, filename: str, dep_refs_json: str) -> bool:
        return (
            self._run(
                "UPDATE packages SET dep_refs = ? WHERE filename = ? AND dep_refs IS NOT ?",
                (dep_refs_json, filename, dep_refs_json),
            ).rowcount
            > 0
        )

    def get_local_content_meta(self, package_filename: str) -> dict[str, dict]:
        rows = self._all(
            "SELECT internal_path, file_mtime, size_bytes, person_atom_ids, display_name, type, thumbnail_path FROM contents WHERE package_filename = ?",
            (package_filename,),
        )
        return {
            r["internal_path"]: {
                "mtime": r["file_mtime"],
                "size": r["size_bytes"],
                "person_atom_ids": r["person_atom_ids"],
                "display_name": r["display_name"],
                "type": r["type"],
                "thumbnail_path": r["thumbnail_path"],
            }
            for r in rows
        }

    _THUMB_TYPE_ORDER = """CASE type
        WHEN 'scene' THEN 1 WHEN 'legacyScene' THEN 1 WHEN 'subscene' THEN 2
        WHEN 'look' THEN 3 WHEN 'legacyLook' THEN 3 WHEN 'skinPreset' THEN 3
        WHEN 'pose' THEN 4 WHEN 'legacyPose' THEN 4
        WHEN 'clothingItem' THEN 5 WHEN 'clothingPreset' THEN 5
        WHEN 'hairItem' THEN 6 WHEN 'hairPreset' THEN 6 ELSE 7 END"""

    def get_content_thumbnail_path(self, package_filename: str) -> str | None:
        row = self._one(
            f"SELECT thumbnail_path FROM contents WHERE package_filename = ? AND thumbnail_path IS NOT NULL ORDER BY {self._THUMB_TYPE_ORDER}, internal_path LIMIT 1",
            (package_filename,),
        )
        return row["thumbnail_path"] if row else None

    def insert_download(self, entry: dict) -> int:
        row = {
            "hub_resource_id": None,
            "download_url": None,
            "file_size": None,
            "priority": "dependency",
            "parent_ref": None,
            "display_name": None,
            "auto_queue_deps": 1,
            "reinstall": 0,
            "quiet": 0,
        }
        row.update(entry)
        row["hub_resource_id"] = to_int_string(row.get("hub_resource_id"))
        row["reinstall"] = 1 if row.get("reinstall") else 0
        row["quiet"] = 1 if row.get("quiet") else 0
        return self._run(
            """INSERT OR IGNORE INTO downloads (package_ref, hub_resource_id, download_url, file_size,
                 priority, parent_ref, display_name, auto_queue_deps, reinstall, quiet, status)
               VALUES (:package_ref, :hub_resource_id, :download_url, :file_size, :priority, :parent_ref,
                 :display_name, :auto_queue_deps, :reinstall, :quiet, 'queued')""",
            row,
        ).rowcount

    def get_download(self, dl_id: int) -> dict | None:
        return self._one("SELECT * FROM downloads WHERE id = ?", (dl_id,))

    def get_download_by_ref(self, package_ref: str) -> dict | None:
        return self._one("SELECT * FROM downloads WHERE package_ref = ?", (package_ref,))

    def get_all_downloads(self) -> list[dict]:
        return self._all("SELECT * FROM downloads ORDER BY created_at ASC, id ASC")

    def update_download_status(self, dl_id: int, status: str, temp_path: str | None = ..., error: str | None = ...) -> None:  # type: ignore[assignment]
        sets = ["status = ?"]
        params: list = [status]
        if temp_path is not ...:
            sets.append("temp_path = ?")
            params.append(temp_path)
        if error is not ...:
            sets.append("error = ?")
            params.append(error)
        if status == "completed":
            sets.append("completed_at = ?")
            params.append(now())
        params.append(dl_id)
        self._run(f"UPDATE downloads SET {', '.join(sets)} WHERE id = ?", params)

    def update_download_fields(self, dl_id: int, **fields) -> None:
        allowed = {"package_ref", "file_size", "download_url", "error", "display_name"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE downloads SET {sets} WHERE id = ?", [*fields.values(), dl_id])

    def rename_download(self, dl_id: int, new_ref: str) -> bool:
        with self.transaction():
            other = self.get_download_by_ref(new_ref)
            if other and other["id"] != dl_id:
                if other["status"] in ("queued", "active"):
                    return False
                self.delete_download(other["id"])
            self._run("UPDATE downloads SET package_ref = ? WHERE id = ?", (new_ref, dl_id))
        return True

    def reset_active_downloads(self) -> None:
        self._run("UPDATE downloads SET status = 'queued' WHERE status = 'active'")

    def fail_unfinished_downloads(self) -> None:
        self._run("UPDATE downloads SET status = 'failed', error = 'Interrupted' WHERE status IN ('active', 'queued')")

    def cancel_all_downloads(self) -> None:
        self._run("UPDATE downloads SET status = 'cancelled' WHERE status IN ('queued', 'active')")

    def clear_completed_downloads(self) -> None:
        self._run("DELETE FROM downloads WHERE status = 'completed'")

    def clear_failed_downloads(self) -> None:
        self._run("DELETE FROM downloads WHERE status = 'failed'")

    def cancel_download(self, dl_id: int) -> None:
        self._run("UPDATE downloads SET status = 'cancelled' WHERE id = ? AND status IN ('queued', 'active')", (dl_id,))

    def retry_download(self, dl_id: int) -> None:
        self._run(
            "UPDATE downloads SET status = 'queued', error = NULL, created_at = 0, quiet = 0 WHERE id = ? AND status = 'failed'",
            (dl_id,),
        )

    def has_pending_quiet_downloads(self) -> bool:
        return (
            self._one("SELECT 1 AS x FROM downloads WHERE quiet = 1 AND status IN ('queued', 'active') LIMIT 1")
            is not None
        )

    def delete_download(self, dl_id: int) -> None:
        self._run("DELETE FROM downloads WHERE id = ?", (dl_id,))

    def upsert_hub_resource_detail(self, resource_id: Any, detail: dict) -> None:
        rid = to_int_string(resource_id)
        if rid is None:
            return
        is_stub = bool(detail.get("_unavailable"))
        extra = " AND hub_resources.hub_json IS NULL" if is_stub else ""
        self._run(
            f"""INSERT INTO hub_resources (resource_id, hub_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET hub_json = excluded.hub_json, updated_at = excluded.updated_at
                WHERE hub_resources.hub_json IS NOT excluded.hub_json{extra}""",
            (rid, json.dumps(detail), now()),
        )

    def clear_hub_resource_detail(self, resource_id: Any) -> None:
        rid = to_int_string(resource_id)
        if rid is not None:
            self._run("UPDATE hub_resources SET hub_json = NULL, updated_at = ? WHERE resource_id = ?", (now(), rid))

    def upsert_hub_resource_search(self, resource_id: Any, data: dict) -> None:
        rid = to_int_string(resource_id)
        if rid is None:
            return
        self._run(
            """INSERT INTO hub_resources (resource_id, search_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(resource_id) DO UPDATE SET search_json = excluded.search_json, updated_at = excluded.updated_at
               WHERE hub_resources.search_json IS NOT excluded.search_json""",
            (rid, json.dumps(data), now()),
        )

    def upsert_hub_resource_find(self, resource_id: Any, data: dict) -> None:
        rid = to_int_string(resource_id)
        if rid is None:
            return
        self._run(
            """INSERT INTO hub_resources (resource_id, find_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(resource_id) DO UPDATE SET find_json = excluded.find_json, updated_at = excluded.updated_at
               WHERE hub_resources.find_json IS NOT excluded.find_json""",
            (rid, json.dumps(data), now()),
        )

    def get_hub_resource(self, resource_id: Any) -> dict | None:
        rid = to_int_string(resource_id)
        if rid is None:
            return None
        return self._one("SELECT * FROM hub_resources WHERE resource_id = ?", (rid,))

    def get_all_hub_resource_jsons(self) -> list[dict]:
        return self._all("SELECT resource_id, hub_json, search_json, find_json FROM hub_resources")

    def get_packages_needing_hub_detail_apply(self, all_linked: bool = False) -> list[dict]:
        cond = "" if all_linked else " AND (p.hub_detail_applied_at IS NULL OR p.hub_detail_applied_at < hr.updated_at)"
        return self._all("""SELECT p.filename, p.package_name, hr.hub_json FROM packages p
               JOIN hub_resources hr ON hr.resource_id = p.hub_resource_id
               WHERE p.hub_resource_id IS NOT NULL AND p.missing_since IS NULL AND hr.hub_json IS NOT NULL""" + cond)

    def get_packages_needing_hub_detail_fetch(self) -> list[dict]:
        return self._all("""SELECT p.filename, p.hub_resource_id AS rid FROM packages p
               LEFT JOIN hub_resources hr ON hr.resource_id = p.hub_resource_id
               WHERE p.hub_resource_id IS NOT NULL AND p.missing_since IS NULL AND hr.hub_json IS NULL""")

    def get_packages_needing_hub_name_lookup(self) -> list[dict]:
        return self._all(
            """SELECT filename, package_name FROM packages
               WHERE filename != ? AND missing_since IS NULL
                 AND hub_resource_id IS NULL AND hub_name_checked_at IS NULL""",
            (LOCAL_PACKAGE_FILENAME,),
        )

    def upsert_hub_user(self, user_id: Any, username: str | None, data: dict) -> None:
        uid = to_int_string(user_id)
        if uid is None:
            return
        self._run(
            """INSERT INTO hub_users (user_id, username, hub_json, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username = COALESCE(excluded.username, hub_users.username),
                 hub_json = excluded.hub_json, updated_at = excluded.updated_at""",
            (uid, username or None, json.dumps(data), now()),
        )

    def get_hub_user_by_username(self, username: str) -> dict | None:
        return self._one("SELECT * FROM hub_users WHERE username = ?", (username,))

    def find_or_create_label(self, name: str) -> dict:
        existing = self._one("SELECT id, name, color FROM labels WHERE name = ?", (name,))
        if existing:
            return {**existing, "created": False}
        cur = self._run("INSERT INTO labels (name, color) VALUES (?, -1)", (name,))
        return {"id": int(cur.lastrowid), "name": name, "color": -1, "created": True}

    def get_all_labels(self) -> list[dict]:
        return self._all("SELECT id, name, color, created_at FROM labels ORDER BY name COLLATE NOCASE")

    def get_all_label_packages(self) -> list[dict]:
        return self._all("SELECT label_id, package_filename FROM label_packages")

    def get_all_label_contents(self) -> list[dict]:
        return self._all("SELECT label_id, package_filename, internal_path FROM label_contents")

    def get_label_by_id(self, label_id: int) -> dict | None:
        return self._one("SELECT id, name, color FROM labels WHERE id = ?", (label_id,))

    def rename_label(self, label_id: int, name: str) -> dict | None:
        trimmed = (name or "").strip()
        if not trimmed:
            raise ValueError("Label name cannot be empty")
        if self._one("SELECT id FROM labels WHERE name = ? AND id != ?", (trimmed, label_id)):
            raise ValueError(f'A label named "{trimmed}" already exists')
        self._run("UPDATE labels SET name = ? WHERE id = ?", (trimmed, label_id))
        return self.get_label_by_id(label_id)

    def recolor_label(self, label_id: int, color: int | None) -> dict | None:
        self._run("UPDATE labels SET color = ? WHERE id = ?", (color, label_id))
        return self.get_label_by_id(label_id)

    def delete_label(self, label_id: int) -> None:
        self._run("DELETE FROM labels WHERE id = ?", (label_id,))

    def apply_label_to_packages(self, label_id: int, filenames: Iterable[str]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO label_packages (label_id, package_filename) VALUES (?, ?)",
                [(label_id, fn) for fn in filenames],
            )

    def remove_label_from_packages(self, label_id: int, filenames: Iterable[str]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "DELETE FROM label_packages WHERE label_id = ? AND package_filename = ?",
                [(label_id, fn) for fn in filenames],
            )

    def apply_label_to_contents(self, label_id: int, items: Iterable[tuple[str, str]]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO label_contents (label_id, package_filename, internal_path) VALUES (?, ?, ?)",
                [(label_id, pf, ip) for pf, ip in items],
            )

    def remove_label_from_contents(self, label_id: int, items: Iterable[tuple[str, str]]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "DELETE FROM label_contents WHERE label_id = ? AND package_filename = ? AND internal_path = ?",
                [(label_id, pf, ip) for pf, ip in items],
            )

    def copy_package_labels(self, from_filename: str, to_filename: str) -> None:
        self._run(
            "INSERT OR IGNORE INTO label_packages (label_id, package_filename) SELECT label_id, ? FROM label_packages WHERE package_filename = ?",
            (to_filename, from_filename),
        )

    def copy_content_labels_for_paths(
        self, from_filename: str, to_filename: str, internal_paths: Iterable[str]
    ) -> None:
        paths = list(internal_paths)
        if not paths:
            return
        placeholders = ",".join("?" for _ in paths)
        self._run(
            f"""INSERT OR IGNORE INTO label_contents (label_id, package_filename, internal_path)
                SELECT label_id, ?, internal_path FROM label_contents
                WHERE package_filename = ? AND internal_path IN ({placeholders})""",
            [to_filename, from_filename, *paths],
        )

    def gc_orphan_labels(self) -> int:
        return self._run(
            "DELETE FROM labels WHERE id NOT IN (SELECT label_id FROM label_packages) AND id NOT IN (SELECT label_id FROM label_contents)"
        ).rowcount

    _BOOKMARK_COLUMNS = "resource_id, hub_json, note, created_at"

    def list_bookmarks(self) -> list[dict]:
        return self._all(f"SELECT {self._BOOKMARK_COLUMNS} FROM bookmarks ORDER BY created_at DESC, resource_id DESC")

    def get_bookmark(self, resource_id: Any) -> dict | None:
        rid = to_int_string(resource_id)
        if rid is None:
            return None
        return self._one(f"SELECT {self._BOOKMARK_COLUMNS} FROM bookmarks WHERE resource_id = ?", (rid,))

    def upsert_bookmark(
        self, resource_id: Any, data: dict, note: str | None = None, created_at: int | None = None
    ) -> bool:
        rid = to_int_string(resource_id)
        if rid is None:
            raise ValueError(f"Not a Hub resource id: {resource_id!r}")
        payload = json.dumps(data)
        with self.transaction():
            if self._one("SELECT 1 FROM bookmarks WHERE resource_id = ?", (rid,)):
                self._run(
                    "UPDATE bookmarks SET hub_json = ?, note = COALESCE(?, note) WHERE resource_id = ?",
                    (payload, note, rid),
                )
                return False
            self._run(
                "INSERT INTO bookmarks (resource_id, hub_json, note, created_at) VALUES (?, ?, ?, ?)",
                (rid, payload, note, now() if created_at is None else int(created_at)),
            )
            return True

    def set_bookmark_note(self, resource_id: Any, note: str | None) -> int:
        rid = to_int_string(resource_id)
        if rid is None:
            return 0
        text = (note or "").strip() or None
        return self._run("UPDATE bookmarks SET note = ? WHERE resource_id = ?", (text, rid)).rowcount

    def delete_bookmark(self, resource_id: Any) -> int:
        rid = to_int_string(resource_id)
        if rid is None:
            return 0
        return self._run("DELETE FROM bookmarks WHERE resource_id = ?", (rid,)).rowcount

    def count_bookmarks(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM bookmarks")["n"]
