from __future__ import annotations

import os
import struct
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .paths import LOCAL_CONTENT_DIRS, LOCAL_PACKAGE_FILENAME, is_local_package
from .varfile import walk_unpacked

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"})
GDI_FORMATS = frozenset({"JPEG", "PNG", "GIF", "BMP", "TIFF"})
MAX_TEXTURE_SIDE = 16384

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_SOF_KINDS = {
    0xC3: "lossless",
    0xC5: "hierarchical",
    0xC6: "hierarchical",
    0xC7: "hierarchical",
    0xC9: "arithmetic-coded",
    0xCA: "arithmetic-coded progressive",
    0xCB: "arithmetic-coded lossless",
    0xCD: "hierarchical arithmetic-coded",
    0xCE: "hierarchical arithmetic-coded",
    0xCF: "hierarchical arithmetic-coded",
}


@dataclass(frozen=True)
class ImageProblem:
    path: str
    severity: str
    message: str
    detected: str | None = None


def detect_format(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if head[:2] == b"BM":
        return "BMP"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "TIFF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WebP"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "AVIF"
        if brand in _HEIF_BRANDS:
            return "HEIF/HEIC"
        return "ISO media (video)"
    if head[:4] == b"8BPS":
        return "Photoshop PSD"
    if head[:4] == b"DDS ":
        return "DDS"
    if head[:2] == b"\xff\x0a" or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "JPEG XL"
    if head[:4] == b"\x76\x2f\x31\x01":
        return "OpenEXR"
    if head.startswith(b"#?RADIANCE") or head.startswith(b"#?RGBE"):
        return "Radiance HDR"
    if head[:4] == b"\xabKTX":
        return "KTX"
    if head[:4] == b"qoif":
        return "QOI"
    if head[:4] == b"\x00\x00\x01\x00":
        return "ICO"
    if head[:4] == b"PK\x03\x04":
        return "ZIP archive"
    if head.startswith(b"\xef\xbb\xbf") or head.lstrip()[:1] in (b"{", b"[", b"<"):
        return "text"
    return None


def _jpeg_problems(path: str, data: bytes) -> list[ImageProblem]:
    out: list[ImageProblem] = []
    i, n = 2, len(data)
    sof = None
    while i + 4 <= n:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xD9, 0xDA):
            break
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and i + 10 <= n:
            sof = marker
            precision, height, width, components = struct.unpack(">BHHB", data[i + 4 : i + 10])
            break
        i += 2 + length
    if sof is None:
        out.append(ImageProblem(path, "error", "damaged JPEG: no frame header before the image data", "JPEG"))
    else:
        kind = _SOF_KINDS.get(sof)
        if kind:
            out.append(
                ImageProblem(
                    path,
                    "error",
                    f"{kind} JPEG; GDI+ decodes only baseline and progressive (Huffman) JPEGs, re-save it as one",
                    "JPEG",
                )
            )
        if precision != 8:
            out.append(ImageProblem(path, "error", f"{precision}-bit JPEG; GDI+ decodes only 8-bit JPEGs", "JPEG"))
        if components == 4:
            out.append(
                ImageProblem(
                    path, "warning", "CMYK (4-channel) JPEG; VaM expects RGB, colors may come out wrong", "JPEG"
                )
            )
        if width == 0 or height == 0:
            out.append(ImageProblem(path, "error", "JPEG with a zero size in its header", "JPEG"))
        elif max(width, height) > MAX_TEXTURE_SIDE:
            out.append(
                ImageProblem(
                    path, "warning", f"{width}×{height} px is over Unity's {MAX_TEXTURE_SIDE} px texture limit", "JPEG"
                )
            )
    if b"\xff\xd9" not in data[-4096:]:
        out.append(
            ImageProblem(
                path,
                "error",
                "truncated JPEG: the end-of-image marker is missing; decoders fill what is missing with flat grey "
                "(or refuse the file), so the texture looks featureless or cut off",
                "JPEG",
            )
        )
    return out


def _png_problems(path: str, data: bytes) -> list[ImageProblem]:
    out: list[ImageProblem] = []
    if len(data) < 33 or data[12:16] != b"IHDR":
        out.append(ImageProblem(path, "error", "damaged PNG: no IHDR header", "PNG"))
        return out
    width, height = struct.unpack(">II", data[16:24])
    if width == 0 or height == 0:
        out.append(ImageProblem(path, "error", "PNG with a zero size in its header", "PNG"))
    elif max(width, height) > MAX_TEXTURE_SIDE:
        out.append(
            ImageProblem(
                path, "warning", f"{width}×{height} px is over Unity's {MAX_TEXTURE_SIDE} px texture limit", "PNG"
            )
        )
    if b"IEND" not in data[-64:]:
        out.append(ImageProblem(path, "error", "truncated PNG: the IEND chunk is missing", "PNG"))
    return out


