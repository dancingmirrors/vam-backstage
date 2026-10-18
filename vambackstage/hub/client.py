from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from ..core.db import Database
from ..core.events import EventBus
from ..core.licenses import (
    COMMERCIAL_USE_ALLOWED_LICENSE_FILTER,
    NONCOMMERCIAL_USE_ALLOWED_LICENSE_FILTER,
    canonicalize_license,
    hub_resource_license,
    is_commercial_use_allowed,
    is_noncommercial_use_allowed,
)
from .http import HttpTransport, TransportError, UrllibTransport

log = logging.getLogger(__name__)

API_URL = "https://hub.virtamate.com/citizenx/api.php"
HUB_PER_PAGE = 60
FILTERS_SETTINGS_KEY = "hub_filters_json"
_MAX_SEARCHES = 500
_MAX_DETAILS = 1000

HUB_RESOURCE_ICON_CDN = "https://1424104733.rsc.cdn77.org/data/resource_icons"


def hub_resource_icon_url(resource_id) -> str | None:
    try:
        n = int(str(resource_id))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"{HUB_RESOURCE_ICON_CDN}/{n // 1000}/{n}.jpg"


HUB_HOST = "hub.virtamate.com"
_HUB_DOWNLOAD_PATH = re.compile(r"^/resources/(?:[^/]*\.)?([0-9]+)(?:/version/([0-9]+))?/download/?$", re.ASCII)


def hub_resource_page_url(resource_id) -> str:
    return f"https://{HUB_HOST}/resources/{resource_id}"


def _id_text(value) -> str | None:
    s = str(value if value is not None else "")
    return str(int(s)) if s.isascii() and s.isdigit() else None


def is_hub_url(url) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and (parts.hostname or "").lower() == HUB_HOST
        and parts.username is None
        and port in (None, 80, 443)
    )


def _query_file_id(url) -> str | None:
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query).get("file")
    except ValueError:
        return None
    return _id_text(values[-1]) if values else None


def parse_hub_download_url(url) -> tuple[str, str | None, str | None] | None:
    if not is_hub_url(url):
        return None
    parts = urllib.parse.urlsplit(str(url))
    m = _HUB_DOWNLOAD_PATH.match(parts.path)
    if not m:
        return None
    return _id_text(m.group(1)) or m.group(1), _query_file_id(url), _id_text(m.group(2))


def pick_hub_file(detail: dict | None, file_id: str | None, version_id: str | None = None) -> dict | None:
    detail = detail if isinstance(detail, dict) else {}
    files = [
        f for f in detail.get("hubFiles") or [] if isinstance(f, dict) and f.get("filename") not in (None, "", "null")
    ]
    if file_id:
        wanted = _id_text(file_id)
        files = [
            f
            for f in files
            if wanted is not None
            and wanted
            in {_id_text(f.get("attachment_id")), *(_query_file_id(f.get(k)) for k in ("urlHosted", "downloadUrl"))}
        ]
    if version_id:
        wanted = _id_text(version_id)
        files = [
            f
            for f in files
            if wanted is not None
            and _id_text(f.get("current_version_id") or detail.get("current_version_id")) == wanted
        ]
    return files[0] if len(files) == 1 else None


class HubError(Exception):
    def __init__(self, message: str, api_error: bool = False):
        super().__init__(message)
        self.api_error = api_error

    @property
    def not_found(self) -> bool:
        return "resource not found" in str(self).lower()


