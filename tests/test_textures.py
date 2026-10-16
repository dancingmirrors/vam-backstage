import struct
import zlib

import pytest

from vambackstage.core.textures import (
    check_image,
    check_package_images,
    detect_format,
    run_texture_check,
)

from .conftest import build_var
from .helpers import by_name, drop, make_lib


def jpeg(sof=0xC0, precision=8, width=64, height=48, components=3, eoi=True, prefix=b"") -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes([16] * 64)
    sof_body = (
        struct.pack(">BHHB", precision, height, width, components)
        + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"[: components * 3]
    )
    sof = bytes([0xFF, sof]) + struct.pack(">H", 2 + len(sof_body)) + sof_body
    dht = b"\xff\xc4" + struct.pack(">H", 20) + b"\x00" + bytes([1] + [0] * 15) + b"\x00"
    sos = (
        b"\xff\xda"
        + struct.pack(">H", 6 + 2 * components)
        + bytes([components])
        + b"\x01\x00\x02\x11\x03\x11"[: components * 2]
        + b"\x00\x3f\x00"
    )
    data = b"\xff\xd8" + prefix + app0 + dqt + sof + dht + sos + b"\x12\x34\x56\x78" * 8
    return data + (b"\xff\xd9" if eoi else b"")


def png(width=4, height=4, iend=True) -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw))
    return out + (chunk(b"IEND", b"") if iend else b"")


WEBP = b"RIFF" + struct.pack("<I", 100) + b"WEBPVP8 " + b"\x00" * 90


def tiff(
    compression=1,
    bits=8,
    samples=3,
    photometric=2,
    sample_format=1,
    width=64,
    height=48,
    big=False,
    little=True,
    planar=1,
    extra=None,
) -> bytes:
    e = "<" if little else ">"
    if big:
        return (b"II" if little else b"MM") + struct.pack(e + "H", 43) + b"\x00" * 12
    entries = [
        (256, 4, 1, width),
        (257, 4, 1, height),
        (258, 3, samples, None),
        (259, 3, 1, compression),
        (277, 3, 1, samples),
        (284, 3, 1, planar),
        (339, 3, 1, sample_format),
    ]
    if photometric is not None:
        entries.append((262, 3, 1, photometric))
    if extra is not None:
        entries.append((338, 3, 1, extra))
    entries.sort()
    ifd_offset = 8
    array_offset = ifd_offset + 2 + 12 * len(entries) + 4
    body = struct.pack(e + "H", len(entries))
    for tag, typ, count, value in entries:
        if value is None and count == 1:
            body += struct.pack(e + "HHIH", tag, typ, count, bits) + b"\x00\x00"
        elif value is None and count == 2:
            body += struct.pack(e + "HHIHH", tag, typ, count, bits, bits)
        elif value is None:
            body += struct.pack(e + "HHII", tag, typ, count, array_offset)
        elif typ == 3:
            body += struct.pack(e + "HHIH", tag, typ, count, value) + b"\x00\x00"
        else:
            body += struct.pack(e + "HHII", tag, typ, count, value)
    body += struct.pack(e + "I", 0)
    body += struct.pack(e + "H" * samples, *([bits] * samples))
    return (b"II" if little else b"MM") + struct.pack(e + "HI", 42, ifd_offset) + body


AVIF = struct.pack(">I", 28) + b"ftypavif" + b"\x00" * 20


class TestDetectFormat:
    @pytest.mark.parametrize(
        "head, fmt",
        [
            (jpeg()[:16], "JPEG"),
            (png()[:16], "PNG"),
            (b"GIF89a" + b"\x00" * 10, "GIF"),
            (b"BM" + b"\x00" * 10, "BMP"),
            (b"II*\x00" + b"\x00" * 10, "TIFF"),
            (b"MM\x00*" + b"\x00" * 10, "TIFF"),
            (WEBP[:16], "WebP"),
            (AVIF[:16], "AVIF"),
            (struct.pack(">I", 24) + b"ftypheic" + b"\x00" * 8, "HEIF/HEIC"),
            (struct.pack(">I", 24) + b"ftypisom" + b"\x00" * 8, "ISO media (video)"),
            (b"8BPS\x00\x01" + b"\x00" * 10, "Photoshop PSD"),
            (b"DDS |\x00\x00\x00" + b"\x00" * 8, "DDS"),
            (b"\xff\x0a" + b"\x00" * 10, "JPEG XL"),
            (b"\x00\x00\x00\x0cJXL \r\n\x87\n" + b"\x00" * 4, "JPEG XL"),
            (b"II+\x00" + b"\x00" * 12, "TIFF"),
            (b"\x76\x2f\x31\x01" + b"\x00" * 8, "OpenEXR"),
            (b"#?RADIANCE\n", "Radiance HDR"),
            (b"PK\x03\x04" + b"\x00" * 8, "ZIP archive"),
            (b'{"atoms": []}', "text"),
            (b"\xef\xbb\xbf<html>", "text"),
            (b"\x00\x01\x02\x03garbage", None),
            (b"", None),
        ],
    )
    def test_formats(self, head, fmt):
        assert detect_format(head) == fmt


