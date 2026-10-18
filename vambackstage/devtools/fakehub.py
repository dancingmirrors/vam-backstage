from __future__ import annotations

import io
import json
import math
import re
import struct
import zipfile
import zlib
from collections.abc import Callable

from ..hub.http import HttpResponse, TransportError

Handler = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.routes: list[tuple[Callable[[str, str], bool], Handler]] = []

    def route(self, pattern: str, handler: Handler, method: str | None = None) -> None:
        rx = re.compile(pattern)
        self.routes.append((lambda m, u: (method is None or m == method) and rx.search(u) is not None, handler))

    def request(self, method, url, *, headers=None, data=None, timeout=30):
        headers = headers or {}
        self.calls.append((method, url, headers, data))
        for pred, handler in self.routes:
            if pred(method, url):
                status, hdrs, body = handler(method, url, headers, data)
                return HttpResponse(status, {k.lower(): v for k, v in hdrs.items()}, url, None, body)
        raise TransportError(f"no fake route for {method} {url}", transient=False)

    def count(self, pattern: str) -> int:
        rx = re.compile(pattern)
        return sum(1 for _, u, _, _ in self.calls if rx.search(u))


class FakeHub:

    def __init__(self, transport: FakeTransport):
        self.catalog_source = None
        self.details: dict[str, dict] = {}
        self.by_name: dict[str, dict] = {}
        self.find: dict[str, dict] = {}
        self.search_pages: dict[int, dict] = {}
        self.info = {
            "status": "success",
            "type": ["Scenes", "Looks"],
            "sort": ["Latest Update", "Rating"],
            "category": ["Free", "Paid"],
            "tags": {"hair": {"ct": 5}},
            "users": {"A": {"ct": "1"}},
            "last_update": "1",
        }
        self.requests: list[dict] = []
        transport.route(r"hub\.virtamate\.com/citizenx/api\.php", self._handle, method="POST")

    def _handle(self, method, url, headers, data):
        body = json.loads(data.decode("utf-8"))
        self.requests.append(body)
        action = body.get("action")
        if action == "getInfo":
            return 200, {}, json.dumps(self.info).encode()
        if action == "getResources":
            page = int(body.get("page") or 1)
            res = self.search_pages.get(page) or {
                "status": "success",
                "pagination": {"total_found": 0, "total_pages": 0},
                "resources": [],
            }
            return 200, {}, json.dumps(res).encode()
        if action == "getResourceDetail":
            if body.get("resource_id"):
                d = self.details.get(str(body["resource_id"]))
                if d is None:
                    return (
                        200,
                        {},
                        json.dumps(
                            {"status": "error", "error": "Resource not found.", "resource_id": body["resource_id"]}
                        ).encode(),
                    )
                return 200, {}, json.dumps(d).encode()
            name = re.sub(r"\.latest$", "", body.get("package_name") or "")
            d = self.by_name.get(name)
            if d is None:
                return 200, {}, json.dumps({"status": "error", "error": "Resource not found."}).encode()
            return 200, {}, json.dumps(d).encode()
        if action == "findPackages":
            out = {}
            for key in (body.get("packages") or "").split(","):
                out[key] = self.find.get(key) or {
                    "filename": "null",
                    "file_size": "null",
                    "licenseType": "null",
                    "package_id": "null",
                    "resource_id": "null",
                    "downloadUrl": "null",
                }
            return 200, {}, json.dumps({"packages": out}).encode()
        return 200, {}, json.dumps({"status": "error", "error": "Invalid action"}).encode()


def file_route(transport: FakeTransport, url_pattern: str, data: bytes, support_range: bool = True):

    def handler(method, url, headers, body):
        rng = headers.get("Range")
        if rng and support_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(data):
                return 416, {}, b""
            return 206, {"Content-Length": str(len(data) - start)}, data[start:]
        return 200, {"Content-Length": str(len(data))}, data

    transport.route(url_pattern, handler, method="GET")


def png_bytes(width: int, height: int, rgb: tuple[int, int, int], rgb2: tuple[int, int, int]) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            t = (x + y) / (width + height)
            px = [int(a + (b - a) * t) for a, b in zip(rgb, rgb2, strict=True)]
            if y < max(4, height // 12):
                px = [min(255, c + 110) for c in px]
            row += bytes(px)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def var_bytes(filename: str, deps: list[str] | None = None, files: dict[str, bytes | str] | None = None) -> bytes:
    creator, name, version = filename[:-4].split(".", 2)
    meta = {
        "licenseType": "CC BY",
        "creatorName": creator,
        "packageName": name,
        "description": "demo",
        "contentList": sorted(files or {}),
        "dependencies": {d: {"licenseType": "CC BY", "dependencies": {}} for d in deps or []},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, indent=2))
        for p, c in (files or {}).items():
            zf.writestr(p, c)
    return buf.getvalue()


_TYPES = ["Scenes", "Looks", "Clothing", "Hair", "Plugins", "Assets", "Morphs", "Poses"]
_SHAPES = [(320, 180), (240, 240), (180, 240), (320, 240), (400, 160)]
_WORDS = [
    "Velvet",
    "Harbor",
    "Nocturne",
    "Orchid",
    "Ember",
    "Lagoon",
    "Meridian",
    "Saffron",
    "Quartz",
    "Juniper",
    "Cobalt",
    "Willow",
]


