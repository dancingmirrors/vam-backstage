#!/usr/bin/env python3

from __future__ import annotations

import argparse
import io
import json
import random
import struct
import zipfile
import zlib
from pathlib import Path

CREATORS = [
    "AcidBubbles",
    "MacGruber",
    "Hazmhox",
    "ClockwiseSilver",
    "Spacedog",
    "VamTastic",
    "Molmark",
    "AshAuryn",
    "Xstatic",
    "Jackaroo",
]
WORDS = [
    "Neon",
    "Velvet",
    "Harbor",
    "Orchid",
    "Granite",
    "Ember",
    "Lagoon",
    "Juniper",
    "Quartz",
    "Saffron",
    "Willow",
    "Cobalt",
    "Dune",
    "Meadow",
    "Ivory",
    "Tundra",
]


def png(width: int, height: int, rgb: tuple[int, int, int], rgb2: tuple[int, int, int]) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            t = (x + y) / (width + height)
            row += bytes(int(a + (b - a) * t) for a, b in zip(rgb, rgb2, strict=True))
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


def hsl_to_rgb(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hls_to_rgb(h, lightness, s)
    return int(r * 255), int(g * 255), int(b * 255)


def build_var(meta: dict, files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, indent=2))
        for p, c in files.items():
            zf.writestr(p, c)
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--packages", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    root = Path(args.target)
    addon = root / "AddonPackages"
    addon.mkdir(parents=True, exist_ok=True)
    (root / "AddonPackagesFilePrefs").mkdir(exist_ok=True)

    packages: list[dict] = []
    kinds = ["scene", "look", "clothing", "hair", "pose", "plugin", "morphs", "assets"]
    for i in range(args.packages):
        creator = rnd.choice(CREATORS)
        name = (
            rnd.choice(WORDS)
            + rnd.choice(["", "Pack", "Set", "Collection", "Studio", "Lab"])
            + ("" if rnd.random() < 0.7 else str(rnd.randint(2, 9)))
        )
        kind = rnd.choice(kinds if i > 6 else ["plugin", "morphs", "assets", "hair", "clothing"])
        version = rnd.choice([1, 1, 1, 2, 3, 5, 12])
        group = f"{creator}.{name}"
        if any(p["group"] == group for p in packages):
            name += "X"
            group = f"{creator}.{name}"
        packages.append(
            {"creator": creator, "name": name, "group": group, "version": version, "kind": kind, "deps": []}
        )

    for i, p in enumerate(packages):
        if p["kind"] in ("scene", "look", "pose") and i > 4:
            n = rnd.randint(1, 4)
            pool = [q for q in packages[:i] if q["kind"] in ("plugin", "morphs", "assets", "hair", "clothing", "look")]
            for q in rnd.sample(pool, min(n, len(pool))):
                ref = f"{q['group']}.{rnd.choice([str(q['version']), 'latest', str(q['version'])])}"
                p["deps"].append(ref)
    packages[-1]["deps"].append("Missing.Creator.Thing.3")
    packages[-2]["deps"].append("AcidBubbles.Timeline.latest")

    hue = 0.0
    written: list[tuple[dict, Path]] = []
    for idx, p in enumerate(packages):
        hue = (hue + 0.137) % 1.0
        c1, c2 = hsl_to_rgb(hue, 0.55, 0.35), hsl_to_rgb((hue + 0.1) % 1.0, 0.7, 0.6)
        thumb = png(96, 96, c1, c2)
        files: dict[str, bytes | str] = {}
        stem = p["name"]
        if p["kind"] == "scene":
            for k in range(rnd.randint(1, 3)):
                files[f"Saves/scene/{stem}_{k}.json"] = json.dumps({"atoms": [{"id": "Person", "type": "Person"}]})
                files[f"Saves/scene/{stem}_{k}.jpg"] = thumb
            files[f"Custom/Atom/Person/Appearance/Preset_{stem}.vap"] = "{}"
        elif p["kind"] == "look":
            files[f"Custom/Atom/Person/Appearance/Preset_{stem}.vap"] = "{}"
            files[f"Custom/Atom/Person/Appearance/Preset_{stem}.jpg"] = thumb
            files[f"Custom/Atom/Person/Skin/Preset_{stem}_skin.vap"] = "{}"
        elif p["kind"] == "clothing":
            for k in range(rnd.randint(1, 5)):
                files[f"Custom/Clothing/Female/{p['creator']}/{stem}{k}/{stem}{k}.vam"] = "{}"
                files[f"Custom/Clothing/Female/{p['creator']}/{stem}{k}/{stem}{k}.vaj"] = "{}"
                files[f"Custom/Clothing/Female/{p['creator']}/{stem}{k}/{stem}{k}.vab"] = b"\0"
                files[f"Custom/Clothing/Female/{p['creator']}/{stem}{k}/{stem}{k}.jpg"] = thumb
        elif p["kind"] == "hair":
            files[f"Custom/Hair/Female/{p['creator']}/{stem}/{stem}.vam"] = "{}"
            files[f"Custom/Hair/Female/{p['creator']}/{stem}/{stem}.jpg"] = thumb
        elif p["kind"] == "pose":
            for k in range(rnd.randint(2, 6)):
                files[f"Custom/Atom/Person/Pose/Preset_{stem}_{k}.vap"] = "{}"
                files[f"Custom/Atom/Person/Pose/Preset_{stem}_{k}.jpg"] = thumb
        elif p["kind"] == "plugin":
            files[f"Custom/Scripts/{p['creator']}/{stem}/{stem}.cs"] = "// plugin"
            files[f"Custom/Scripts/{p['creator']}/{stem}/{stem}.cslist"] = f"{stem}.cs"
        elif p["kind"] == "morphs":
            for k in range(rnd.randint(5, 40)):
                files[f"Custom/Atom/Person/Morphs/female/{p['creator']}/{stem}_{k}.vmi"] = "{}"
                files[f"Custom/Atom/Person/Morphs/female/{p['creator']}/{stem}_{k}.vmb"] = b"\0"
        else:
            files[f"Custom/Assets/{p['creator']}/{stem}.assetbundle"] = b"\0" * 2048
        meta = {
            "licenseType": rnd.choice(["CC BY", "CC BY-NC", "CC BY-SA", "FC", "PC"]),
            "creatorName": p["creator"],
            "packageName": p["name"],
            "description": f"{p['kind'].title()} package {stem} by {p['creator']}. "
            + rnd.choice(["Demo content.", "Includes several variants.", "Requires the listed dependencies."]),
            "dependencies": {d: {"licenseType": "CC BY", "dependencies": {}} for d in p["deps"]},
        }
        data = build_var(meta, files)
        filename = f"{p['group']}.{p['version']}.var"
        sub = addon / rnd.choice(["", "", "", p["creator"]])
        sub.mkdir(parents=True, exist_ok=True)
        (sub / filename).write_bytes(data)
        written.append((p, sub / filename))
        if idx % 11 == 5:
            (sub / (filename + ".disabled")).write_bytes(b"")
        if idx % 17 == 3:
            (sub / f"{p['group']}.{max(1, p['version'] - 1)}.var").write_bytes(
                build_var({**meta, "description": "Older version"}, files)
            )

    for p, path in written:
        if p["kind"] != "scene":
            continue
        with zipfile.ZipFile(path) as zf:
            zf.extractall(str(path) + ".unpacking")
        path.rename(str(path) + ".orig")
        Path(str(path) + ".unpacking").rename(path)
        break

    (root / "Saves" / "scene").mkdir(parents=True, exist_ok=True)
    (root / "Saves" / "scene" / "My own scene.json").write_text(
        json.dumps({"atoms": [{"id": "Person", "type": "Person"}]})
    )
    (root / "Saves" / "scene" / "My own scene.jpg").write_bytes(png(96, 96, (40, 60, 90), (90, 160, 220)))
    (root / "Custom" / "Atom" / "Person" / "Appearance").mkdir(parents=True, exist_ok=True)
    (root / "Custom" / "Atom" / "Person" / "Appearance" / "Preset_Loose look.vap").write_text("{}")
    print(f"Demo library written to {root} ({len(packages)} packages)")


if __name__ == "__main__":
    main()