_TIFF_WIDTH, _TIFF_HEIGHT = 256, 257
_TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 6: 1, 7: 1, 8: 2, 9: 4}


def _tiff_tags(data: bytes) -> dict[int, int] | None:
    if len(data) < 8:
        return None
    endian = "<" if data[:2] == b"II" else ">"
    magic, offset = struct.unpack(endian + "HI", data[2:8])
    if magic != 42 or offset + 2 > len(data):
        return None
    (count,) = struct.unpack(endian + "H", data[offset : offset + 2])
    tags: dict[int, int] = {}
    for k in range(count):
        e = offset + 2 + 12 * k
        if e + 12 > len(data):
            break
        tag, typ, n = struct.unpack(endian + "HHI", data[e : e + 8])
        size = _TIFF_TYPE_SIZE.get(typ)
        if size is None or n == 0 or typ not in (3, 4):
            continue
        if size * n <= 4:
            raw = data[e + 8 : e + 8 + size]
        else:
            (off,) = struct.unpack(endian + "I", data[e + 8 : e + 12])
            raw = data[off : off + size]
        if len(raw) == size:
            tags[tag] = struct.unpack(endian + ("H" if typ == 3 else "I"), raw)[0]
    return tags


def _tiff_problems(path: str, data: bytes) -> list[ImageProblem]:
    out: list[ImageProblem] = []

    def error(msg: str) -> None:
        out.append(ImageProblem(path, "error", msg, "TIFF"))

    def warning(msg: str) -> None:
        out.append(ImageProblem(path, "warning", msg, "TIFF"))

    if len(data) >= 4 and struct.unpack(("<" if data[:2] == b"II" else ">") + "H", data[2:4])[0] == 43:
        error("BigTIFF (64-bit offsets), not the classic TIFF format; re-save it as a classic TIFF, JPEG or PNG")
        return out
    tags = _tiff_tags(data)
    if tags is None:
        error("damaged or truncated TIFF: its image directory cannot be read")
        return out
    width, height = tags.get(_TIFF_WIDTH), tags.get(_TIFF_HEIGHT)
    if width is not None and height is not None:
        if width == 0 or height == 0:
            error("TIFF with a zero size in its header")
        elif max(width, height) > MAX_TEXTURE_SIDE:
            warning(f"{width}×{height} px is over Unity's {MAX_TEXTURE_SIDE} px texture limit")
    return out


def check_image(path: str, data: bytes) -> list[ImageProblem]:
    ext = os.path.splitext(path)[1].lower()
    if not data:
        return [ImageProblem(path, "error", "empty file (0 bytes)")]
    fmt = detect_format(data[:64])
    if fmt is None:
        shown = data[:8].hex(" ")
        return [ImageProblem(path, "error", f"not an image format GDI+ knows (data starts with {shown})")]
    if fmt not in GDI_FORMATS:
        return [
            ImageProblem(
                path,
                "error",
                f"{fmt} data in a {ext} file; VaM's decoder (GDI+) cannot read {fmt}, re-save it as JPEG or PNG",
                fmt,
            )
        ]
    out: list[ImageProblem] = []
    if fmt == "JPEG":
        out += _jpeg_problems(path, data)
    elif fmt == "PNG":
        out += _png_problems(path, data)
    elif fmt == "TIFF":
        out += _tiff_problems(path, data)
    elif fmt == "GIF" and data[-1:] != b";":
        out.append(ImageProblem(path, "warning", "GIF without its trailer byte: possibly truncated", "GIF"))
    elif fmt == "BMP" and len(data) >= 6 and struct.unpack("<I", data[2:6])[0] > len(data):
        out.append(ImageProblem(path, "error", "truncated BMP: shorter than its header says", "BMP"))
    return out


@dataclass
class TextureReport:
    images: int = 0
    bytes: int = 0
    problems: list[ImageProblem] = field(default_factory=list)


def check_package_images(path: str | os.PathLike) -> TextureReport:
    path = os.fspath(path)
    report = TextureReport()
    if os.path.isdir(path):
        for rel, full, _size, _mtime in walk_unpacked(path):
            if os.path.splitext(rel)[1].lower() not in IMAGE_SUFFIXES:
                continue
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError as err:
                report.problems.append(ImageProblem(rel, "error", f"unreadable: {err}"))
                continue
            report.images += 1
            report.bytes += len(data)
            report.problems += check_image(rel, data)
        return report
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.filename.endswith("/") or os.path.splitext(info.filename)[1].lower() not in IMAGE_SUFFIXES:
                continue
            try:
                data = zf.read(info)
            except Exception as err:
                report.problems.append(ImageProblem(info.filename, "error", f"unreadable entry: {err}"))
                continue
            report.images += 1
            report.bytes += len(data)
            report.problems += check_image(info.filename, data)
    return report