def populate_demo_hub(transport: FakeTransport, fake: FakeHub, count: int = 48, per_page: int = 60) -> None:
    fake.info.update(
        {
            "type": _TYPES,
            "category": ["Free", "Paid", "Patreon"],
            "sort": ["Latest Update", "Rating", "Downloads"],
            "last_update": "1700000000",
        }
    )
    resources = []
    for i in range(count):
        rid = 1000 + i
        t = _TYPES[i % len(_TYPES)]
        creator = f"Creator{i % 7}"
        word = _WORDS[i % len(_WORDS)]
        pkg = f"{word}{t[:-1] if t.endswith('s') else t}{i}"
        version = 1 + i % 5
        filename = f"{creator}.{pkg}.{version}.var"
        deps = [f"Creator{(i + 1) % 7}.{_WORDS[(i + 3) % len(_WORDS)]}Lib{i % 4}.1"] if i % 3 == 0 else []
        data = var_bytes(
            filename,
            deps,
            {"Saves/scene/demo.json": "{}"} if t == "Scenes" else {"Custom/Atom/Person/Appearance/demo.vap": "{}"},
        )
        url = f"https://hub.virtamate.com/resources/{rid}/download?file={rid}"
        file_route(transport, re.escape(url) + "$", data)
        r = {
            "resource_id": str(rid),
            "package_id": str(50000 + i),
            "hub_hosted": "1",
            "title": f"{word} {t[:-1] if t.endswith('s') else t} {i + 1}",
            "tag_line": f"A demo {t.lower()} resource number {i + 1} served by the fake Hub.",
            "user_id": str(100 + i % 7),
            "username": creator,
            "avatar_date": "0",
            "type": t,
            "category": ["Free", "Paid", "Patreon"][i % 3],
            "download_count": str(37 * (i + 1)),
            "view_count": str(410 * (i + 1)),
            "rating_avg": f"{4 + (i % 10) / 10:.1f}",
            "rating_count": str(i % 23),
            "last_update": str(1700000000 + i * 86400),
            "version_string": f"1.{version}",
            "licenseType": "CC BY",
            "resource_date": str(1690000000 + i * 86400),
        }
        resources.append(r)
        fake.details[str(rid)] = {
            **r,
            "status": "success",
            "hubFiles": [
                {"filename": filename, "urlHosted": url, "file_size": str(len(data)), "resource_id": str(rid)}
            ],
            "dependencies": {
                f"{creator}.{pkg}": [
                    {
                        "packageName": d.rsplit(".", 1)[0],
                        "filename": d,
                        "version": d.rsplit(".", 1)[1],
                        "latest_version": d.rsplit(".", 1)[1],
                        "licenseType": "CC BY",
                        "resource_id": "null",
                        "downloadUrl": "null",
                        "file_size": "null",
                    }
                    for d in deps
                ]
            },
        }
        fake.by_name[f"{creator}.{pkg}"] = fake.details[str(rid)]
    pages = max(1, math.ceil(count / per_page))
    for p in range(pages):
        chunk = resources[p * per_page : (p + 1) * per_page]
        fake.search_pages[p + 1] = {
            "status": "success",
            "pagination": {"page": str(p + 1), "perpage": str(per_page), "total_found": count, "total_pages": pages},
            "resources": chunk,
        }

    def icon(method, url, headers, body):
        rid = int(re.search(r"/(\d+)\.jpg$", url).group(1))
        w, h = _SHAPES[rid % len(_SHAPES)]
        hue = (rid * 37) % 360
        return (
            200,
            {"Content-Type": "image/png"},
            png_bytes(w, h, _hsl(hue, 0.45, 0.35), _hsl((hue + 60) % 360, 0.5, 0.6)),
        )

    transport.route(r"resource_icons/\d+/\d+\.jpg$", icon, method="GET")
    transport.route(r"avatars/", lambda m, u, h, b: (404, {}, b""), method="GET")

    def catalog(method, url, headers, body):
        index = {
            f["filename"]: int(rid)
            for rid, d in fake.details.items()
            for f in d.get("hubFiles", [])
            if f.get("filename")
        }
        source = fake.catalog_source() if fake.catalog_source else {}
        n = 0
        for fn, pkg in sorted(source.items()):
            if not pkg.get("is_direct") or fn.startswith("__") or not fn.lower().endswith(".var"):
                continue
            n += 1
            if n % 3:
                continue
            try:
                creator, name, version = (
                    fn[:-4].rsplit(".", 2)[0].split(".", 1)[0],
                    fn[:-4].rsplit(".", 1)[0].split(".", 1)[1],
                    int(fn[:-4].rsplit(".", 1)[1]),
                )
            except ValueError:
                continue
            newer = f"{creator}.{name}.{version + 1}.var"
            rid = 9000 + n
            index[newer] = rid
            data = var_bytes(newer)
            url_dl = f"https://hub.virtamate.com/resources/{rid}/download?file={rid}"
            file_route(transport, re.escape(url_dl) + "$", data)
            fake.find[newer[:-4]] = {
                "filename": newer,
                "resource_id": str(rid),
                "downloadUrl": url_dl if n != 3 else "",
                "file_size": str(len(data)),
                "licenseType": "CC BY",
                "package_id": str(rid),
            }
        return 200, {"Content-Type": "application/json", "ETag": '"demo"'}, json.dumps(index).encode()

    transport.route(r"packages\.json", catalog, method="GET")


def _hsl(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hls_to_rgb(h / 360.0, lightness, s)
    return int(r * 255), int(g * 255), int(b * 255)
