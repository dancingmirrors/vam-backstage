from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from gi.repository import Gdk, Gio, GLib, Gtk

from .dispatch import run_async

log = logging.getLogger(__name__)


def _toast(message: str, timeout: int = 8) -> None:
    window = getattr(Gtk.Application.get_default(), "window", None)
    if window is not None and hasattr(window, "toast"):
        window.toast(message, timeout)
    else:
        log.warning(message)


def copy_to_clipboard(text: str) -> bool:
    display = Gdk.Display.get_default()
    if display is None:
        return False
    try:
        display.get_clipboard().set(text)
        return True
    except Exception:
        log.debug("clipboard unavailable", exc_info=True)
        return False


def _unopenable(what: str, target: str) -> None:
    copied = copy_to_clipboard(target)
    _toast(f"{what} {target}" + (" (copied to the clipboard)" if copied else ""))


def _gio_open(uri: str) -> bool:
    try:
        return bool(Gio.AppInfo.launch_default_for_uri(uri, None))
    except GLib.Error as err:
        log.info("GIO could not open %s: %s", uri, err.message)
        return False


def _spawn(argv: list[str], settle: float = 0.0) -> bool:
    exe = shutil.which(argv[0])
    if not exe:
        return False
    try:
        proc = subprocess.Popen([exe, *argv[1:]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as err:
        log.info("could not run %s: %s", argv[0], err)
        return False
    if settle:
        try:
            code = proc.wait(timeout=settle)
        except subprocess.TimeoutExpired:
            return True
        if code != 0:
            log.info("%s exited with %s", argv[0], code)
            return False
    return True


_BROWSERS = (
    "xdg-open",
    "x-www-browser",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "firefox",
    "firefox-esr",
    "librewolf",
    "epiphany",
    "falkon",
    "qutebrowser",
    "vivaldi",
    "opera",
    "midori",
)


def _open_url_fallbacks(url: str) -> bool:
    custom = os.environ.get("BROWSER", "").strip()
    for entry in filter(None, custom.split(os.pathsep)):
        argv = [a.replace("%s", url) for a in entry.split()] if "%s" in entry else [*entry.split(), url]
        if _spawn(argv, settle=1.0):
            return True
    if sys.platform == "win32":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _spawn(["open", url], settle=1.0)
    for name in _BROWSERS:
        if _spawn([name, url], settle=2.0 if name in ("xdg-open", "x-www-browser") else 0.0):
            log.info("opened %s with %s", url, name)
            return True
    return False


def open_url(url: str) -> None:
    if _gio_open(url):
        return
    run_async(
        _open_url_fallbacks,
        url,
        on_done=lambda ok: None if ok else _unopenable("No browser found to open", url),
        on_error=lambda _err: _unopenable("No browser found to open", url),
    )


def open_in_file_manager(path: str) -> None:
    folder = os.path.dirname(path) if os.path.isfile(path) else path
    if sys.platform == "win32":
        ok = _spawn(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        ok = _spawn(["open", "-R", path])
    else:
        ok = _gio_open(Gio.File.new_for_path(folder).get_uri()) or _spawn(["xdg-open", folder])
    if not ok:
        _unopenable("No file manager found to open", folder)