ARCHIVE_SUFFIXES = frozenset({".vac", ".zip"})
LOCAL_LABEL = "Loose files (Saves, Custom)"


def check_loose_images(vam_dir: str | os.PathLike) -> TextureReport:
    vam_dir = os.fspath(vam_dir)
    report = TextureReport()
    for sub in LOCAL_CONTENT_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(vam_dir, sub)):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, vam_dir).replace(os.sep, "/")
                ext = os.path.splitext(name)[1].lower()
                if ext in IMAGE_SUFFIXES:
                    try:
                        with open(full, "rb") as f:
                            data = f.read()
                    except OSError as err:
                        report.problems.append(ImageProblem(rel, "error", f"unreadable: {err}"))
                        continue
                    report.images += 1
                    report.bytes += len(data)
                    report.problems += check_image(rel, data)
                elif ext in ARCHIVE_SUFFIXES:
                    try:
                        inner = check_package_images(full)
                    except Exception as err:
                        report.problems.append(ImageProblem(rel, "error", f"unreadable archive: {err}"))
                        continue
                    report.images += inner.images
                    report.bytes += inner.bytes
                    report.problems += [
                        ImageProblem(f"{rel} › {p.path}", p.severity, p.message, p.detected) for p in inner.problems
                    ]
    return report


_WORKERS = max(1, min(4, os.cpu_count() or 1))


def run_texture_check(
    lib,
    on_progress: Callable[[dict], None] | None = None,
    cancel: threading.Event | None = None,
    filenames: Iterable[str] | None = None,
    workers: int | None = None,
) -> dict:
    progress = on_progress or (lambda _p: None)
    wanted = set(filenames) if filenames is not None else None
    targets = []
    for pkg in lib.store.package_index.values():
        fn = pkg["filename"]
        if is_local_package(fn) or pkg.get("is_dummy") or (wanted is not None and fn not in wanted):
            continue
        p = lib.dirs.resolve_content_path(pkg)
        if p and os.path.exists(p):
            targets.append((fn, p))
    targets.sort()
    vam_dir = lib.vam_dir
    if vam_dir and (wanted is None or LOCAL_PACKAGE_FILENAME in wanted):
        targets.append((LOCAL_PACKAGE_FILENAME, vam_dir))
    started = time.monotonic()
    checked: list[str] = []
    problems: list[dict] = []
    errors: dict[str, str] = {}
    images = bytes_done = 0
    cancelled = False

    def check(item):
        fn, p = item
        if cancel is not None and cancel.is_set():
            return fn, None, None
        try:
            return fn, (check_loose_images(p) if is_local_package(fn) else check_package_images(p)), None
        except Exception as err:
            return fn, None, f"{type(err).__name__}: {err}"

    with ThreadPoolExecutor(max_workers=workers or _WORKERS) as pool:
        for fn, report, error in pool.map(check, targets):
            if report is None and error is None:
                cancelled = True
                continue
            checked.append(fn)
            if error:
                errors[fn] = error
            else:
                images += report.images
                bytes_done += report.bytes
                problems += [
                    {
                        "filename": fn,
                        "path": p.path,
                        "severity": p.severity,
                        "message": p.message,
                        "detected": p.detected,
                    }
                    for p in report.problems
                ]
            progress(
                {
                    "step": len(checked),
                    "total": len(targets),
                    "filename": fn,
                    "status": "checking",
                    "bytes": bytes_done,
                    "problems": len(problems),
                }
            )
    progress(
        {
            "step": len(checked),
            "total": len(targets),
            "filename": "",
            "status": "cancelled" if cancelled else "done",
            "bytes": bytes_done,
            "problems": len(problems),
        }
    )
    affected = sorted({p["filename"] for p in problems})
    return {
        "total": len(targets),
        "checked": len(checked),
        "checked_files": checked,
        "images": images,
        "bytes": bytes_done,
        "problems": problems,
        "errors": errors,
        "packages_with_problems": affected,
        "error_count": sum(1 for p in problems if p["severity"] == "error"),
        "warning_count": sum(1 for p in problems if p["severity"] == "warning"),
        "seconds": time.monotonic() - started,
        "cancelled": cancelled,
    }
