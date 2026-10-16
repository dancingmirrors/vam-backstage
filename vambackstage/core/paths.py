from __future__ import annotations

import os
import sys
from pathlib import Path

ADDON_PACKAGES = "AddonPackages"
ADDON_PACKAGES_FILE_PREFS = "AddonPackagesFilePrefs"

LOCAL_PACKAGE_FILENAME = "__local__"
LOCAL_PACKAGE_DISPLAY_NAME = "Local content"

LOCAL_CONTENT_DIRS = ("Saves/scene", "Saves/Person", "Custom")

STORAGE_STATES = ("enabled", "disabled", "offloaded", "archived")

ACTIVENESS = {"enabled": 3, "disabled": 2, "offloaded": 1, "archived": 0}


def is_local_package(filename: str | None) -> bool:
    return filename == LOCAL_PACKAGE_FILENAME


def is_package_installed(storage_state: str | None) -> bool:
    return storage_state is not None


def is_package_active(storage_state: str | None) -> bool:
    return storage_state == "enabled"


def is_package_archived(storage_state: str | None) -> bool:
    return storage_state == "archived"


APP_DIR_NAME = "vam-backstage"


def user_data_dir() -> Path:
    override = os.environ.get("VAM_BACKSTAGE_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def user_cache_dir() -> Path:
    override = os.environ.get("VAM_BACKSTAGE_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_DIR_NAME
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_DIR_NAME
