from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import gi
from gi.repository import Adw, GLib, Gtk

from .. import __version__
from ..hub.client import HUB_HOST, is_hub_url, parse_hub_download_url, parse_hub_resource_url, pick_hub_file
from ..hub.downloads import ensure_var_ext
from .dispatch import run_async
from .widgets import icon_or_text_button

log = logging.getLogger(__name__)

try:
    gi.require_version("WebKit", "6.0")
    gi.require_version("Soup", "3.0")
    from gi.repository import Soup, WebKit
except Exception as _err:
    log.info("WebKitGTK is not usable (%s). Hub pages open in the web browser.", _err)
    Soup = WebKit = None

SETTING_KEY = "hub_pages_in_app"
CONSENT_COOKIE = ("vamhubconsent", "yes")
DEMO_HTML = (
    "<!doctype html><html><body style='font-family:sans-serif;background:#1e1e2e;color:#cdd6f4;padding:2em'>"
    "<h1>Hub pages open here</h1><p>Smoke-test page: the real dialog loads hub.virtamate.com.</p></body></html>"
)
_TEN_YEARS = 10 * 365 * 24 * 3600
_session = None
_dialog: HubWebDialog | None = None


def available() -> bool:
    return WebKit is not None and os.environ.get("VAM_BACKSTAGE_NO_WEBVIEW", "0") in ("", "0")


def enabled(lib) -> bool:
    return available() and lib.get_setting(SETTING_KEY) != "0"


def missing_hint() -> str:
    if WebKit is not None:
        return "Turned off by VAM_BACKSTAGE_NO_WEBVIEW in the environment. Hub pages open in your web browser."
    return "Needs the WebKitGTK 6.0 introspection data. Until then Hub pages open in your web browser."


def show(backend, window, url: str | None) -> HubWebDialog:
    global _dialog
    if _dialog is None:
        _dialog = HubWebDialog(backend, window)
    if url:
        _dialog.load(url)
    _dialog.present(window)
    return _dialog


def _open_externally(url: str) -> None:
    from .launch import open_url_externally

    open_url_externally(url)


def _leaves_hub(uri: str) -> bool:
    return urlsplit(uri).scheme in ("http", "https") and not is_hub_url(uri)


