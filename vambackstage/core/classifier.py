from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import NamedTuple

from .content_types import VISIBLE_CATEGORIES, category_of


class FileEntry(NamedTuple):
    path: str
    size: int = 0


@dataclass
class ContentItem:
    internal_path: str
    display_name: str
    type: str
    thumbnail_path: str | None


@dataclass(frozen=True)
class _Rule:
    type: str
    match: Callable[[str], bool]
    extensions: frozenset[str]
    prefer: tuple[str, ...] = ()


def _prefix(pattern: str) -> Callable[[str], bool]:
    rx = re.compile(pattern, re.IGNORECASE)
    return lambda p: rx.match(p) is not None


_ATOM_PRESET_RX = re.compile(r"^Custom/[^/]+/[^/]+\.vap$", re.IGNORECASE)
_ATOM_PRESET_EXCLUDED = frozenset(
    {
        "atom",
        "clothing",
        "hair",
        "assets",
        "asset",
        "scripts",
        "plugindata",
        "sounds",
        "sound",
        "audio",
        "subscene",
        "subscenes",
    }
)


def _atom_preset_match(p: str) -> bool:
    if not _ATOM_PRESET_RX.match(p):
        return False
    return p.split("/")[1].lower() not in _ATOM_PRESET_EXCLUDED


RULES: tuple[_Rule, ...] = (
    _Rule("scene", _prefix(r"^Saves/scene/"), frozenset({".json"})),
    _Rule("legacyScene", _prefix(r"^Saves/scene/"), frozenset({".vac"})),
    _Rule("subscene", _prefix(r"^Custom/SubScenes?/"), frozenset({".json"})),
    _Rule("look", _prefix(r"^Custom/Atom/Person/Appearance/"), frozenset({".vap"})),
    _Rule("legacyLook", _prefix(r"^Saves/Person/Appearance/"), frozenset({".json"})),
    _Rule("skinPreset", _prefix(r"^Custom/Atom/Person/Skin/"), frozenset({".vap"})),
    _Rule("pose", _prefix(r"^Custom/Atom/Person/Pose/"), frozenset({".vap"})),
    _Rule("legacyPose", _prefix(r"^Saves/Person/Pose/"), frozenset({".json"})),
    _Rule(
        "clothingItem",
        _prefix(r"^Custom/Clothing/"),
        frozenset({".vab", ".vaj", ".vam"}),
        (".vam", ".vab", ".vaj"),
    ),
    _Rule("clothingPreset", _prefix(r"^Custom/Atom/Person/Clothing/"), frozenset({".vap"})),
    _Rule(
        "hairItem",
        _prefix(r"^Custom/Hair/"),
        frozenset({".vab", ".vaj", ".vam"}),
        (".vam", ".vab", ".vaj"),
    ),
    _Rule("hairPreset", _prefix(r"^Custom/Atom/Person/Hair/"), frozenset({".vap"})),
    _Rule("pluginPreset", _prefix(r"^Custom/Atom/Person/Plugins?/"), frozenset({".vap"})),
    _Rule(
        "morphBinary",
        _prefix(r"^Custom/Atom/Person/Morphs?/"),
        frozenset({".vmi", ".vmb", ".dsf"}),
        (".vmi", ".dsf", ".vmb"),
    ),
    _Rule("pluginScript", _prefix(r"^Custom/Scripts/"), frozenset({".cs"})),
    _Rule("scriptList", _prefix(r"^Custom/Scripts/"), frozenset({".cslist"})),
    _Rule("assetbundle", _prefix(r"^Custom/(Assets?|Sounds?|Audio)/"), frozenset({".assetbundle"})),
    _Rule(
        "audio",
        _prefix(r"^Custom/(Sounds?|Audio)/"),
        frozenset({".wav", ".mp3", ".ogg", ".aif", ".aiff"}),
    ),
    _Rule(
        "texture",
        _prefix(r"^Custom/Atom/Person/Textures?/"),
        frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"}),
    ),
    _Rule("atomPreset", _atom_preset_match, frozenset({".vap"})),
)