class TestCheckImage:
    def test_good_images_pass(self):
        assert check_image("Custom/Atom/Person/Textures/a.jpg", jpeg()) == []
        assert check_image("Custom/Atom/Person/Textures/a.JPG", jpeg(sof=0xC2)) == []
        assert check_image("Saves/scene/thumb.png", png()) == []
        assert check_image("x.gif", b"GIF89a" + b"\x00" * 20 + b";") == []
        assert check_image("x.bmp", b"BM" + struct.pack("<I", 30) + b"\x00" * 24) == []
        assert check_image("x.tif", tiff()) == []

    def test_empty_and_unknown(self):
        (p,) = check_image("a.jpg", b"")
        assert p.severity == "error" and "empty" in p.message
        (p,) = check_image("a.png", b"\x00\x01\x02\x03 nope")
        assert p.severity == "error" and "not an image format GDI+ knows" in p.message and "00 01 02 03" in p.message

    @pytest.mark.parametrize(
        "data, fmt",
        [(WEBP, "WebP"), (AVIF, "AVIF"), (b"8BPS" + b"\x00" * 30, "Photoshop PSD"), (b"DDS " + b"\x00" * 30, "DDS")],
    )
    def test_formats_gdi_cannot_read(self, data, fmt):
        (p,) = check_image("Custom/Atom/Person/Textures/skin.jpg", data)
        assert p.severity == "error" and p.detected == fmt and f"{fmt} data in a .jpg file" in p.message

    def test_mismatched_extension_is_fine(self):
        assert check_image("thumb.jpg", png()) == []
        assert check_image("thumb.png", jpeg()) == []

    def test_jpeg_flavors(self):
        (p,) = check_image("a.jpg", jpeg(sof=0xC9))
        assert p.severity == "error" and p.message.startswith("arithmetic-coded JPEG")
        (p,) = check_image("a.jpg", jpeg(sof=0xCA))
        assert p.message.startswith("arithmetic-coded progressive JPEG")
        (p,) = check_image("a.jpg", jpeg(sof=0xC3))
        assert p.message.startswith("lossless JPEG")
        (p,) = check_image("a.jpg", jpeg(precision=12))
        assert p.severity == "error" and "12-bit JPEG" in p.message
        (p,) = check_image("a.jpg", jpeg(components=4))
        assert p.severity == "warning" and "CMYK" in p.message
        (p,) = check_image("a.jpg", jpeg(width=20000))
        assert p.severity == "warning" and "20000×48" in p.message and "16384" in p.message
        (p,) = check_image("a.jpg", jpeg(width=0))
        assert p.severity == "error" and "zero size" in p.message

    def test_jpeg_truncated_or_damaged(self):
        (p,) = check_image("a.jpg", jpeg(eoi=False))
        assert p.severity == "error" and "truncated JPEG" in p.message and "flat grey" in p.message
        full = jpeg()
        cut = full[: full.index(b"\xff\xda") + 8]
        assert [p.message.split(":")[0] for p in check_image("a.jpg", cut)] == ["truncated JPEG"]
        (p,) = check_image("a.jpg", b"\xff\xd8\xff" + b"\x00" * 40 + b"\xff\xd9")
        assert "damaged JPEG" in p.message
        assert check_image("a.jpg", jpeg(prefix=b"\xff\xff")) == []

    def test_png_truncated_or_damaged(self):
        (p,) = check_image("a.png", png(iend=False))
        assert p.severity == "error" and "truncated PNG" in p.message
        (p,) = check_image("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        assert "damaged PNG" in p.message
        (p,) = check_image("a.png", png(width=17000))
        assert p.severity == "warning" and "17000×4" in p.message
        (p,) = check_image("a.png", png(width=0))
        assert "zero size" in p.message

    def test_tiff_structure(self):
        assert check_image("Custom/Atom/Person/Textures/skin.tif", tiff()) == []
        assert check_image("skin.tiff", tiff(compression=5, little=False)) == []
        (p,) = check_image("skin.tif", tiff(width=20000))
        assert p.severity == "warning" and "20000×48" in p.message
        (p,) = check_image("skin.tif", tiff(width=0))
        assert p.severity == "error" and "zero size" in p.message
        (p,) = check_image("skin.tif", tiff(big=True))
        assert p.severity == "error" and "BigTIFF" in p.message
        (p,) = check_image("skin.tif", b"II*\x00" + struct.pack("<I", 5000) + b"\x00" * 8)
        assert p.severity == "error" and "damaged or truncated TIFF" in p.message

    @pytest.mark.parametrize(
        "data",
        [
            tiff(compression=6),
            tiff(compression=7, photometric=6),
            tiff(compression=34712),
            tiff(photometric=8),
            tiff(photometric=None),
            tiff(planar=2),
            tiff(samples=2),
            tiff(samples=4, extra=7),
            tiff(bits=16),
            tiff(bits=16, samples=4, extra=1),
            tiff(bits=16, samples=2, photometric=1),
            tiff(bits=16, samples=1, photometric=3),
            tiff(sample_format=3, bits=32),
            tiff(bits=24),
            tiff(photometric=5, samples=4),
            tiff(photometric=5, samples=4, bits=16),
            tiff(photometric=5, samples=3),
        ],
    )
    def test_tiff_variants_are_not_guessed_at(self, data):
        assert check_image("skin.tif", data) == []

    def test_gif_and_bmp_truncation(self):
        (p,) = check_image("a.gif", b"GIF89a" + b"\x00" * 20)
        assert p.severity == "warning" and "GIF" in p.message
        (p,) = check_image("a.bmp", b"BM" + struct.pack("<I", 1000) + b"\x00" * 24)
        assert p.severity == "error" and "truncated BMP" in p.message


FILES = {
    "meta.json": "{}",
    "Custom/Atom/Person/Textures/good.jpg": jpeg(),
    "Custom/Atom/Person/Textures/webp.jpg": WEBP,
    "Custom/Atom/Person/Textures/cut.png": png(iend=False),
    "Custom/Atom/Person/Textures/empty.png": b"",
    "Custom/Atom/Person/Textures/arith.JPEG": jpeg(sof=0xC9),
    "Saves/scene/thumb.jpg": png(),
    "Custom/Atom/Person/Morphs/female/x.vmb": b"\x00\x01",
    "Custom/Scripts/readme.txt": "not an image, not checked",
}


class TestPackages:
    def test_archive_and_folder_agree(self, tmp_path):
        data = build_var(meta={"packageName": "A.B"}, files={k: v for k, v in FILES.items() if k != "meta.json"})
        archive = tmp_path / "A.B.1.var"
        archive.write_bytes(data)
        folder = tmp_path / "A.B.2.var"
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            zf.extractall(folder)
        for report in (check_package_images(archive), check_package_images(folder)):
            assert report.images == 6 and report.bytes == sum(
                len(FILES[k]) for k in FILES if k.endswith((".jpg", ".png", ".JPEG"))
            )
            found = {p.path: p for p in report.problems}
            assert set(found) == {
                "Custom/Atom/Person/Textures/webp.jpg",
                "Custom/Atom/Person/Textures/cut.png",
                "Custom/Atom/Person/Textures/empty.png",
                "Custom/Atom/Person/Textures/arith.JPEG",
            }
            assert all(p.severity == "error" for p in report.problems) and len(report.problems) == 4

    def test_library_run_scopes_and_reports(self, vam):
        lib = make_lib(vam)
        drop(vam.addon_packages, "Dep.Tex.1.var", files={"Custom/Atom/Person/Textures/skin.jpg": WEBP})
        drop(vam.addon_packages, "Scene.Pkg.1.var", deps=["Dep.Tex.1"], files={"Saves/scene/s.jpg": jpeg()})
        drop(vam.addon_packages, "Other.Pkg.1.var", files={"Custom/Atom/Person/Textures/t.png": png(iend=False)})
        lib.scan()
        lib.create_dummies(["Gone.Pkg.1"])
        assert "Gone.Pkg.1.var" in by_name(lib)
        seen = []
        res = run_texture_check(lib, seen.append)
        assert res["total"] == 4 and res["checked"] == 4 and res["images"] == 3 and not res["cancelled"]
        assert res["packages_with_problems"] == ["Dep.Tex.1.var", "Other.Pkg.1.var"]
        assert res["error_count"] == 2 and res["warning_count"] == 0 and res["errors"] == {}
        assert {(p["filename"], p["path"]) for p in res["problems"]} == {
            ("Dep.Tex.1.var", "Custom/Atom/Person/Textures/skin.jpg"),
            ("Other.Pkg.1.var", "Custom/Atom/Person/Textures/t.png"),
        }
        assert seen[-1]["status"] == "done" and seen[-1]["problems"] == 2 and seen[0]["status"] == "checking"
        assert set(res["checked_files"]) == {"Dep.Tex.1.var", "Other.Pkg.1.var", "Scene.Pkg.1.var", "__local__"}
        res = lib.texture_check(filenames=["Scene.Pkg.1.var"], include_deps=True)
        assert res["total"] == 2 and res["packages_with_problems"] == ["Dep.Tex.1.var"]
        res = lib.texture_check(filenames=["Scene.Pkg.1.var"])
        assert res["total"] == 1 and res["problems"] == []
        (vam.addon_packages / "Other.Pkg.1.var").write_bytes(b"garbage")
        res = lib.texture_check(filenames=["Other.Pkg.1.var"])
        assert res["checked"] == 1 and "Other.Pkg.1.var" in res["errors"] and res["problems"] == []
        lib.close()

    def test_loose_files_and_vac_archives(self, vam):
        lib = make_lib(vam)
        drop(vam.addon_packages, "Scene.Pkg.1.var", files={"Saves/scene/s.jpg": jpeg()})
        scene_dir = vam.saves_dir / "scene"
        scene_dir.mkdir(parents=True, exist_ok=True)
        (scene_dir / "mine.jpg").write_bytes(WEBP)
        (scene_dir / "mine.json").write_text("{}")
        (vam.custom_dir / "Atom" / "Person" / "Textures").mkdir(parents=True)
        (vam.custom_dir / "Atom" / "Person" / "Textures" / "ok.png").write_bytes(png())
        (vam.custom_dir / "Atom" / "Person" / "Textures" / "skin.tif").write_bytes(tiff(big=True))
        vac = scene_dir / "Old style.vac"
        vac.write_bytes(
            build_var(
                files={
                    "scene.json": "{}",
                    "Custom/Atom/Person/Textures/legacy.tif": b"II*\x00" + struct.pack("<I", 5000) + b"\x00" * 8,
                    "Custom/Atom/Person/Textures/good.jpg": jpeg(),
                }
            )
        )
        (scene_dir / "broken.vac").write_bytes(b"not a zip")
        lib.scan()
        res = lib.texture_check()
        assert res["total"] == 2 and res["images"] == 6
        local = {p["path"]: p for p in res["problems"] if p["filename"] == "__local__"}
        assert set(local) == {
            "Saves/scene/mine.jpg",
            "Custom/Atom/Person/Textures/skin.tif",
            "Saves/scene/Old style.vac › Custom/Atom/Person/Textures/legacy.tif",
            "Saves/scene/broken.vac",
        }
        assert (
            local["Saves/scene/mine.jpg"]["severity"] == "error" and local["Saves/scene/mine.jpg"]["detected"] == "WebP"
        )
        assert local["Saves/scene/Old style.vac › Custom/Atom/Person/Textures/legacy.tif"]["severity"] == "error"
        assert "unreadable archive" in local["Saves/scene/broken.vac"]["message"]
        assert res["packages_with_problems"] == ["__local__"]
        assert lib.texture_check(filenames=["Scene.Pkg.1.var"])["total"] == 1
        only_local = lib.texture_check(filenames=["__local__"])
        assert only_local["total"] == 1 and len(only_local["problems"]) == 4
        lib.close()

    def test_cancel(self, vam):
        import threading

        lib = make_lib(vam)
        drop(vam.addon_packages, "A.Pkg.1.var", files={"Saves/scene/s.jpg": jpeg()})
        lib.scan()
        cancel = threading.Event()
        cancel.set()
        res = lib.texture_check(cancel=cancel)
        assert res["cancelled"] and res["checked"] == 0 and res["total"] == 2
        lib.close()
