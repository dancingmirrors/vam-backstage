from __future__ import annotations

import logging
import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .dispatch import EventBridge
from .thumbs import TextureLoader

log = logging.getLogger(__name__)

APP_ID = "org.dancingmirrors.VamBackstage"

_TILING_ENV = ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "NIRI_SOCKET", "I3SOCK", "RIVER_CONTROL_SOCKET")
_TILING_NAMES = (
    "sway",
    "hyprland",
    "river",
    "niri",
    "i3",
    "bspwm",
    "dwm",
    "dwl",
    "awesome",
    "qtile",
    "xmonad",
    "ratpoison",
    "cagebreak",
    "herbstluftwm",
    "leftwm",
    "stumpwm",
    "spectrwm",
    "mango",
)


def detect_tiling_wm() -> bool:
    env = os.environ
    if any(env.get(v) for v in _TILING_ENV):
        return True
    names = ":".join(env.get(v, "") for v in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")).lower()
    return any(n in names.split(":") for n in _TILING_NAMES)


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class App(Adw.Application):
    def __init__(
        self,
        backend,
        *,
        quit_after: float | None = None,
        screenshot: str | None = None,
        debug_view: str | None = None,
        debug_select: int | None = None,
        debug_scroll: int = 0,
        debug_status: str | None = None,
        debug_search: str | None = None,
        debug_action: str | None = None,
    ):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.backend = backend
        self.bridge = EventBridge(backend.events)
        self.textures = TextureLoader(backend)
        self.quit_after = quit_after
        self.screenshot = screenshot
        self.debug_view = debug_view
        self.debug_select = debug_select
        self.debug_scroll = debug_scroll
        self.debug_status = debug_status
        self.debug_search = debug_search
        self.debug_action = debug_action
        self.window = None
        self.tiling_mode = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_for_display(display).add_search_path(str(DATA_DIR / "icons"))
        tiling_setting = self.backend.lib.get_setting("tiling_wm")
        self.tiling_mode = tiling_setting == "1" if tiling_setting is not None else detect_tiling_wm()
        if tiling_setting is None and self.tiling_mode:
            log.info("tiling window manager detected; header-bar dragging and the client-side frame are off")
        scheme = self.backend.lib.get_setting("color_scheme") or "dark"
        Adw.StyleManager.get_default().set_color_scheme(
            {"dark": Adw.ColorScheme.FORCE_DARK, "light": Adw.ColorScheme.FORCE_LIGHT}.get(
                scheme, Adw.ColorScheme.DEFAULT
            )
        )
        self._add_action("quit", lambda *_: self.quit(), "<primary>q")

    def _add_action(self, name: str, cb, accel: str | None = None) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", cb)
        self.add_action(action)
        if accel:
            self.set_accels_for_action(f"app.{name}", [accel])

    def do_activate(self):
        from .window import MainWindow

        if self.window is None:
            self.window = MainWindow(self, self.backend)
        self.window.present()
        lib = self.backend.lib
        if not lib.is_configured or not lib.initial_scan_done:
            self.window.show_first_run()
        else:
            self.window.start_library()
        if self.debug_view or self.debug_select is not None or self.debug_action:
            GLib.timeout_add(2500, self._debug_apply)
        if self.quit_after:
            GLib.timeout_add(int(self.quit_after * 1000), self._timed_exit)

    def _debug_apply(self) -> bool:
        if self.debug_view == "settings":
            self.window.open_settings()
        elif self.debug_view:
            self.window.stack.set_visible_child_name(self.debug_view)
        if self.debug_status and hasattr(self.window.stack.get_visible_child(), "show_status"):
            self.window.stack.get_visible_child().show_status(self.debug_status)
        if self.debug_search and hasattr(self.window.stack.get_visible_child(), "search"):
            self.window.stack.get_visible_child().search.set_text(self.debug_search)
        if self.debug_select is not None or self.debug_scroll:
            self._debug_tries = 0
            GLib.timeout_add(400, self._debug_select_when_loaded)
        if self.debug_action:
            GLib.timeout_add(2500, self._debug_fire_action)
        return False

    def _debug_fire_action(self) -> bool:
        for root in (self.window.stack.get_visible_child(), self.window):
            obj = root
            for part in self.debug_action.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if callable(obj):
                obj()
                return False
        return False

    def _debug_select_when_loaded(self) -> bool:
        view = self.window.stack.get_visible_child()
        sel = getattr(view, "selection", None)
        self._debug_tries += 1
        if sel is None or sel.get_n_items() <= (self.debug_select or 0):
            return self._debug_tries < 20
        if self.debug_scroll:
            scroll = getattr(view, "scroll", None) or getattr(view, "grid_scroll", None)
            if scroll is not None:
                scroll.get_vadjustment().set_value(self.debug_scroll)
        if self.debug_select is not None:
            GLib.timeout_add(300, lambda: sel.set_selected(self.debug_select) and False)
        return False

    def _timed_exit(self) -> bool:
        if self.screenshot and self.window:
            try:
                self.window.save_screenshot(self.screenshot)
            except Exception:
                log.exception("screenshot failed")
        self.quit()
        return False

    def do_shutdown(self):
        self.textures.shutdown()
        try:
            self.backend.close()
        except Exception:
            log.exception("backend close failed")
        Adw.Application.do_shutdown(self)