_EXT_PRIORITY: dict[tuple[str, str], int] = {}
for _rule in RULES:
    for _i, _ext in enumerate(_rule.prefer):
        _EXT_PRIORITY[(_rule.type, _ext)] = _i

_CROSS_TYPE_PAIRS = {
    "clothingItem": "clothingPreset",
    "clothingPreset": "clothingItem",
    "hairItem": "hairPreset",
    "hairPreset": "hairItem",
}

_THUMB_EXTS = (".jpg", ".jpeg", ".png")
_PRESET_PREFIX_RX = re.compile(r"^Preset ", re.IGNORECASE)


def strip_disabled_suffix(p: str) -> str:
    return p[: -len(".disabled")] if p.endswith(".disabled") else p


def _stem(p: str) -> str:
    return re.sub(r"\.[^.]+$", "", p)


def _find_thumbnail(content_path: str, all_paths: set[str]) -> str | None:
    base = _stem(content_path)
    for ext in _THUMB_EXTS:
        candidate = base + ext
        if candidate in all_paths:
            return candidate
    return None


def classify_contents(file_list: Iterable[FileEntry]) -> list[ContentItem]:
    files = list(file_list)
    path_set = {f.path for f in files}
    raw: list[tuple[ContentItem, str]] = []

    for f in files:
        live_path = strip_disabled_suffix(f.path)
        ext = posixpath.splitext(live_path)[1].lower()
        for rule in RULES:
            if ext in rule.extensions and rule.match(live_path):
                name = posixpath.splitext(posixpath.basename(live_path))[0]
                display_name = _PRESET_PREFIX_RX.sub("", name.replace("_", " "))
                thumb = _find_thumbnail(live_path, path_set)
                raw.append((ContentItem(f.path, display_name, rule.type, thumb), ext))
                break

    return _deduplicate(raw)


def _deduplicate(items: list[tuple[ContentItem, str]]) -> list[ContentItem]:
    groups: dict[tuple[str, str], tuple[ContentItem, int]] = {}
    pass1: list[ContentItem] = []
    for item, ext in items:
        pri = _EXT_PRIORITY.get((item.type, ext))
        if pri is None:
            pass1.append(item)
            continue
        key = (item.type, _stem(item.internal_path))
        existing = groups.get(key)
        if existing is None or pri < existing[1]:
            groups[key] = (item, pri)
    pass1.extend(item for item, _ in groups.values())

    by_display: dict[str, dict[str, ContentItem]] = {}
    for item in pass1:
        if item.type not in _CROSS_TYPE_PAIRS:
            continue
        by_display.setdefault(item.display_name.lower(), {})[item.type] = item

    suppressed: set[str] = set()
    for group in by_display.values():
        for type_ in list(group.keys()):
            partner = _CROSS_TYPE_PAIRS.get(type_)
            if not partner or partner not in group:
                continue
            a = group[type_]
            b = group[partner]
            if a.internal_path in suppressed or b.internal_path in suppressed:
                continue
            a_thumb = bool(a.thumbnail_path)
            b_thumb = bool(b.thumbnail_path)
            if a_thumb and not b_thumb:
                winner, loser = a, b
            elif b_thumb and not a_thumb:
                winner, loser = b, a
            else:
                a_is_preset = a.type.endswith("Preset")
                winner, loser = (a, b) if a_is_preset else (b, a)
            if not winner.thumbnail_path and loser.thumbnail_path:
                winner.thumbnail_path = loser.thumbnail_path
            suppressed.add(loser.internal_path)

    return [item for item in pass1 if item.internal_path not in suppressed]


def derive_package_type(content_items: Iterable[ContentItem]) -> str | None:
    present = {category_of(item.type) for item in content_items}
    for cat in VISIBLE_CATEGORIES:
        if cat in present:
            return cat
    return None