class _Lru:
    def __init__(self, max_size: int):
        self.max = max_size
        self.d: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        if key in self.d:
            self.d.move_to_end(key)
            return self.d[key]
        return default

    def __contains__(self, key):
        return key in self.d

    def set(self, key, value) -> None:
        self.d[key] = value
        self.d.move_to_end(key)
        if len(self.d) > self.max:
            for _ in range(max(1, self.max // 5)):
                self.d.popitem(last=False)

    def clear(self) -> None:
        self.d.clear()


def _is_version_segment(seg: str) -> bool:
    return seg == "latest" or seg.isdigit() or re.fullmatch(r"min\d+", seg, re.IGNORECASE) is not None


def hub_package_base_name(name) -> str:
    if not name:
        return ""
    s = re.sub(r"\.var$", "", str(name), flags=re.IGNORECASE)
    parts = s.split(".")
    if len(parts) >= 3 and _is_version_segment(parts[-1]):
        return ".".join(parts[:-1])
    return s


def _normalize_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def title_fits_package(title: str | None, package_name: str) -> bool:
    base = hub_package_base_name(package_name)
    if not title or not base:
        return False
    t = _normalize_name(title)
    pkg = _normalize_name(base.split(".", 1)[1] if "." in base else base)
    return bool(t) and bool(pkg) and t in (pkg, _normalize_name(base))


def resource_detail_matches_package_name(detail: dict | None, package_name: str) -> bool:
    if not detail or not detail.get("resource_id") or not package_name:
        return False
    base = hub_package_base_name(package_name).lower()
    if not base:
        return False
    files = [
        hub_package_base_name(f["filename"]).lower()
        for f in detail.get("hubFiles") or []
        if isinstance(f, dict) and f.get("filename") and f.get("filename") != "null"
    ]
    if base in files:
        return True
    for key in detail.get("dependencies") or {}:
        if not key or hub_package_base_name(key).lower() != base:
            continue
        if title_fits_package(detail.get("title"), base):
            return True
        if not files and key == hub_package_base_name(key):
            return True
    return False


def is_valid_filters(data) -> bool:
    return (
        bool(data)
        and isinstance(data.get("type"), list)
        and bool(data["type"])
        and isinstance(data.get("sort"), list)
        and bool(data["sort"])
    )


class HubClient:
    def __init__(
        self,
        db: Database,
        events: EventBus | None = None,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
    ):
        self.db = db
        self.events = events or EventBus()
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.retry_delay = 1.0
        self._lock = threading.Lock()
        self._filters: dict | None = None
        self._searches = _Lru(_MAX_SEARCHES)
        self._details = _Lru(_MAX_DETAILS)

    def _post(self, body: dict, throw_on_api_error: bool = True) -> dict | None:
        payload = {"source": "VaM", **body}
        if self.db.get_setting("hub_debug_requests") == "1":
            log.info("[Hub] -> %s", json.dumps(payload))
        for attempt in (1, 2):
            try:
                res = self.transport.request(
                    "POST",
                    API_URL,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    data=json.dumps(payload).encode("utf-8"),
                    timeout=self.timeout,
                )
                if not res.ok:
                    raise HubError(f"Hub API {res.status}")
                res.read()
                break
            except TransportError as err:
                if attempt == 1 and err.transient:
                    log.info("Hub request %s failed (%s), retrying once", body.get("action"), err)
                    time.sleep(self.retry_delay)
                    continue
                raise HubError(f"Hub unreachable: {err}") from err
        try:
            data = res.json()
        except ValueError as err:
            raise HubError(f"Hub returned invalid JSON: {err}") from err
        if isinstance(data, dict) and data.get("status") == "error":
            if throw_on_api_error:
                raise HubError(data.get("error") or "Hub API error", api_error=True)
            return None
        return data

    def get_filters(self) -> dict:
        with self._lock:
            if self._filters:
                return self._filters
        persisted = self.db.get_setting(FILTERS_SETTINGS_KEY)
        if persisted:
            try:
                parsed = json.loads(persisted)
                if is_valid_filters(parsed):
                    with self._lock:
                        self._filters = parsed
                    return parsed
            except ValueError:
                pass
        return self.refresh_filters()

    def refresh_filters(self) -> dict:
        data = self._post({"action": "getInfo"})
        if not is_valid_filters(data):
            raise HubError("Hub getInfo returned an unexpected shape")
        with self._lock:
            self._filters = data
        self.db.set_setting(FILTERS_SETTINGS_KEY, json.dumps(data))
        return data  # type: ignore[return-value]

    def search_resources(self, params: dict | None = None) -> dict:
        """`{resources, total_found, total_pages}`. Params: sort, search, type,
        category, username, tags, license, page, perpage, persist."""
        params = dict(params or {})
        persist = params.pop("persist", True) is not False
        key = json.dumps(params, sort_keys=True)
        if persist:
            with self._lock:
                cached = self._searches.get(key)
            if cached is not None:
                return cached
        body: dict[str, Any] = {
            "action": "getResources",
            "latest_image": "Y",
            "perpage": str(params.get("perpage") or HUB_PER_PAGE),
            "page": str(params.get("page") or 1),
        }
        if params.get("sort"):
            body["sort"] = params["sort"]
        if params.get("search"):
            body["search"] = params["search"]
            body["searchall"] = "true"
        for k in ("type", "category"):
            if params.get(k) and params[k] != "All":
                body[k] = params[k]
        for k in ("username", "tags"):
            if params.get(k):
                body[k] = params[k]
        lic = params.get("license")
        if lic and lic not in ("Any", COMMERCIAL_USE_ALLOWED_LICENSE_FILTER, NONCOMMERCIAL_USE_ALLOWED_LICENSE_FILTER):
            body["license"] = lic
        data = self._post(body) or {}
        resources = list(data.get("resources") or [])
        if lic and lic != "Any":
            if lic == COMMERCIAL_USE_ALLOWED_LICENSE_FILTER:
                resources = [r for r in resources if is_commercial_use_allowed(hub_resource_license(r)) is True]
            elif lic == NONCOMMERCIAL_USE_ALLOWED_LICENSE_FILTER:
                resources = [r for r in resources if is_noncommercial_use_allowed(hub_resource_license(r)) is True]
            else:
                want = canonicalize_license(lic)
                resources = [r for r in resources if canonicalize_license(hub_resource_license(r)) == want]
        if persist:
            try:
                with self.db.transaction():
                    for r in resources:
                        if r.get("resource_id"):
                            self.db.upsert_hub_resource_search(r["resource_id"], r)
            except Exception:
                log.exception("persisting search results failed")
        pagination = data.get("pagination") or {}
        result = {
            "resources": resources,
            "total_found": _to_int(pagination.get("total_found")),
            "total_pages": _to_int(pagination.get("total_pages")),
        }
        if persist:
            with self._lock:
                self._searches.set(key, result)
        return result

    def get_resource_detail(self, resource_id) -> dict:
        key = str(resource_id)
        with self._lock:
            cached = self._details.get(key)
        if cached is not None:
            return cached
        data = self._post({"action": "getResourceDetail", "latest_image": "Y", "resource_id": key})
        if not isinstance(data, dict):
            raise HubError("Hub returned an unexpected detail shape")
        try:
            self.db.upsert_hub_resource_detail(key, data)
        except Exception:
            log.exception("persisting detail failed")
        with self._lock:
            self._details.set(key, data)
        return data

    def get_resource_detail_by_name(self, package_name: str) -> dict | None:
        key = "pkg:" + package_name
        with self._lock:
            if key in self._details:
                return self._details.get(key)
        data = self._post(
            {"action": "getResourceDetail", "latest_image": "Y", "package_name": package_name + ".latest"},
            throw_on_api_error=False,
        )
        if isinstance(data, dict) and data.get("resource_id"):
            rid = str(data["resource_id"])
            try:
                self.db.upsert_hub_resource_detail(rid, data)
            except Exception:
                log.exception("persisting detail failed")
            with self._lock:
                self._details.set(rid, data)
            if not resource_detail_matches_package_name(data, package_name):
                data = None
        else:
            data = None
        with self._lock:
            self._details.set(key, data)
        return data

    def get_cached_detail(self, resource_id) -> dict | None:
        with self._lock:
            return self._details.get(str(resource_id))

    def find_packages(self, package_names: Iterable[str]) -> dict[str, dict]:
        names = [n for n in package_names if n]
        results: dict[str, dict] = {}
        for i in range(0, len(names), 50):
            batch = names[i : i + 50]
            data = self._post({"action": "findPackages", "packages": ",".join(batch)}) or {}
            pkgs = data.get("packages") or {}
            results.update(pkgs)
            try:
                with self.db.transaction():
                    for f in pkgs.values():
                        if isinstance(f, dict) and f.get("resource_id") and f["resource_id"] != "null":
                            self.db.upsert_hub_resource_find(f["resource_id"], f)
            except Exception:
                log.exception("persisting find results failed")
        return results

    def invalidate_caches(self) -> None:
        with self._lock:
            self._filters = None
            self._searches.clear()
            self._details.clear()
        self.db.set_setting(FILTERS_SETTINGS_KEY, None)


def is_null_find_entry(entry: dict | None) -> bool:
    return not entry or entry.get("filename") in (None, "null") or entry.get("downloadUrl") in (None, "null", "")


def _to_int(v) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0
