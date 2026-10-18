from __future__ import annotations

VISIBLE_CATEGORIES = ("Scenes", "Looks", "Poses", "Clothing", "Hairstyles")
DETAIL_ONLY_CATEGORIES = ("SubScenes",)

LOOK_ITEM_EXACT_TYPES = frozenset({"look", "legacyLook", "skinPreset"})

EXACT_TYPE_TO_CATEGORY: dict[str, str | None] = {
    "scene": "Scenes",
    "legacyScene": "Scenes",
    "subscene": "SubScenes",
    "look": "Looks",
    "legacyLook": "Looks",
    "skinPreset": "Looks",
    "pose": "Poses",
    "legacyPose": "Poses",
    "clothingItem": "Clothing",
    "clothingPreset": "Clothing",
    "hairItem": "Hairstyles",
    "hairPreset": "Hairstyles",
    "atomPreset": None,
    "pluginScript": None,
    "scriptList": None,
    "pluginPreset": None,
    "morphBinary": None,
    "assetbundle": None,
    "audio": None,
    "texture": None,
}

TAG_PRESET = {"label": "Preset", "color": "#7dd3fc"}
TAG_LEGACY = {"label": "Legacy", "color": "#fbbf24"}

EXACT_TYPE_TAG = {
    "legacyScene": TAG_LEGACY,
    "legacyLook": TAG_LEGACY,
    "legacyPose": TAG_LEGACY,
    "skinPreset": {**TAG_PRESET, "label": "Skin Preset"},
    "clothingPreset": TAG_PRESET,
    "hairPreset": TAG_PRESET,
}

CATEGORY_COLORS = {
    "Scenes": "#3b82f6",
    "Looks": "#ec4899",
    "Clothing": "#8b5cf6",
    "Hairstyles": "#f59e0b",
    "Poses": "#f97316",
    "SubScenes": "#06b6d4",
}


def category_of(exact_type: str | None) -> str | None:
    return EXACT_TYPE_TO_CATEGORY.get(exact_type)  # type: ignore[arg-type]


def tag_of(exact_type: str | None) -> dict | None:
    return EXACT_TYPE_TAG.get(exact_type)  # type: ignore[arg-type]


def is_visible(exact_type: str | None) -> bool:
    return category_of(exact_type) is not None


def is_gallery_visible(exact_type: str | None) -> bool:
    cat = category_of(exact_type)
    return cat is not None and cat in VISIBLE_CATEGORIES


def is_core_package_category(pkg_type: str | None) -> bool:
    return bool(pkg_type) and pkg_type in VISIBLE_CATEGORIES