def _network_session(backend):
    global _session
    if _session is None:
        data = Path(backend.lib.db.path).parent / "webkit"
        _session = WebKit.NetworkSession.new(str(data), str(Path(backend.cache_dir) / "webkit"))
        cookies = _session.get_cookie_manager()
        cookies.set_persistent_storage(str(data / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        cookies.set_accept_policy(WebKit.CookieAcceptPolicy.NO_THIRD_PARTY)
        cookies.add_cookie(Soup.Cookie.new(*CONSENT_COOKIE, HUB_HOST, "/", _TEN_YEARS), None, lambda *_: None)
        _session.connect("download-started", _on_download_started)
    return _session


def _on_download_started(_session, download) -> None:
    download.cancel()
    uri = download.get_request().get_uri()
    if _dialog is not None:
        _dialog.take_file(uri)
    else:
        _open_externally(uri)


class HubWebDialog(Adw.Dialog):

    def __init__(self, backend, window):
        super().__init__(title="VaM Hub", content_width=1040, content_height=760)
        self.backend = backend
        self.window = window
        self._wanted: str | None = None
        self._failed: str | None = None
        self._passthrough: str | None = None
        self._handled: set[str] = set()
        self.web = WebKit.WebView(network_session=_network_session(backend))
        settings = self.web.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_back_forward_navigation_gestures(True)
        settings.set_user_agent_with_application_details("VaMBackstage", __version__)

        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="VaM Hub")
        header.set_title_widget(self.title_widget)
        self.back = self._nav_button(header, "go-previous-symbolic", "Back", self.web.go_back)
        self.forward = self._nav_button(header, "go-next-symbolic", "Forward", self.web.go_forward)
        self._nav_button(header, "view-refresh-symbolic", "Reload", self.web.reload)
        external = Gtk.Button(icon_name="adw-external-link-symbolic", tooltip_text="Open in your web browser")
        external.connect("clicked", lambda *_: self.open_externally())
        header.pack_end(external)
        self._bookmark_rid: str | None = None
        self.bookmark = icon_or_text_button("bookmark-new-symbolic", "Bookmark", "Bookmark this resource")
        self.bookmark.set_sensitive(False)
        self.bookmark.connect("clicked", lambda *_: self.toggle_bookmark())
        header.pack_end(self.bookmark)
        window.bridge.connect("bookmarks:updated", lambda _p: self._sync_bookmark())

        self.progress = Gtk.ProgressBar(valign=Gtk.Align.START, can_target=False, visible=False)
        self.progress.add_css_class("osd")
        overlay = Gtk.Overlay(child=self.web)
        overlay.add_overlay(self.progress)
        self.error = Adw.StatusPage(icon_name="network-offline-symbolic", title="The Hub did not answer")
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self.retry())
        buttons.append(retry)
        browser = Gtk.Button(label="Open in your web browser")
        browser.add_css_class("pill")
        browser.connect("clicked", lambda *_: self.open_externally())
        buttons.append(browser)
        self.error.set_child(buttons)
        self.stack = Gtk.Stack()
        self.stack.add_named(overlay, "web")
        self.stack.add_named(self.error, "error")
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_child(self.toasts)

        shortcuts = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        for keys, action in (
            ("<Alt>Left", self.web.go_back),
            ("<Alt>Right", self.web.go_forward),
            ("F5", self.web.reload),
            ("<Control>r", self.web.reload),
            ("<Control>d", self.toggle_bookmark),
        ):
            shortcuts.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(keys),
                    Gtk.CallbackAction.new(lambda *_, fn=action: bool(fn()) or True),
                )
            )
        self.add_controller(shortcuts)

        self.web.connect("decide-policy", self._decide_policy)
        self.web.connect("create", self._on_create)
        self.web.connect("load-changed", self._on_load_changed)
        self.web.connect("load-failed", self._on_load_failed)
        self.web.connect("web-process-terminated", self._on_terminated)
        self.web.connect("notify::title", lambda *_: self._sync_title())
        self.web.connect("notify::uri", lambda *_: self._sync_nav())
        self.web.connect("notify::estimated-load-progress", lambda *_: self._sync_progress())
        self.connect("closed", self._on_closed)

    def load(self, url: str) -> None:
        self._wanted = url
        self.web.load_uri(url)

    def retry(self) -> None:
        target = self._failed or self._wanted
        if target:
            self.web.load_uri(target)
        else:
            self.web.reload()

    def load_html(self, html: str) -> None:
        self.web.load_html(html, None)

    def add_toast(self, toast: Adw.Toast) -> None:
        self.toasts.add_toast(toast)

    def open_externally(self) -> None:
        uri = self.web.get_uri() or self._wanted
        if uri and is_hub_url(uri):
            _open_externally(uri)

    def take_file(self, uri: str) -> None:
        parsed = parse_hub_download_url(uri)
        if not parsed or uri in self._handled:
            _open_externally(uri)
            return
        rid, file_id, version_id = parsed

        def work():
            detail = self.backend.hub.get_resource_detail(rid)
            hub_file = pick_hub_file(detail, file_id, version_id)
            if hub_file is None:
                return None
            res = self.backend.downloads.enqueue_install(rid, detail, target_filename=hub_file["filename"])
            return ensure_var_ext(hub_file["filename"]), res

        def done(result):
            if result is None:
                if self.get_parent() is None:
                    return
                self._handled.add(uri)
                self._passthrough = uri
                self.web.load_uri(uri)
                return
            fn, res = result
            if res["inserted"]:
                msg = f"Queued {fn} for download"
                if res.get("unresolved_deps"):
                    n = len(res["unresolved_deps"])
                    msg += f"; {n} dependenc{'y' if n == 1 else 'ies'} not available on the Hub"
            elif res["already_queued"]:
                msg = f"{fn} is already downloading"
            else:
                msg = f"{fn} is already in your library"
            self.window.toast(msg)

        run_async(work, on_done=done, on_error=lambda err: self.window.toast(f"Could not queue the download: {err}", 8))

    def toggle_bookmark(self) -> None:
        rid = self._bookmark_rid
        if not rid:
            return
        if self.backend.lib.is_bookmarked(rid):
            self.window.run_op(self.backend.lib.remove_bookmark, rid, success="Bookmark removed")
            return
        title = re.sub(r"\s*\|\s*Virt-A-Mate Hub\s*$", "", self.web.get_title() or "", flags=re.IGNORECASE).strip()
        self.bookmark.set_sensitive(False)

        def done(res):
            self._sync_bookmark()
            self.window.toast(f"Bookmarked {res['title']}: listed in the library under Bookmarked")

        def failed(err):
            self._sync_bookmark()
            self.window.toast(f"Could not bookmark it: {err}", 8)

        run_async(self.backend.bookmark_resource, rid, None, None, title or None, on_done=done, on_error=failed)

    def _sync_bookmark(self) -> None:
        rid = parse_hub_resource_url(self.web.get_uri() or "")
        self._bookmark_rid = rid
        self.bookmark.set_sensitive(bool(rid))
        marked = bool(rid) and self.backend.lib.is_bookmarked(rid)
        if marked:
            self.bookmark.add_css_class("suggested-action")
            self.bookmark.set_tooltip_text("Bookmarked; listed in the library under Bookmarked. Click to forget it")
        else:
            self.bookmark.remove_css_class("suggested-action")
            self.bookmark.set_tooltip_text(
                "Bookmark this resource (Ctrl+D): a note to self in the library under Bookmarked, "
                "for packages to buy or fetch elsewhere, until the package arrives"
                if rid
                else "Open a resource page to bookmark it"
            )

    def _nav_button(self, header: Adw.HeaderBar, icon: str, tip: str, action) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, tooltip_text=tip)
        button.connect("clicked", lambda *_: action())
        header.pack_start(button)
        return button

    def _sync_title(self) -> None:
        title = self.web.get_title() or "VaM Hub"
        self.title_widget.set_title(title)
        self.set_title(title)

    def _sync_nav(self) -> None:
        self.back.set_sensitive(self.web.can_go_back())
        self.forward.set_sensitive(self.web.can_go_forward())
        self.title_widget.set_subtitle(self.web.get_uri() or "")
        self._sync_bookmark()

    def _sync_progress(self) -> None:
        fraction = self.web.get_estimated_load_progress()
        self.progress.set_fraction(fraction)
        self.progress.set_visible(self.web.is_loading() and fraction < 1.0)

    def _show_status(self, title: str, message: str) -> None:
        self.error.set_title(title)
        self.error.set_description(GLib.markup_escape_text(message))
        self.stack.set_visible_child_name("error")

    def _route(self, uri: str) -> None:
        if urlsplit(uri).scheme not in ("http", "https"):
            return
        if is_hub_url(uri):
            self.web.load_uri(uri)
        else:
            _open_externally(uri)

    def _on_create(self, _web, action):
        self._route(action.get_request().get_uri())
        return None

    def _decide_policy(self, _web, decision, kind) -> bool:
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            self._route(decision.get_navigation_action().get_request().get_uri())
        elif kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if uri == self._passthrough:
                self._passthrough = None
                return False
            if parse_hub_download_url(uri):
                self.take_file(uri)
            elif urlsplit(uri).scheme not in ("http", "https", "about", "data", "blob"):
                _open_externally(uri)
            elif action.is_user_gesture() and _leaves_hub(uri):
                _open_externally(uri)
            else:
                return False
        elif kind == WebKit.PolicyDecisionType.RESPONSE:
            uri = decision.get_response().get_uri()
            if not decision.is_main_frame_main_resource():
                if decision.is_mime_type_supported():
                    return False
            elif not decision.is_mime_type_supported():
                self.take_file(uri)
            elif _leaves_hub(uri):
                _open_externally(uri)
            else:
                return False
        else:
            return False
        decision.ignore()
        return True

    def _on_load_changed(self, _web, event) -> None:
        if event == WebKit.LoadEvent.STARTED:
            self.stack.set_visible_child_name("web")
        elif event == WebKit.LoadEvent.COMMITTED:
            self._handled.clear()
            self._failed = None
        elif event == WebKit.LoadEvent.FINISHED:
            self.progress.set_visible(False)
        self._sync_nav()

    def _on_load_failed(self, _web, _event, failing_uri: str, error: GLib.Error) -> bool:
        if error.matches(WebKit.NetworkError.quark(), WebKit.NetworkError.CANCELLED) or error.matches(
            WebKit.PolicyError.quark(), WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE
        ):
            if not self.web.get_uri():
                self._show_status(
                    "Nothing to show here",
                    "That link was passed on: a package goes to the download queue, anything else to your web browser.",
                )
            return False
        if error.matches(WebKit.PolicyError.quark(), WebKit.PolicyError.CANNOT_SHOW_URI):
            _open_externally(failing_uri)
            return True
        log.info("Hub page %s failed: %s", failing_uri, error.message)
        self._failed = failing_uri
        if self.web.get_uri():
            self.window.toast(f"Could not open that page: {error.message}", 8)
        else:
            self._show_status("The Hub did not answer", error.message)
        return True

    def _on_terminated(self, _web, reason) -> None:
        if reason == WebKit.WebProcessTerminationReason.TERMINATED_BY_API:
            return
        log.warning("WebKit web process for the Hub page stopped: %s", reason.value_nick)
        self._show_status(
            "The page renderer stopped", f"WebKit reported: {reason.value_nick}. Your web browser still works."
        )

    def _on_closed(self, *_) -> None:
        self.web.stop_loading()
        self._passthrough = None
        self._handled.clear()
        self.web.terminate_web_process()
