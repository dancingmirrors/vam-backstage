from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gio, GLib, Gtk

from ..core.paths import is_local_package
from ..core.textures import LOCAL_LABEL
from .app import APP_ID
from .dispatch import Debouncer, on_main, run_async
from .library_view import LibraryView
from .models import human_size
from .widgets import hbox, label, strip_window_drag

log = logging.getLogger(__name__)


class PlaceholderView(Gtk.Box):
    def __init__(self, title: str, description: str, icon: str):
        super().__init__()
        page = Adw.StatusPage(title=title, description=description, icon_name=icon)
        page.set_hexpand(True)
        page.set_vexpand(True)
        self.append(page)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend):
        super().__init__(application=app, title="VaM Backstage", default_width=1360, default_height=860)
        self.app = app
        self.backend = backend
        self.bridge = app.bridge
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)

        self.stack = Adw.ViewStack()
        self.header = header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.plain_title = Adw.WindowTitle(title="VaM Backstage")
        header.set_title_widget(self.switcher)
        menu = Gio.Menu()
        menu.append("Rescan library", "win.rescan")
        menu.append("Check for updates on the Hub", "win.check-updates")
        menu.append("Settings", "win.settings")
        menu.append("About", "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)
        self.rescan_spinner = Gtk.Spinner()
        header.pack_end(self.rescan_spinner)
        toolbar.add_top_bar(header)

        from .content_view import ContentView
        from .downloads_view import DownloadsView
        from .hub_view import HubView

        self.library = LibraryView(self)
        self.content = ContentView(self)
        self.hub = HubView(self)
        self.downloads = DownloadsView(self)
        self.stack.add_titled_with_icon(self.library, "library", "Library", "view-grid-symbolic")
        self.stack.add_titled_with_icon(self.content, "content", "Content", "view-paged-symbolic")
        self.stack.add_titled_with_icon(self.hub, "hub", "Hub", "network-workgroup-symbolic")
        self.downloads_page = self.stack.add_titled_with_icon(
            self.downloads, "downloads", "Downloads", "folder-download-symbolic"
        )

        self.setup_page = Adw.StatusPage(
            title="No library yet",
            icon_name="folder-open-symbolic",
            description="Choose your Virt-a-Mate folder and Backstage will index its packages.",
        )
        setup_button = Gtk.Button(label="Set up library", halign=Gtk.Align.CENTER)
        setup_button.add_css_class("suggested-action")
        setup_button.add_css_class("pill")
        setup_button.connect("clicked", lambda *_: self.show_first_run())
        self.setup_page.set_child(setup_button)
        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(self.setup_page, "setup")
        self.content_stack.add_named(self.stack, "views")
        toolbar.set_content(self.content_stack)
        self.connect("close-request", self._on_close_request)
        self.connect("notify::visible-dialog", self._on_visible_dialog)
        if app.tiling_mode:
            self.set_tiling_mode(True)

        self.status = hbox(16)
        self.status.add_css_class("vb-statusbar")
        self.status_counts = label("", "vb-dim")
        self.status.append(self.status_counts)
        self.status_progress = Gtk.ProgressBar(show_text=False, valign=Gtk.Align.CENTER)
        self.status_progress.set_visible(False)
        self.status_progress_label = label("", "vb-dim")
        self.status_progress_label.set_visible(False)
        self.status.append(self.status_progress)
        self.status.append(self.status_progress_label)
        self.status_stop = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.status_stop.add_css_class("flat")
        self.status_stop.set_visible(False)
        self.status_stop.connect("clicked", lambda *_: self._integrity_cancel.set() if self._integrity_cancel else None)
        self.status.append(self.status_stop)
        self._integrity_cancel: threading.Event | None = None
        spacer = Gtk.Box(hexpand=True)
        self.status.append(spacer)
        from .. import __version__

        self.status.append(label(f"v{__version__}", "vb-dim"))
        toolbar.add_bottom_bar(self.status)

        self.sync_configured_state()
        self._add_action("rescan", self.rescan)
        self._add_action("check-updates", lambda *_: self.library.check_updates())
        self._add_action("settings", self.open_settings)
        self._add_action("about", self.show_about)
        self.app.set_accels_for_action("win.settings", ["<primary>comma"])
        self.app.set_accels_for_action("win.rescan", ["F5"])

        self._stats_debounce = Debouncer(200, self.refresh_stats)
        self.bridge.connect("packages:updated", lambda _p: self._stats_debounce.trigger())
        self.bridge.connect("contents:updated", lambda _p: self._stats_debounce.trigger())
        self.bridge.connect("scan:progress", self._on_scan_progress)
        self.bridge.connect("toast", self._on_toast)
        self.bridge.connect("downloads:updated", lambda _p: self._update_downloads_badge())
        self.stack.connect("notify::visible-child", lambda *_: self._on_view_changed())
        self._first_run = None
        self._settings_dialog = None
        self._about_dialog = None
        self.closing = False

    def _add_action(self, name: str, cb) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: cb())
        self.add_action(action)

    def toast(self, message: str, timeout: int = 4) -> None:
        previous = getattr(self, "_current_toast", None)
        if previous is not None:
            previous.dismiss()
        toast = Adw.Toast(title=message, timeout=timeout, use_markup=False)
        self._current_toast = toast
        toast.connect(
            "dismissed",
            lambda t: setattr(self, "_current_toast", None) if getattr(self, "_current_toast", None) is t else None,
        )
        dialog = self.get_visible_dialog()
        if isinstance(dialog, Adw.PreferencesDialog):
            dialog.add_toast(toast)
        else:
            self.toasts.add_toast(toast)

    def _on_toast(self, payload):
        if isinstance(payload, dict) and payload.get("message"):
            self.toast(payload["message"], 6)

    def confirm(self, heading: str, body: str, action_label: str, on_ok, destructive: bool = True) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", action_label)
        if destructive:
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: on_ok() if r == "ok" else None)
        dialog.present(self)

    def run_op(self, fn, *args, success: str | None = None, then=None) -> None:
        self._begin_busy()

        def done(result):
            self._end_busy()
            if success:
                self.toast(success)
            if then:
                then(result)

        def failed(err):
            self._end_busy()
            self.toast(f"Failed: {err}", 8)

        run_async(fn, *args, on_done=done, on_error=failed)

    def _begin_busy(self) -> None:
        self._busy = getattr(self, "_busy", 0) + 1
        if self._busy == 1 and getattr(self, "_busy_source", None) is None:
            self._busy_source = GLib.timeout_add(300, self._show_busy)

    def _show_busy(self) -> bool:
        self._busy_source = None
        if getattr(self, "_busy", 0):
            self.stack.set_sensitive(False)
            self.rescan_spinner.start()
        return False

    def _end_busy(self) -> None:
        self._busy = max(0, getattr(self, "_busy", 0) - 1)
        if self._busy == 0:
            if getattr(self, "_busy_source", None) is not None:
                GLib.source_remove(self._busy_source)
                self._busy_source = None
            self.stack.set_sensitive(True)
            self.rescan_spinner.stop()

    def show_first_run(self) -> None:
        from .first_run import FirstRunDialog

        if self.get_visible_dialog() is not None:
            return
        self._first_run = FirstRunDialog(self.backend, lambda: self.start_library(rescan=False))
        self._first_run.present(self)

    def set_tiling_mode(self, on: bool) -> None:
        self.app.tiling_mode = on
        self.set_decorated(not on)
        if on:
            strip_window_drag(self)
            dialog = self.get_visible_dialog()
            if dialog is not None:
                strip_window_drag(dialog)

    def _on_visible_dialog(self, *_) -> None:
        dialog = self.get_visible_dialog()
        if dialog is not None and self.app.tiling_mode:
            strip_window_drag(dialog)

    def _on_close_request(self, *_) -> bool:
        self.closing = True
        for _ in range(8):
            dialog = self.get_visible_dialog()
            if dialog is None:
                break
            dialog.force_close()
        return False

    def sync_configured_state(self) -> None:
        configured = self.backend.lib.is_configured
        self.content_stack.set_visible_child_name("views" if configured else "setup")
        self.header.set_title_widget(self.switcher if configured else self.plain_title)

    def start_library(self, rescan: bool = True) -> None:
        self.sync_configured_state()
        if not self.backend.lib.is_configured:
            return
        self.library.refresh()
        self.content.refresh()
        self.refresh_stats()
        self._update_downloads_badge()
        run_async(self.backend.resolve_thumbnails)
        run_async(self.backend.refresh_catalog)
        run_async(self._retry_hub_details)
        self.backend.start_watcher()
        if rescan and self.backend.lib.initial_scan_done:
            self._rescan_on_start()

    def _rescan_on_start(self) -> None:

        def done(summary):
            if summary.added or summary.removed or summary.replaced:
                msg = f"Library changed since last time: {summary.added} added, {summary.removed} removed"
                if summary.replaced:
                    msg += f", {len(summary.replaced)} placeholder(s) replaced by the real package"
                self.toast(msg, 6)
                run_async(self.backend.resolve_thumbnails)
            self.refresh_stats()

        run_async(self.backend.lib.scan, on_done=done, on_error=lambda e: log.warning("startup rescan failed: %s", e))

    def _retry_hub_details(self):
        if self.backend.retry_pending_hub_details()["fetched"]:
            self.backend.resolve_thumbnails()

    def _on_view_changed(self):
        child = self.stack.get_visible_child()
        if hasattr(child, "on_shown"):
            child.on_shown()

    def rescan(self) -> None:
        if not self.backend.lib.is_configured:
            self.show_first_run()
            return
        self._begin_busy()

        def done(summary):
            self._end_busy()
            msg = f"Rescan complete: {summary.added} added, {summary.removed} removed, {summary.scanned} re-read"
            if summary.replaced:
                msg += f"; {len(summary.replaced)} placeholder(s) replaced by the real package"
            self.toast(msg)
            run_async(self.backend.resolve_thumbnails)
            run_async(self._retry_hub_details)

        def failed(err):
            self._end_busy()
            self.toast(f"Rescan failed: {err}", 8)

        run_async(self.backend.lib.scan, on_done=done, on_error=failed)

    def _on_scan_progress(self, p: dict):
        phase = p.get("phase")
        done = phase == "finalizing" and p.get("step") == p.get("total")
        self.status_progress.set_visible(not done)
        self.status_progress_label.set_visible(not done)
        if done:
            return
        total = p.get("total") or 0
        if total:
            self.status_progress.set_fraction(p.get("step", 0) / total)
        else:
            self.status_progress.pulse()
        self.status_progress_label.set_text(f"{phase}: {str(p.get('message') or '')[:50]}")

    def show_progress(self, text: str, fraction: float | None) -> None:
        self.status_progress.set_visible(True)
        self.status_progress_label.set_visible(True)
        if fraction is None:
            self.status_progress.pulse()
        else:
            self.status_progress.set_fraction(max(0.0, min(1.0, fraction)))
        self.status_progress_label.set_text(text[:80])

    def hide_progress(self) -> None:
        self.status_progress.set_visible(False)
        self.status_progress_label.set_visible(False)

    def verify_integrity(self) -> None:
        if self._integrity_cancel is not None:
            self.toast("An integrity check is already running")
            return
        if not self.backend.lib.is_configured:
            return
        self._integrity_cancel = threading.Event()
        self._begin_busy()
        self.status_stop.set_visible(True)
        self.show_progress("Verifying packages…", 0.0)
        run_async(
            self.backend.lib.integrity_check,
            lambda p: on_main(self._integrity_progress, p),
            self._integrity_cancel,
            on_done=self._integrity_done,
            on_error=self._integrity_failed,
        )

    def _integrity_progress(self, p: dict) -> None:
        if p.get("status") != "checking":
            return
        total = p.get("total") or 0
        self.show_progress(
            f"Verifying {p['step']}/{total}: {p.get('filename', '')}", (p["step"] / total) if total else None
        )

    def _integrity_finish(self) -> None:
        self._integrity_cancel = None
        self._end_busy()
        self.status_stop.set_visible(False)
        self.hide_progress()

    def _integrity_failed(self, err) -> None:
        self._integrity_finish()
        self.toast(f"Integrity check failed: {err}", 8)

    def _integrity_done(self, result: dict) -> None:
        self._integrity_finish()
        bad = result["corrupted_files"]
        secs = result["seconds"]
        took = f"{secs:.1f} s" if secs < 10 else f"{secs:.0f} s" if secs < 90 else f"{secs / 60:.1f} min"
        if result["cancelled"]:
            heading = "Integrity check stopped"
        elif bad:
            heading = f"{len(bad)} corrupted package{'s' if len(bad) != 1 else ''}"
        else:
            heading = "All packages verified"
        body = (
            f"{result['checked']} of {result['total']} packages checked ({human_size(result['bytes'])}) in {took}: "
            "every entry decompressed and its CRC32 compared."
        )
        if result.get("unpacked"):
            body += f" {len(result['unpacked'])} unpacked folder(s) have no checksums to verify."
        dialog = Adw.AlertDialog(heading=heading, body=body)
        if bad:
            text = "\n".join(f"{fn}  ({result['errors'].get(fn, '')[:60]})" for fn in bad)
            lbl = Gtk.Label(label=text, xalign=0, selectable=True, wrap=True)
            lbl.add_css_class("vb-mono")
            scroller = Gtk.ScrolledWindow(
                child=lbl, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=260
            )
            dialog.set_extra_child(scroller)
            dialog.add_response("show", "Show in library")
            with self.backend.lib.lock:
                store = self.backend.lib.store
                redownload = [
                    fn
                    for fn in bad
                    if (store.package_index.get(fn) or {}).get("hub_resource_id") or fn in store.replaceable_set
                ]
            if redownload:
                dialog.add_response("redownload", f"Re-download {len(redownload)} from the Hub")
                dialog.set_response_appearance("redownload", Adw.ResponseAppearance.SUGGESTED)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")

        def on_response(_d, r):
            if r == "show":
                self._show_corrupted()
            elif r == "redownload":
                self.run_op(
                    self.backend.downloads.enqueue_reinstall_many,
                    redownload,
                    then=lambda res: self.toast(
                        f"Queued {len(res['queued'])} re-download(s)"
                        + (f", {len(res['unavailable'])} not on the Hub" if res["unavailable"] else ""),
                        6,
                    ),
                )

        dialog.connect("response", on_response)
        dialog.present(self)

    def check_textures(self, filenames: list[str] | None = None, include_deps: bool = False) -> None:
        if self._integrity_cancel is not None:
            self.toast("A check is already running")
            return
        if not self.backend.lib.is_configured:
            return
        self._integrity_cancel = threading.Event()
        self._begin_busy()
        self.status_stop.set_visible(True)
        self.show_progress("Checking textures…", 0.0)
        log.info("texture check started (%s)", f"{len(filenames)} package(s)" if filenames else "whole library")
        run_async(
            self.backend.lib.texture_check,
            lambda p: on_main(self._texture_progress, p),
            self._integrity_cancel,
            filenames,
            include_deps,
            on_done=lambda r: self._texture_done(r, filenames, include_deps),
            on_error=self._texture_failed,
        )

    def _texture_progress(self, p: dict) -> None:
        if p.get("status") != "checking":
            return
        total = p.get("total") or 0
        found = f", {p['problems']} problem(s) so far" if p.get("problems") else ""
        name = LOCAL_LABEL if is_local_package(p.get("filename")) else p.get("filename", "")
        if not p.get("step"):
            name = f"reading {total} package(s)…"
        self.show_progress(
            f"Checking textures {p['step']}/{total}{found}: {name}", (p["step"] / total) if total else None
        )

    def _texture_failed(self, err) -> None:
        self._integrity_finish()
        self.toast(f"Texture check failed: {err}", 8)

    def _texture_done(self, result: dict, filenames: list[str] | None, include_deps: bool) -> None:
        self._integrity_finish()
        log.info(
            "texture check done: %d of %d packages, %d images, %d problem(s), cancelled=%s",
            result.get("checked", 0),
            result.get("total", 0),
            result.get("images", 0),
            len(result.get("problems") or []),
            result.get("cancelled"),
        )
        try:
            self._texture_report(result, filenames, include_deps)
        except Exception as err:
            log.exception("texture report failed")
            n = len(result.get("problems") or [])
            self.toast(f"Texture check finished ({n} problem(s)) but the report could not be shown: {err}", 10)

    def _texture_report(self, result: dict, filenames: list[str] | None, include_deps: bool) -> None:
        problems = result["problems"]
        self.library.set_texture_problems(result, replace_all=filenames is None)
        errors, warnings = result["error_count"], result["warning_count"]
        secs = result["seconds"]
        took = f"{secs:.1f} s" if secs < 10 else f"{secs:.0f} s" if secs < 90 else f"{secs / 60:.1f} min"
        if result["cancelled"]:
            heading = "Texture check stopped"
        elif errors:
            heading = f"{errors} image{'s' if errors != 1 else ''} VaM cannot load" + (
                f", {warnings} suspect" if warnings else ""
            )
        elif warnings:
            heading = f"{warnings} suspect image{'s' if warnings != 1 else ''}"
        else:
            heading = "Every image can be decoded"
        scope = (
            " (the package and its dependencies)"
            if filenames and include_deps
            else (" (the package)" if filenames else "")
        )
        body = (
            f"{result['images']} images ({human_size(result['bytes'])}) in {result['checked']} of {result['total']} "
            f"packages{scope} read in {took}: each file's data checked against what VaM's decoder (GDI+) accepts, "
            "which the integrity check cannot tell."
        )
        if result["errors"]:
            body += f" {len(result['errors'])} package(s) could not be opened: " + ", ".join(list(result["errors"])[:3])
        dialog = Adw.AlertDialog(heading=heading, body=body)
        if problems:
            lines = []
            for p in problems:
                mark = "✗" if p["severity"] == "error" else "!"
                owner = LOCAL_LABEL if is_local_package(p["filename"]) else p["filename"]
                lines.append(f"{mark} {owner}  ›  {p['path']}\n      {p['message']}")
            lbl = Gtk.Label(label="\n".join(lines), xalign=0, selectable=True, wrap=True)
            lbl.add_css_class("vb-mono")
            scroller = Gtk.ScrolledWindow(
                child=lbl, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=320
            )
            scroller.set_size_request(620, -1)
            dialog.set_extra_child(scroller)
            dialog.add_response("show", "Show in library")
            body += " Show in library lists them under the Texture problems filter, each with its files."
            dialog.set_body(body)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")

        def on_response(_d, r):
            if r == "show":
                self.stack.set_visible_child_name("library")
                self.library.show_texture_problems()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_corrupted(self) -> None:
        self.stack.set_visible_child_name("library")
        self.library.show_status("corrupted")

    def refresh_stats(self) -> None:
        if not self.backend.lib.is_configured:
            return

        def done(s):
            self.status_counts.set_text(
                f"{s['total_count']} packages · {s['direct_count']} direct · {s['dep_count']} dependencies · "
                f"{s['total_content']} items · {human_size(s['total_size'])}"
                + (f" · {s['missing_dep_count']} missing refs" if s["missing_dep_count"] else "")
            )

        run_async(self.backend.lib.stats, on_done=done)

    def _update_downloads_badge(self):
        def done(items):
            active = sum(1 for d in items if d["status"] in ("active", "queued"))
            failed = sum(1 for d in items if d["status"] == "failed")
            self.downloads_page.set_badge_number(active)
            self.downloads_page.set_needs_attention(failed > 0)

        run_async(self.backend.downloads.list, on_done=done)

    def open_settings(self) -> None:
        from .settings_view import SettingsDialog

        if self._settings_dialog is not None:
            self._settings_dialog.present(self)
            return
        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.connect("closed", lambda *_: setattr(self, "_settings_dialog", None))
        self._settings_dialog.present(self)

    def show_about(self) -> None:
        from .. import __version__

        if self._about_dialog is not None:
            self._about_dialog.present(self)
            return
        about = Adw.AboutDialog(
            application_name="VaM Backstage",
            application_icon=APP_ID,
            version=__version__,
            developer_name="dancingmirrors",
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/dancingmirrors/vam-backstage",
            comments="Manage your Virt-a-Mate library: dependencies, content visibility and Hub installs.",
        )
        about.connect("closed", lambda *_: setattr(self, "_about_dialog", None))
        self._about_dialog = about
        about.present(self)

    def select_package(self, filename: str) -> None:
        self.stack.set_visible_child_name("library")
        self.library.select_filename(filename)

    def show_content_for_package(self, filename: str) -> None:
        self.stack.set_visible_child_name("content")
        self.content.filter_package(filename)

    def show_content_item(self, package_filename: str, internal_path: str) -> None:
        self.stack.set_visible_child_name("content")
        self.content.filter_package(package_filename)
        self.content.select_item(package_filename, internal_path)

    def search_hub(self, text: str) -> None:
        self.stack.set_visible_child_name("hub")
        self.hub.search_for(text)

    def save_screenshot(self, path: str) -> None:
        paintable = Gtk.WidgetPaintable.new(self)
        w, h = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        if node is None:
            raise RuntimeError("nothing to render")
        texture = self.get_native().get_renderer().render_texture(node, None)
        texture.save_to_png(path)
        log.info("screenshot saved to %s", path)
