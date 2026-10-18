from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .content_types import VISIBLE_CATEGORIES
from .db import Database
from .events import EventBus
from .graph import (
    compute_cascade_disable,
    compute_cascade_enable,
    compute_removable_deps,
    get_transitive_deps,
    parse_dep_ref,
)
from .library_dirs import LibraryDirs, library_rel_subpath
from .paths import is_local_package, is_package_active, is_package_archived, user_data_dir
from .prefs import read_all_prefs, set_favorite, set_hidden
from .scanner import (
    ScanSummary,
    apply_auto_hide_rule,
    inherit_from_older_version,
    remove_auto_hide_rule,
    run_integrity_check,
    run_local_scan,
    run_scan,
    scan_and_upsert,
    sync_auto_hide_after_direct_change,
)
from .storage_state import (
    StorageStateError,
    StorageTarget,
    apply_storage_state,
    compute_install_target,
    disable_target_for,
    next_storage_state_for_intent,
    parse_disable_behavior,
    plan_resettle,
)
from .store import Store
from .textures import run_texture_check
from .unpack import has_backup, repack_package, restore_original, unpack_package
from .varfile import qvaro_disabled_name, read_var

log = logging.getLogger(__name__)

ALLOWED_TYPE_OVERRIDES = frozenset(VISIBLE_CATEGORIES) | {"Other"}


def _names(arg) -> list[str]:
    return [arg] if isinstance(arg, str) else list(arg)


