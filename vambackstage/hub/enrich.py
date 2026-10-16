from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .client import HubClient, HubError, resource_detail_matches_package_name
from .packages_json import PackagesJsonIndex

log = logging.getLogger(__name__)

_FETCH_WORKERS = 10
_LOOKUP_PROGRESS_STRIDE = 50
ProgressFn = Callable[[dict], None]


def _upsert_user(db, detail: dict) -> None:
    if detail.get("user_id"):
        db.upsert_hub_user(
            detail["user_id"],
            detail.get("username"),
            {
                "user_id": detail["user_id"],
                "username": detail.get("username"),
                "avatar_date": detail.get("avatar_date"),
            },
        )


def fetch_one_detail(lib, hub: HubClient, rid: str, filename: str) -> str:
    try:
        detail = hub.get_resource_detail(rid)
    except HubError as err:
        if err.not_found:
            log.info("%s is no longer on the Hub (resource %s)", filename, rid)
            lib.db.upsert_hub_resource_detail(rid, {"_unavailable": True, "_error": str(err)})
            lib.db.apply_hub_detail_to_package(filename, {"_unavailable": True})
            return "not_found"
        log.warning("Hub detail for %s (resource %s) failed: %s; will retry on the next scan", filename, rid, err)
        return "failed"
    except Exception:
        log.exception("Hub detail for %s (resource %s) failed", filename, rid)
        return "failed"
    _upsert_user(lib.db, detail)
    lib.db.apply_hub_detail_to_package(filename, detail)
    return "ok"


def resolve_one_by_name(lib, hub: HubClient, filename: str, package_name: str) -> bool:
    try:
        detail = hub.get_resource_detail_by_name(package_name)
    except HubError as err:
        log.warning("name lookup failed for %s: %s", package_name, err)
        return False
    except Exception:
        log.exception("name lookup failed for %s", package_name)
        return False
    if detail and detail.get("resource_id"):
        _upsert_user(lib.db, detail)
        lib.db.set_hub_resource_id(filename, detail["resource_id"])
        lib.db.apply_hub_detail_to_package(filename, detail)
        lib.db.mark_hub_name_checked(filename)
        return True
    lib.db.mark_hub_name_checked(filename)
    return False


def _run_parallel(items, fn, on_done=None) -> list:
    if not items:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for i, r in enumerate(pool.map(fn, items)):
            results.append(r)
            if on_done:
                on_done(i + 1)
    return results


def _stub_resource_ids(db) -> tuple[set[str], set[str]]:
    gone, other = set(), set()
    for row in db.get_all_hub_resource_jsons():
        try:
            d = json.loads(row["hub_json"]) if row["hub_json"] else None
        except ValueError:
            continue
        if d and d.get("_unavailable"):
            rid = str(row["resource_id"])
            if "resource not found" in str(d.get("_error") or "").lower():
                gone.add(rid)
            else:
                other.add(rid)
    return gone, other


def not_found_resource_ids(db) -> set[str]:
    return _stub_resource_ids(db)[0]


def unlink_mismatched(lib) -> int:
    unlinked = 0
    for row in lib.db.get_packages_needing_hub_detail_apply(all_linked=True):
        try:
            detail = json.loads(row["hub_json"]) if row["hub_json"] else None
        except ValueError:
            continue
        if not detail or detail.get("_unavailable") or not row.get("package_name"):
            continue
        if not resource_detail_matches_package_name(detail, row["package_name"]):
            log.info(
                "%s: unlinking Hub resource %s (%r), which does not carry it",
                row["filename"],
                detail.get("resource_id"),
                detail.get("title"),
            )
            lib.db.unlink_hub_resource(row["filename"])
            unlinked += 1
    return unlinked


def retry_pending_hub_details(lib, hub: HubClient) -> dict:
    for rid in _stub_resource_ids(lib.db)[1]:
        lib.db.clear_hub_resource_detail(rid)
    unlinked = unlink_mismatched(lib)
    if unlinked:
        with lib.lock:
            lib.store.build_from_db(skip_graph=True)
        lib.notify("packages:updated")
    to_fetch = lib.db.get_packages_needing_hub_detail_fetch()
    counts: Counter = Counter()
    if to_fetch:
        counts.update(_run_parallel(to_fetch, lambda r: fetch_one_detail(lib, hub, str(r["rid"]), r["filename"])))
        log.info(
            "Hub metadata retry: %d pending, %d fetched, %d no longer on the Hub, %d failed",
            len(to_fetch),
            counts["ok"],
            counts["not_found"],
            counts["failed"],
        )
    if counts["ok"]:
        with lib.lock:
            lib.store.build_from_db(skip_graph=True)
        lib.notify("packages:updated")
        lib.notify("avatars:updated")
    return {
        "pending": len(to_fetch),
        "fetched": counts["ok"],
        "not_found": counts["not_found"],
        "failed": counts["failed"],
    }