class Library:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path) if db_path else user_data_dir() / "backstage.db"
        self.db = Database(path)
        self.dirs = LibraryDirs(self.db)
        self.store = Store(self.db)
        self.store.strict_versions = self.db.get_setting("strict_versions") != "0"
        self.events = EventBus()
        self.lock = threading.RLock()
        self._scan_lock = threading.RLock()
        self.on_new_packages: Callable[[list[str]], None] | None = None
        self.install_missing_hook: Callable[[str], int] | None = None

    def close(self) -> None:
        self.db.close()

    @property
    def vam_dir(self) -> str | None:
        return self.db.get_setting("vam_dir")

    def set_vam_dir(self, path: str) -> None:
        self.db.set_setting("vam_dir", path)
        self.dirs.refresh()

    @property
    def is_configured(self) -> bool:
        return bool(self.vam_dir)

    @property
    def initial_scan_done(self) -> bool:
        return self.db.get_setting("initial_scan_done") == "1"

    def notify(self, channel: str, payload=None) -> None:
        self.events.emit(channel, payload)

    def get_setting(self, key: str) -> str | None:
        return self.db.get_setting(key)

    def set_setting(self, key: str, value: str | None) -> None:
        self.db.set_setting(key, value)
        self.notify("settings:updated", key)

    def _require_vam_dir(self) -> str:
        v = self.vam_dir
        if not v:
            raise RuntimeError("VaM directory not configured")
        return v

    def load(self) -> None:
        with self.lock:
            self.dirs.refresh()
            if self.vam_dir:
                self.store.set_prefs_map(read_all_prefs(self.vam_dir))
            self.store.build_from_db()
            self.db.gc_orphan_labels()

    def hold_scans(self):
        return self._scan_lock

    def scan(self, on_progress=None) -> ScanSummary:
        with self._scan_lock:
            return self._scan(on_progress)

    def _scan(self, on_progress=None) -> ScanSummary:
        was_initial = not self.initial_scan_done

        def progress(p):
            self.notify("scan:progress", p)
            if on_progress:
                on_progress(p)

        summary = run_scan(self, progress)
        self.notify("packages:updated")
        self.notify("contents:updated")
        if summary.added_filenames and self.on_new_packages and not was_initial:
            try:
                self.on_new_packages(summary.added_filenames)
            except Exception:
                log.exception("new-package hook failed")
        return summary

    def list_packages(self) -> list[dict]:
        with self.lock:
            return self.store.get_filtered_packages()

    def package_detail(self, filename: str) -> dict | None:
        with self.lock:
            detail = self.store.get_package_detail(filename)
            pkg = self.store.package_index.get(filename)
            path = self.dirs.resolve_content_path(pkg) if detail and pkg and not is_local_package(filename) else None
        if detail is not None:
            detail["package_path"] = path
            detail["has_backup"] = bool(path) and has_backup(path)
        return detail

    def stats(self) -> dict:
        with self.lock:
            return dict(self.store.stats)

    def status_counts(self) -> dict:
        with self.lock:
            return self.store.get_status_counts()

    def local_demand_summary(self) -> dict | None:
        with self.lock:
            return self.store.local_demand_summary()

    def local_summary(self) -> dict | None:
        with self.lock:
            return self.store.local_summary()

    def placeholder_counts(self) -> dict:
        with self.lock:
            return self.store.placeholder_counts()

    def replace_placeholder(
        self,
        filename: str,
        source_path: str,
        *,
        move: bool = False,
        source_name: str | None = None,
        is_direct: bool | None = None,
    ) -> dict:
        from .dummy import ALIASES_SUBDIR
        from .varfile import canonical_var_filename, verify_zip_file

        src_name = canonical_var_filename(source_name or os.path.basename(source_path))
        if src_name.lower() != filename.lower():
            raise ValueError(
                f"That file is {src_name}; the placeholder is {filename}. Only the same package and version can replace it."
            )
        if not os.path.isfile(source_path):
            raise ValueError("Pick a .var file")
        if not verify_zip_file(source_path):
            raise ValueError(f"{src_name} is not a valid .var (zip) file")
        with self._scan_lock, self.lock:
            self._require_vam_dir()
            pkg = self.store.package_index.get(filename)
            if not pkg:
                raise ValueError(f"{filename} is not in the library")
            kind = (
                "dummy"
                if pkg.get("is_dummy")
                else ("alias" if (pkg.get("subpath") or "").split("/")[0] == ALIASES_SUBDIR else None)
            )
            if kind is None:
                raise ValueError(f"{filename} is not a placeholder")
            dst = os.path.join(self.dirs.main_dir_path(), filename)
            current = self.dirs.resolve_content_path(pkg)
            if os.path.exists(dst) and os.path.realpath(dst) != os.path.realpath(current or dst):
                raise ValueError(f"{dst} already exists")
            direct = bool(pkg.get("is_direct")) or bool(is_direct)
            tmp = dst + ".tmp"
            if move:
                if os.path.realpath(source_path) != os.path.realpath(tmp):
                    os.replace(source_path, tmp)
            else:
                shutil.copy2(source_path, tmp)
            self._unlink_package_physical_and_aliases(pkg, filename)
            os.replace(tmp, dst)
            res = self.integrate_new_var(dst, is_direct=direct)
        log.info("placeholder %s (%s) replaced by %s", filename, kind, source_path)
        return {"filename": filename, "replaced": kind, "is_direct": direct, "landed": (res or {}).get("landed")}

    def type_counts(self) -> dict:
        with self.lock:
            return self.store.get_type_counts()

    def list_contents(self, package_filename: str | None = None) -> list[dict]:
        with self.lock:
            return self.store.get_filtered_contents(package_filename)

    def content_type_counts(self) -> dict:
        with self.lock:
            return self.store.get_content_type_counts()

    def content_visibility_counts(self) -> dict:
        with self.lock:
            return self.store.get_content_visibility_counts()

    def missing_deps(self, hub_packages_index=None, hub_filename_index=None) -> list[dict]:
        with self.lock:
            return self.store.get_missing_deps(hub_packages_index, hub_filename_index)

    def labels(self) -> list[dict]:
        with self.lock:
            return self.store.get_label_list()

    def file_list(self, filename: str) -> dict:
        with self.lock:
            pkg = self.store.package_index.get(filename)
            if not pkg:
                raise KeyError(f"Package not found: {filename}")
            path = self.dirs.resolve_content_path(pkg)
        if not path or not os.path.exists(path):
            raise FileNotFoundError(path or filename)
        info = read_var(path)
        return {"file_list": [(f.path, f.size) for f in info.file_list], "var_path": path}

    def package_path(self, filename: str) -> str | None:
        with self.lock:
            pkg = self.store.package_index.get(filename)
            return self.dirs.resolve_content_path(pkg) if pkg else None

    def _find_content(self, package_filename: str, internal_path: str) -> dict | None:
        for c in self.store.content_by_package.get(package_filename, ()):
            if c["internal_path"] == internal_path:
                return c
        return None

    def toggle_hidden(self, package_filename: str, internal_path: str) -> bool:
        with self.lock:
            vam = self._require_vam_dir()
            item = self._find_content(package_filename, internal_path)
            if item is None:
                raise KeyError("Content item not found")
            new = not item["hidden"]
            set_hidden(vam, package_filename, internal_path, new)
            self.store.update_pref(package_filename, internal_path, "hidden", new)
        self.notify("contents:updated")
        return new

    def toggle_favorite(self, package_filename: str, internal_path: str) -> bool:
        with self.lock:
            vam = self._require_vam_dir()
            item = self._find_content(package_filename, internal_path)
            if item is None:
                raise KeyError("Content item not found")
            new = not item["favorite"]
            set_favorite(vam, package_filename, internal_path, new)
            self.store.update_pref(package_filename, internal_path, "favorite", new)
        self.notify("contents:updated")
        self.notify("packages:updated")
        return new

    def set_hidden_batch(self, items: Iterable[tuple[str, str]], hidden: bool) -> None:
        with self.lock:
            vam = self._require_vam_dir()
            for pf, ip in items:
                set_hidden(vam, pf, ip, hidden)
                self.store.update_pref(pf, ip, "hidden", hidden)
        self.notify("contents:updated")

    def set_favorite_batch(self, items: Iterable[tuple[str, str]], favorite: bool) -> None:
        with self.lock:
            vam = self._require_vam_dir()
            for pf, ip in items:
                set_favorite(vam, pf, ip, favorite)
                self.store.update_pref(pf, ip, "favorite", favorite)
        self.notify("contents:updated")
        self.notify("packages:updated")

    def _disable_target(self) -> StorageTarget:
        return disable_target_for(parse_disable_behavior(self.db.get_setting("disable_behavior")))

    def _apply_storage_state_change(self, filenames: list[str], intent_fn: Callable[[dict], str]) -> list[dict]:
        self._require_vam_dir()
        disable_target = self._disable_target()
        store = self.store
        out = []
        cleared_archive = False
        for filename in filenames:
            pkg = store.package_index.get(filename)
            if not pkg:
                out.append({"ok": False, "filename": filename, "error": f"Package not found: {filename}"})
                continue
            intent = intent_fn(pkg)
            target = next_storage_state_for_intent(pkg["storage_state"], intent, disable_target)
            if target is None:
                out.append(
                    {
                        "ok": True,
                        "filename": filename,
                        "storage_state": pkg["storage_state"],
                        "cascade_count": 0,
                        "unchanged": True,
                    }
                )
                continue
            if intent == "enable":
                cascade = compute_cascade_enable(filename, store.package_index, store.forward_deps)
            else:
                cascade = compute_cascade_disable(filename, store.package_index, store.forward_deps, store.reverse_deps)
            if is_package_archived(pkg["storage_state"]):
                cleared_archive = True
            try:
                apply_storage_state(self, filename, target)
            except (StorageStateError, OSError) as err:
                out.append({"ok": False, "filename": filename, "error": str(err)})
                continue
            for dep in sorted(cascade):
                dep_pkg = store.package_index.get(dep)
                if not dep_pkg:
                    continue
                dep_target = next_storage_state_for_intent(dep_pkg["storage_state"], intent, disable_target)
                if dep_target is None:
                    continue
                if is_package_archived(dep_pkg["storage_state"]):
                    cleared_archive = True
                try:
                    apply_storage_state(self, dep, dep_target)
                except (StorageStateError, OSError) as err:
                    log.warning("Cascade %s failed for %s: %s", intent, dep, err)
            out.append(
                {"ok": True, "filename": filename, "storage_state": target.storage_state, "cascade_count": len(cascade)}
            )
        if cleared_archive:
            store.recompute_demand_aggregates()
        else:
            store.recompute_inactive_deps()
        return out

    def toggle_enabled(self, filenames) -> list[dict]:
        with self.lock:
            res = self._apply_storage_state_change(
                _names(filenames), lambda p: "disable" if p["storage_state"] == "enabled" else "enable"
            )
        self.notify("packages:updated")
        return res

    def set_enabled(self, filenames, enabled: bool) -> list[dict]:
        intent = "enable" if enabled else "disable"
        with self.lock:
            res = self._apply_storage_state_change(_names(filenames), lambda p: intent)
        self.notify("packages:updated")
        return res

    def enable_deps(self, filenames) -> dict:
        with self.lock:
            to_enable: set[str] = set()
            for fn in _names(filenames):
                to_enable |= compute_cascade_enable(fn, self.store.package_index, self.store.forward_deps)
            if not to_enable:
                return {"ok": True, "count": 0}
            res = self._apply_storage_state_change(sorted(to_enable), lambda p: "enable")
        self.notify("packages:updated")
        return {"ok": True, "count": len(to_enable), "results": res}

    def promote(self, filenames) -> dict:
        names = _names(filenames)
        with self.lock:
            self._require_vam_dir()
            idx = self.store.package_index
            stay_archived = {fn for fn in names if idx.get(fn) and is_package_archived(idx[fn]["storage_state"])}
            to_enable = [
                fn
                for fn in names
                if fn not in stay_archived and idx.get(fn) and not is_package_active(idx[fn]["storage_state"])
            ]
            if to_enable:
                self._apply_storage_state_change(to_enable, lambda p: "enable")
            for fn in names:
                if fn not in idx:
                    raise KeyError(f"Package not found: {fn}")
                self.db.set_package_direct(fn, True)
                self.store.patch_direct(fn, True)
                if fn not in stay_archived:
                    self.db.touch_package_first_seen(fn)
                sync_auto_hide_after_direct_change(self, fn, True)
            self.store.build_from_db(skip_graph=True)
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "count": len(names)}

    def demote(self, filenames) -> dict:
        names = _names(filenames)
        with self.lock:
            self._require_vam_dir()
            for fn in names:
                if fn not in self.store.package_index:
                    raise KeyError(f"Package not found: {fn}")
                self.db.set_package_direct(fn, False)
                self.store.patch_direct(fn, False)
                sync_auto_hide_after_direct_change(self, fn, False)
            self.store.build_from_db(skip_graph=True)
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "count": len(names)}

    def set_type_override(self, filenames, type_override: str | None) -> dict:
        names = _names(filenames)
        if type_override is not None and type_override not in ALLOWED_TYPE_OVERRIDES:
            raise ValueError("Invalid package type")
        with self.lock:
            for fn in names:
                if fn not in self.store.package_index:
                    raise KeyError(f"Package not found: {fn}")
                self.db.set_package_type_override(fn, type_override)
                self.store.patch_type_override(fn, type_override)
        self.notify("packages:updated")
        return {"ok": True, "count": len(names)}

    def mark_recent(self, filenames) -> dict:
        names = _names(filenames)
        now = time.time()
        with self.lock:
            for fn in names:
                pkg = self.store.package_index.get(fn)
                if not pkg:
                    raise KeyError(f"Package not found: {fn}")
                path = self.dirs.resolve_content_path(pkg)
                if not path:
                    raise FileNotFoundError(fn)
                os.utime(path, (now, now))
                mtime = os.stat(path).st_mtime
                self.db._run(
                    "UPDATE packages SET first_seen_at = ?, file_mtime = ? WHERE filename = ?", (int(mtime), mtime, fn)
                )
                pkg["first_seen_at"] = int(mtime)
                pkg["file_mtime"] = mtime
        self.notify("packages:updated")
        return {"ok": True, "count": len(names)}

    def _package_on_disk(self, filename: str) -> tuple[dict, str]:
        pkg = self.store.package_index.get(filename)
        if not pkg or is_local_package(filename):
            raise KeyError(f"Package not found: {filename}")
        if pkg.get("is_dummy"):
            raise ValueError(f"{filename} is a dummy package: there is nothing inside")
        path = self.dirs.resolve_content_path(pkg)
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"{filename} is not on disk")
        if os.path.basename(path) != filename:
            raise ValueError(f"{filename} is disabled by a renamed file ({os.path.basename(path)}); enable it first")
        return pkg, path

    def reindex_package(self, filename: str) -> dict | None:
        detail = None
        with self.lock:
            pkg = self.store.package_index.get(filename)
            if pkg and pkg.get("hub_resource_id"):
                cached = self.db.get_hub_resource(pkg["hub_resource_id"])
                if cached and cached.get("hub_json"):
                    try:
                        detail = json.loads(cached["hub_json"])
                    except ValueError:
                        detail = None
        return self.refresh_package_file(filename, detail)

    def unpack_package(self, filename: str) -> dict:
        with self._scan_lock, self.lock:
            self._require_vam_dir()
            _pkg, path = self._package_on_disk(filename)
            if os.path.isdir(path):
                raise ValueError(f"{filename} is already unpacked")
            res = unpack_package(path)
            self.reindex_package(filename)
        log.info("unpacked %s: %d files; backup %s", filename, res.files, "created" if res.backup_created else "kept")
        return {
            "filename": filename,
            "folder": res.folder,
            "backup": res.backup,
            "backup_created": res.backup_created,
            "files": res.files,
            "bytes": res.bytes,
        }

    def repack_package(self, filename: str, keep_backup: bool = False) -> dict:
        with self._scan_lock, self.lock:
            self._require_vam_dir()
            _pkg, path = self._package_on_disk(filename)
            if not os.path.isdir(path):
                raise ValueError(f"{filename} is not unpacked")
            res = repack_package(path, keep_backup=keep_backup)
            self.reindex_package(filename)
        log.info("repacked %s: %d files, %d bytes", filename, res.files, res.bytes)
        return {
            "filename": filename,
            "archive": res.archive,
            "files": res.files,
            "bytes": res.bytes,
            "backup_removed": res.backup_removed,
        }

    def restore_original(self, filename: str) -> dict:
        with self._scan_lock, self.lock:
            self._require_vam_dir()
            _pkg, path = self._package_on_disk(filename)
            res = restore_original(path)
            self.reindex_package(filename)
        log.info("restored the original %s (%s discarded)", filename, res.discarded or "nothing")
        return {"filename": filename, "archive": res.archive, "discarded": res.discarded}

    def unpacked_folder(self, filename: str) -> str:
        with self.lock:
            _pkg, path = self._package_on_disk(filename)
        if not os.path.isdir(path):
            raise ValueError(f"{filename} is not unpacked")
        return path

    def _unlink_package_physical_and_aliases(self, pkg: dict | None, filename: str) -> None:
        targets = []
        physical = self.dirs.pkg_var_path(pkg) if pkg else None
        if physical:
            targets += [
                physical,
                physical + ".disabled",
                qvaro_disabled_name(physical),
                physical + ".orig",
                physical + ".depend.txt",
            ]
            content = self.dirs.resolve_content_path(pkg)
            if content:
                targets.append(content)
        main = self.dirs.main_dir_path()
        if main:
            root = os.path.join(main, filename)
            targets += [root, root + ".disabled", qvaro_disabled_name(root)]
        seen = set()
        for p in targets:
            if p in seen:
                continue
            seen.add(p)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            except FileNotFoundError:
                pass
            except OSError as err:
                log.warning("unlink %s failed: %s", p, err)

    def _resettle_deps(self, candidates: Iterable[str], prune: bool = True) -> dict:
        behavior = parse_disable_behavior(self.db.get_setting("disable_behavior"))
        target_id = behavior.aux_dir_id if behavior.kind == "move-to" else None
        plan = plan_resettle(
            candidates, self.store.package_index, self.store.reverse_deps, self.store.replaceable_set, target_id, prune
        )
        relocated = settled = 0
        for fn, target in plan.decisions.items():
            try:
                apply_storage_state(self, fn, target)
                if target.storage_state == "archived":
                    relocated += 1
                else:
                    settled += 1
            except (StorageStateError, OSError) as err:
                log.warning("Re-settle failed for %s: %s", fn, err)
        pruned = 0
        for fn in sorted(plan.to_prune):
            self._unlink_package_physical_and_aliases(self.store.package_index.get(fn), fn)
            self.db.delete_package(fn)
            pruned += 1
        return {"pruned": pruned, "relocated_to_archive": relocated, "settled_down": settled}

    def uninstall(self, filenames) -> dict:
        names = _names(filenames)
        with self.lock:
            self._require_vam_dir()
            store = self.store
            results = []
            resettle: set[str] = set()
            for filename in names:
                pkg = store.package_index.get(filename)
                if not pkg:
                    raise KeyError(f"Package not found: {filename}")
                replaceable = store.replaceable_set
                dependents = store.reverse_deps.get(filename, set())
                non_archived = [
                    d
                    for d in dependents
                    if store.package_index.get(d) and not is_package_archived(store.package_index[d]["storage_state"])
                ]
                if non_archived:
                    was_direct = bool(pkg.get("is_direct"))
                    if was_direct:
                        self.db.set_package_direct(filename, False)
                        store.patch_direct(filename, False)
                        sync_auto_hide_after_direct_change(self, filename, False)
                        store.build_from_db(skip_graph=True)
                        resettle.add(filename)
                    results.append(
                        {
                            "ok": True,
                            "filename": filename,
                            "demoted": True,
                            "already_dependency": not was_direct,
                            "needed_by": sorted(non_archived),
                        }
                    )
                    continue
                if dependents and filename not in replaceable:
                    target = compute_install_target(dependents, store.package_index)
                    if target and target.storage_state == "archived":
                        try:
                            transitive = get_transitive_deps(filename, store.forward_deps)
                            apply_storage_state(self, filename, target)
                            store.build_from_db()
                            resettle |= transitive
                            results.append({"ok": True, "filename": filename, "relocated_to_archive": True})
                            continue
                        except (StorageStateError, OSError) as err:
                            log.warning("Relocate-to-archive on uninstall failed for %s: %s", filename, err)
                removable = compute_removable_deps(
                    filename, store.package_index, store.forward_deps, store.reverse_deps, replaceable
                )
                transitive = get_transitive_deps(filename, store.forward_deps)
                to_delete = [filename] + sorted(fn for fn in removable.removable_filenames if fn in replaceable)
                for fn in to_delete:
                    self._unlink_package_physical_and_aliases(store.package_index.get(fn), fn)
                    self.db.delete_package(fn)
                resettle |= {d for d in transitive if d not in to_delete}
                store.build_from_db()
                results.append(
                    {"ok": True, "filename": filename, "deleted": len(to_delete), "deleted_filenames": to_delete}
                )
            resettled = None
            if resettle:
                res = self._resettle_deps(resettle)
                if res["pruned"] or res["relocated_to_archive"] or res["settled_down"]:
                    store.build_from_db()
                    resettled = res
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "results": results, "resettled": resettled}

    def force_remove(self, filenames) -> dict:
        names = _names(filenames)
        with self.lock:
            for fn in names:
                self._unlink_package_physical_and_aliases(self.store.package_index.get(fn), fn)
                self.db.delete_package(fn)
            self.store.build_from_db()
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "count": len(names)}

    def remove_orphans(self) -> dict:
        with self.lock:
            return self._delete_unneeded(set(self.store.orphan_set))

    def remove_superseded(self) -> dict:
        with self.lock:
            return self._delete_unneeded(set(self.store.superseded_set))

    def _delete_unneeded(self, filenames: set[str]) -> dict:
        store = self.store
        if not filenames:
            return {"ok": True, "count": 0, "freed_bytes": 0, "moved_to_archive": 0}
        resettle: set[str] = set()
        for fn in filenames:
            resettle |= {d for d in get_transitive_deps(fn, store.forward_deps) if d not in filenames}
        freed = 0
        for fn in sorted(filenames):
            pkg = store.package_index.get(fn)
            if pkg:
                freed += pkg.get("size_bytes") or 0
            self._unlink_package_physical_and_aliases(pkg, fn)
            self.db.delete_package(fn)
        store.build_from_db()
        moved = 0
        if resettle:
            res = self._resettle_deps(resettle)
            moved = res["relocated_to_archive"]
            if res["pruned"] or res["relocated_to_archive"] or res["settled_down"]:
                store.build_from_db()
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "count": len(filenames), "freed_bytes": freed, "moved_to_archive": moved}

    def archive_preview(self, filenames, archive_dir_id: int | None = None) -> dict:
        names = _names(filenames)
        with self.lock:
            store = self.store
            archive_dirs = self.dirs.archive_library_dirs()
            dir_id = archive_dir_id if archive_dir_id is not None else (archive_dirs[0]["id"] if archive_dirs else None)
            batch = {
                fn
                for fn in names
                if store.package_index.get(fn) and not is_package_archived(store.package_index[fn]["storage_state"])
            }
            overlay = {
                fn: {**store.package_index[fn], "storage_state": "archived", "library_dir_id": dir_id} for fn in batch
            }

            class Sim:
                def get(self_inner, fn):
                    return overlay.get(fn) or store.package_index.get(fn)

            closure: set[str] = set()
            for fn in batch:
                closure |= get_transitive_deps(fn, store.forward_deps)
            behavior = parse_disable_behavior(self.db.get_setting("disable_behavior"))
            target_id = behavior.aux_dir_id if behavior.kind == "move-to" else None

            def summarize(prune: bool) -> dict:
                plan = plan_resettle(closure, Sim(), store.reverse_deps, store.replaceable_set, target_id, prune)
                t = {"delete_count": 0, "delete_bytes": 0, "store_count": 0, "store_bytes": 0}
                for fn in plan.to_prune:
                    p = store.package_index.get(fn)
                    if p:
                        t["delete_count"] += 1
                        t["delete_bytes"] += p.get("size_bytes") or 0
                for fn, target in plan.decisions.items():
                    if target.storage_state != "archived":
                        continue
                    p = store.package_index.get(fn)
                    if p:
                        t["store_count"] += 1
                        t["store_bytes"] += p.get("size_bytes") or 0
                return t

            return {
                "archive_count": len(batch),
                "prune": summarize(True),
                "store": summarize(False),
                "catalog_unavailable": store.hub_filename_index is None,
            }

    def archive(self, filenames, archive_dir_id: int | None = None, dep_mode: str = "prune") -> dict:
        names = _names(filenames)
        with self.lock:
            self._require_vam_dir()
            archive_dirs = self.dirs.archive_library_dirs()
            if not archive_dirs:
                raise RuntimeError("No archive directory configured")
            dir_id = archive_dir_id if archive_dir_id is not None else archive_dirs[0]["id"]
            if not self.dirs.is_archive_library_dir(dir_id):
                raise ValueError(f"Not an archive directory: {archive_dir_id}")
            store = self.store
            archived = []
            for fn in names:
                pkg = store.package_index.get(fn)
                if not pkg:
                    raise KeyError(f"Package not found: {fn}")
                if is_package_archived(pkg["storage_state"]):
                    continue
                try:
                    apply_storage_state(self, fn, StorageTarget("archived", dir_id))
                    archived.append(fn)
                except (StorageStateError, OSError) as err:
                    log.warning("Archive move failed for %s: %s", fn, err)
            store.build_from_db()
            closure: set[str] = set()
            for fn in archived:
                closure |= get_transitive_deps(fn, store.forward_deps)
            res = {"pruned": 0, "relocated_to_archive": 0, "settled_down": 0}
            if closure:
                res = self._resettle_deps(closure, prune=(dep_mode != "store"))
                store.build_from_db()
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {
            "ok": True,
            "archived": len(archived),
            "pruned": res["pruned"],
            "stored_to_archive": res["relocated_to_archive"],
            "settled_down": res["settled_down"],
        }

    def install_from_archive(self, filenames) -> dict:
        with self.lock:
            self._require_vam_dir()
            idx = self.store.package_index
            names = [fn for fn in _names(filenames) if idx.get(fn) and is_package_archived(idx[fn]["storage_state"])]
            if not names:
                return {"ok": True, "count": 0, "queued": 0}
            self._apply_storage_state_change(names, lambda p: "enable")
            for fn in names:
                self.db.set_package_direct(fn, True)
                self.store.patch_direct(fn, True)
                self.db.touch_package_first_seen(fn)
                sync_auto_hide_after_direct_change(self, fn, True)
            self.store.build_from_db(skip_graph=True)
        queued = 0
        if self.install_missing_hook:
            for fn in names:
                try:
                    queued += int(self.install_missing_hook(fn) or 0)
                except Exception as err:
                    log.warning("Install-from-archive dep enqueue failed for %s: %s", fn, err)
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "count": len(names), "queued": queued}

    def list_library_dirs(self) -> dict:
        with self.lock:
            self.dirs.refresh()
            main = self.dirs.main_dir_path()
            aux = []
            for row in self.dirs.aux_library_dirs():
                stats = self.db.count_packages_in_library_dir(row["id"])
                aux.append(
                    {
                        "id": row["id"],
                        "path": row["path"],
                        "archive": bool(row["archive"]),
                        "package_count": stats["n"],
                        "bytes": stats["bytes"],
                        "exists": os.path.isdir(row["path"]),
                    }
                )
            main_stats = self.db.count_packages_in_library_dir(None)
            return {"main": {"path": main, "package_count": main_stats["n"], "bytes": main_stats["bytes"]}, "aux": aux}

    def add_library_dir(self, path: str, archive: bool = False) -> int:
        path = os.path.abspath(path)
        with self.lock:
            self._require_vam_dir()
            if not os.path.isdir(path):
                raise ValueError("Folder does not exist")
            err = self.dirs.validate_new_aux_dir_path(path)
            if err:
                raise ValueError(err)
            if not self.dirs.same_filesystem_as_main(path):
                raise ValueError(
                    "The folder must be on the same drive/filesystem as AddonPackages so packages can be moved without copying"
                )
            if self.db.get_library_dir_by_path(path):
                raise ValueError("That folder is already registered")
            dir_id = self.db.insert_library_dir(path, archive)
            self.dirs.refresh()
        self.notify("library-dirs:updated")
        return dir_id

    def remove_library_dir(self, dir_id: int, force: bool = False) -> dict:
        with self.lock:
            row = self.db.get_library_dir(dir_id)
            if not row:
                raise KeyError(f"Library dir not found: {dir_id}")
            count = self.db.count_packages_in_library_dir(dir_id)["n"]
            if count and not force:
                raise ValueError(
                    f"The folder still holds {count} package(s). Move them out first, or force-remove to forget them."
                )
            tombstoned = self.db.remove_library_dir_tombstoning_packages(dir_id)
            behavior = parse_disable_behavior(self.db.get_setting("disable_behavior"))
            if behavior.kind == "move-to" and behavior.aux_dir_id == dir_id:
                self.db.set_setting("disable_behavior", "suffix")
            self.dirs.refresh()
            self.store.build_from_db()
        self.notify("library-dirs:updated")
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"ok": True, "tombstoned": tombstoned}

    def set_library_dir_role(self, dir_id: int, archive: bool) -> int:
        with self.lock:
            changed = self.db.set_library_dir_role(dir_id, archive)
            if archive:
                behavior = parse_disable_behavior(self.db.get_setting("disable_behavior"))
                if behavior.kind == "move-to" and behavior.aux_dir_id == dir_id:
                    self.db.set_setting("disable_behavior", "suffix")
            self.dirs.refresh()
            self.store.build_from_db()
        self.notify("library-dirs:updated")
        self.notify("packages:updated")
        return changed

    def apply_auto_hide_rule(self, rule_id: str, on_progress=None) -> None:
        with self.lock:
            self._require_vam_dir()
            apply_auto_hide_rule(self, rule_id, on_progress)
            self.store.recompute_inactive_deps()
            self.store._compute_stats()
        self.notify("contents:updated")

    def remove_auto_hide_rule(self, rule_id: str, on_progress=None) -> None:
        with self.lock:
            self._require_vam_dir()
            remove_auto_hide_rule(self, rule_id, on_progress)
            self.store._compute_stats()
        self.notify("contents:updated")

    def integrity_check(self, on_progress=None, cancel=None) -> dict:
        self._require_vam_dir()
        result = run_integrity_check(self, on_progress, cancel)
        with self.lock:
            bad = set(result["corrupted_files"])
            self.db.batch_set_corrupted([(fn, fn in bad) for fn in result["checked_files"]])
            self.store.build_from_db()
        self.notify("packages:updated")
        return result

    def texture_check(self, on_progress=None, cancel=None, filenames=None, include_deps: bool = False) -> dict:
        self._require_vam_dir()
        names = None
        if filenames is not None:
            with self.lock:
                names = set(_names(filenames))
                if include_deps:
                    for fn in list(names):
                        names |= get_transitive_deps(fn, self.store.forward_deps)
        return run_texture_check(self, on_progress, cancel, names)

    def rescan_local_content(self) -> dict:
        res = run_local_scan(self)
        with self.lock:
            self.store.build_from_db(skip_graph=not res.get("refs_changed"))
        self.notify("contents:updated")
        if res.get("refs_changed"):
            self.notify("packages:updated")
        return res

    def create_dummies(self, refs, allow_shadow: bool = False) -> dict:
        from .dummy import DUMMIES_SUBDIR, build_dummy_var, dummy_filename_for_ref

        created: list[str] = []
        skipped: list[tuple[str, str]] = []
        paths: list[str] = []
        with self.lock:
            self._require_vam_dir()
            folder = os.path.join(self.dirs.main_dir_path(), DUMMIES_SUBDIR)
            for ref in _names(refs):
                parsed = parse_dep_ref(ref)
                if parsed is None:
                    skipped.append((ref, "not a package reference (expected Creator.Package.N)"))
                    continue
                filename = dummy_filename_for_ref(parsed)
                if filename in self.store.package_index or filename in created:
                    skipped.append((ref, "already installed"))
                    continue
                installed = sorted(self.store.group_index.get(parsed.package_name) or [])
                if installed and not allow_shadow:
                    skipped.append((ref, "a version of this package is installed; an empty one would shadow it"))
                    continue
                depends_on = tuple(fn[:-4] for fn in installed if fn.lower().endswith(".var"))
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, filename)
                with open(path, "wb") as f:
                    f.write(build_dummy_var(parsed, ref, depends_on))
                created.append(filename)
                paths.append(path)
        for path in paths:
            self.integrate_new_var(path, is_direct=False)
        log.info("dummy packages: %d created, %d skipped", len(created), len(skipped))
        return {"created": created, "skipped": skipped}

    def create_aliases(self, refs, sources: dict[str, str] | None = None) -> dict:
        from .dummy import ALIASES_SUBDIR, dummy_filename_for_ref
        from .graph import pick_highest_version

        created: list[dict] = []
        skipped: list[tuple[str, str]] = []
        paths: list[str] = []
        with self.lock:
            self._require_vam_dir()
            folder = os.path.join(self.dirs.main_dir_path(), ALIASES_SUBDIR)
            for ref in _names(refs):
                parsed = parse_dep_ref(ref)
                if parsed is None:
                    skipped.append((ref, "not a package reference (expected Creator.Package.N)"))
                    continue
                target = dummy_filename_for_ref(parsed)
                if target in self.store.package_index or any(c["filename"] == target for c in created):
                    skipped.append((ref, "already installed"))
                    continue
                source = (sources or {}).get(ref)
                if source and source not in self.store.package_index:
                    skipped.append((ref, f"{source} is not in the library"))
                    continue
                if not source:
                    source = pick_highest_version(
                        self.store.group_index.get(parsed.package_name) or [], self.store.package_index
                    )
                if not source:
                    skipped.append((ref, "no version of this package is installed to alias"))
                    continue
                src_path = self.dirs.resolve_content_path(self.store.package_index[source])
                if not src_path or not os.path.exists(src_path):
                    skipped.append((ref, f"{source} is not on disk"))
                    continue
                os.makedirs(folder, exist_ok=True)
                dst = os.path.join(folder, target)
                linked = False
                try:
                    if os.path.isdir(src_path):
                        shutil.copytree(src_path, dst)
                    else:
                        try:
                            os.link(src_path, dst)
                            linked = True
                        except OSError:
                            shutil.copy2(src_path, dst)
                except OSError as err:
                    skipped.append((ref, f"could not copy {source}: {err}"))
                    continue
                created.append({"filename": target, "source": source, "linked": linked})
                paths.append(dst)
        for path in paths:
            self.integrate_new_var(path, is_direct=False)
        log.info("alias packages: %d created, %d skipped", len(created), len(skipped))
        return {"created": created, "skipped": skipped}

    def set_strict_versions(self, on: bool) -> None:
        with self.lock:
            self.db.set_setting("strict_versions", "1" if on else "0")
            self.store.strict_versions = on
            self.store.build_from_db()
        self.notify("packages:updated")

    def refresh_package_file(self, filename: str, hub_detail: dict | None = None) -> dict | None:
        with self._scan_lock, self.lock:
            self._require_vam_dir()
            pkg = self.store.package_index.get(filename)
            if not pkg:
                return None
            path = self.dirs.resolve_content_path(pkg)
            if not path or not os.path.exists(path):
                return None
            row = self.db.get_package(filename) or {}
            result = scan_and_upsert(
                self.db,
                path,
                storage_state=pkg["storage_state"],
                library_dir_id=pkg.get("library_dir_id"),
                subpath=pkg.get("subpath") or "",
                is_direct=bool(pkg.get("is_direct")),
                type_override=row.get("type_override"),
                first_seen_at=row.get("first_seen_at"),
            )
            if result is None:
                return None
            self.db.set_package_corrupted(filename, False)
            if hub_detail and not hub_detail.get("_unavailable"):
                self.db.apply_hub_detail_to_package(filename, hub_detail)
            self.store.build_from_db()
            sync_auto_hide_after_direct_change(self, filename, bool(pkg.get("is_direct")), result.content_items)
        self.notify("packages:updated")
        self.notify("contents:updated")
        return {"filename": filename, "package_name": result.package_name, "landed": pkg["storage_state"]}

    def integrate_new_var(
        self, full_path: str, *, is_direct: bool = False, hub_resource_id=None, hub_detail: dict | None = None
    ) -> dict | None:
        from .prefs import read_package_prefs
        from .varfile import canonical_var_filename

        with self._scan_lock, self.lock:
            vam = self._require_vam_dir()
            filename = canonical_var_filename(os.path.basename(full_path))
            prior = self.db.get_package_reconcile_info(filename)
            reborn_or_new = prior is None or prior["missing_since"] is not None
            hub_type = (hub_detail or {}).get("type")
            hub_type = hub_type.strip() if isinstance(hub_type, str) and hub_type.strip() else None
            result = scan_and_upsert(
                self.db,
                full_path,
                storage_state="enabled",
                library_dir_id=None,
                subpath=library_rel_subpath(self.dirs.main_dir_path() or "", full_path),
                is_direct=is_direct,
                type_override=hub_type,
            )
            if result is None:
                return None
            if is_direct:
                self.db.set_package_direct(filename, True)
                self.db.touch_package_first_seen(filename)
            elif reborn_or_new:
                self.db.set_package_direct(filename, False)
            if hub_resource_id:
                self.db.set_hub_resource_id(filename, hub_resource_id)
            if hub_detail and not hub_detail.get("_unavailable"):
                self.db.apply_hub_detail_to_package(filename, hub_detail)
                if hub_detail.get("user_id"):
                    self.db.upsert_hub_user(
                        hub_detail["user_id"],
                        hub_detail.get("username"),
                        {
                            "user_id": hub_detail["user_id"],
                            "username": hub_detail.get("username"),
                            "avatar_date": hub_detail.get("avatar_date"),
                        },
                    )
            inherited = inherit_from_older_version(self, filename, result.package_name, result.content_items)
            self.store.build_graph_only()
            pkg = self.store.package_index.get(filename) or {}
            if inherited:
                self.store.prefs_map.update(read_package_prefs(vam, filename))
            if not inherited or pkg.get("is_direct"):
                sync_auto_hide_after_direct_change(self, filename, bool(pkg.get("is_direct")), result.content_items)
            landed = "enabled"
            target = None
            if not (is_direct or pkg.get("is_direct")):
                behavior = parse_disable_behavior(self.db.get_setting("disable_behavior"))
                target = compute_install_target(
                    self.store.reverse_deps.get(filename),
                    self.store.package_index,
                    behavior.aux_dir_id if behavior.kind == "move-to" else None,
                )
                if target is not None:
                    try:
                        apply_storage_state(self, filename, target)
                        landed = target.storage_state
                    except (StorageStateError, OSError) as err:
                        log.warning("Landing %s at %s failed: %s", filename, target, err)
            if landed == "enabled":
                for dep in sorted(compute_cascade_enable(filename, self.store.package_index, self.store.forward_deps)):
                    dep_pkg = self.store.package_index.get(dep)
                    t = next_storage_state_for_intent(dep_pkg["storage_state"], "enable") if dep_pkg else None
                    if t is None:
                        continue
                    try:
                        apply_storage_state(self, dep, t)
                    except (StorageStateError, OSError) as err:
                        log.warning("Cascade enable failed for %s: %s", dep, err)
            self.store.build_from_db()
        self.notify("packages:updated")
        self.notify("contents:updated")
        if self.on_new_packages and not hub_resource_id:
            try:
                self.on_new_packages([filename])
            except Exception:
                log.exception("new-package hook failed")
        return {
            "filename": filename,
            "package_name": result.package_name,
            "landed": landed,
            "inherited": bool(inherited),
        }