def scan_hub_details(lib, hub: HubClient, pj: PackagesJsonIndex, on_progress: ProgressFn | None = None) -> dict:
    progress = on_progress or (lambda _d: None)
    try:
        pj.fetch()
    except Exception as err:
        log.warning("packages.json fetch failed: %s", err)
    index = pj.index
    unlink_mismatched(lib)
    rows = [r for r in lib.db.get_all_packages() if r["package_name"]]
    not_found = not_found_resource_ids(lib.db)
    total = len(rows)
    found = 0
    pass1_changes = 0
    for i, row in enumerate(rows):
        if (i + 1) % _LOOKUP_PROGRESS_STRIDE == 0 or i + 1 == total:
            progress({"phase": "lookup", "current": i + 1, "total": total, "found": found})
        if not index:
            continue
        entry = index.get(row["package_name"])
        if not entry:
            continue
        found += 1
        rid = str(entry["resource_id"])
        if rid in not_found:
            continue
        pass1_changes += lib.db.set_hub_resource_id(row["filename"], rid)

    dirty = lib.db.get_packages_needing_hub_detail_apply()
    for row in dirty:
        try:
            lib.db.apply_hub_detail_to_package(row["filename"], json.loads(row["hub_json"]))
        except ValueError:
            pass
    to_fetch = lib.db.get_packages_needing_hub_detail_fetch()
    name_lookups = lib.db.get_packages_needing_hub_name_lookup() if index else []
    net_total = len(to_fetch) + len(name_lookups)

    if pass1_changes or dirty:
        with lib.lock:
            lib.store.build_from_db(skip_graph=True)
        lib.notify("packages:updated")

    statuses = Counter(
        _run_parallel(
            to_fetch,
            lambda r: fetch_one_detail(lib, hub, str(r["rid"]), r["filename"]),
            on_done=lambda n: progress({"phase": "fetching", "current": n, "total": net_total, "found": found}),
        )
    )
    hits = _run_parallel(
        name_lookups,
        lambda r: resolve_one_by_name(lib, hub, r["filename"], r["package_name"]),
        on_done=lambda n: progress(
            {"phase": "fetching", "current": len(to_fetch) + n, "total": net_total, "found": found}
        ),
    )
    name_hits = sum(1 for h in hits if h)

    progress({"phase": "hub-finalize", "current": 0, "total": 1, "found": found})
    if to_fetch or name_hits:
        with lib.lock:
            lib.store.build_from_db(skip_graph=True)
        lib.notify("packages:updated")
        lib.notify("avatars:updated")
    progress({"phase": "hub-finalize", "current": 1, "total": 1, "found": found})
    log.info(
        "Hub metadata: %d of %d packages known to the Hub; %d details fetched, %d no longer on the Hub, "
        "%d failed (retried on the next scan); %d of %d looked up by name matched",
        found,
        total,
        statuses["ok"],
        statuses["not_found"],
        statuses["failed"],
        name_hits,
        len(name_lookups),
    )
    return {
        "total": total,
        "found": found,
        "enriched": found - len(to_fetch),
        "queued": len(to_fetch),
        "skipped": total - found,
        "name_checked": len(name_lookups),
        "name_hits": name_hits,
        "not_found": statuses["not_found"],
        "failed": statuses["failed"],
    }


def enrich_new_packages(lib, hub: HubClient, pj: PackagesJsonIndex, filenames: list[str]) -> bool:
    index = pj.index
    if not index or not filenames:
        return False
    want = set(filenames)
    eligible = {r["filename"] for r in lib.db.get_packages_needing_hub_name_lookup() if r["filename"] in want}
    not_found = not_found_resource_ids(lib.db)
    to_fetch = []
    by_name = []
    changed = False
    for filename in filenames:
        parts = (filename[:-4] if filename.lower().endswith(".var") else filename).split(".")
        if len(parts) < 3:
            continue
        package_name = ".".join(parts[:-1])
        entry = index.get(package_name)
        if not entry:
            if filename in eligible:
                by_name.append((filename, package_name))
            continue
        rid = str(entry["resource_id"])
        if rid in not_found:
            if filename in eligible:
                by_name.append((filename, package_name))
            continue
        if lib.db.set_hub_resource_id(filename, rid) > 0:
            changed = True
        cached = lib.db.get_hub_resource(rid)
        if cached and cached["hub_json"]:
            try:
                lib.db.apply_hub_detail_to_package(filename, json.loads(cached["hub_json"]))
                changed = True
            except ValueError:
                pass
            continue
        to_fetch.append((rid, filename))
    if to_fetch:
        statuses = Counter(_run_parallel(to_fetch, lambda t: fetch_one_detail(lib, hub, t[0], t[1])))
        if statuses["failed"]:
            log.info(
                "Hub metadata for new packages: %d fetched, %d failed (retried on the next scan)",
                statuses["ok"],
                statuses["failed"],
            )
        changed = True
    if by_name:
        hits = _run_parallel(by_name, lambda t: resolve_one_by_name(lib, hub, t[0], t[1]))
        if any(hits):
            changed = True
    if changed:
        with lib.lock:
            lib.store.build_from_db(skip_graph=True)
        lib.notify("packages:updated")
    return changed
