from __future__ import annotations

import logging
import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..core.paths import ADDON_PACKAGES
from ..core.scanner import detect_vam_dir, quick_count_vars, resolve_vam_dir
from .dispatch import on_main, run_async
from .widgets import hbox, label, vbox

log = logging.getLogger(__name__)

SCAN_PHASE_WEIGHT = {"indexing": 0.1, "reading": 0.55, "graph": 0.05, "local": 0.1, "finalizing": 0.05}
HUB_WEIGHT = 0.15


class FirstRunDialog(Adw.Dialog):
    def __init__(self, backend, on_finished):
        super().__init__(title="Welcome to VaM Backstage", content_width=520, content_height=460)
        self.backend = backend
        self.on_finished = on_finished
        self._finished = False
        self._dismissed = False
        self.connect("closed", self._on_closed)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=True, show_start_title_buttons=False))
        toolbar.set_content(self.stack)
        self.set_child(toolbar)
        self._detected: dict | None = None
        self._var_count = 0
        self._scan_summary = None
        self._build_welcome()
        self._build_scanning()
        self._build_setup()
        self._build_done()
        self.stack.set_visible_child_name("welcome")
        self._detect()

    def _page(self, name: str) -> Gtk.Box:
        page = vbox(12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(28)
        page.set_margin_end(28)
        self.stack.add_named(page, name)
        return page

    def _build_welcome(self):
        page = self._page("welcome")
        page.append(label("Your VaM library", "title-2", wrap=True))
        page.append(
            label(
                "Backstage indexes the packages in your Virt-a-Mate folder and tells apart the ones you installed "
                "from the ones that are only there as dependencies. Nothing is modified until you ask.",
                "vb-dim",
                wrap=True,
            )
        )
        self.path_row = Adw.ActionRow(title="VaM folder", subtitle="Looking for AddonPackages…")
        self.path_row.set_use_markup(False)
        self.path_row.add_css_class("property")
        browse = Gtk.Button(label="Choose…")
        browse.set_valign(Gtk.Align.CENTER)
        browse.connect("clicked", self._browse)
        self.path_row.add_suffix(browse)
        group = Adw.PreferencesGroup()
        group.add(self.path_row)
        page.append(group)
        self.count_label = label("", "vb-dim", wrap=True)
        page.append(self.count_label)
        self.problem_label = label("", "warning", wrap=True)
        self.problem_label.set_visible(False)
        page.append(self.problem_label)
        spacer = Gtk.Box(vexpand=True)
        page.append(spacer)
        self.scan_button = Gtk.Button(label="Scan library")
        self.scan_button.add_css_class("suggested-action")
        self.scan_button.add_css_class("pill")
        self.scan_button.set_halign(Gtk.Align.END)
        self.scan_button.set_sensitive(False)
        self.scan_button.connect("clicked", lambda *_: self._start_scan())
        page.append(self.scan_button)

    def _detect(self):
        current = self.backend.lib.vam_dir

        def work():
            if current and os.path.isdir(os.path.join(current, ADDON_PACKAGES)):
                return {"path": current, "source": "configured"}
            return detect_vam_dir()

        run_async(work, on_done=self._detected_done)

    def _detected_done(self, found):
        self._detected = found
        if getattr(self, "_path", None):
            return
        if found:
            self._set_path(found["path"])
        else:
            self.path_row.set_subtitle("Not found automatically. Choose the folder that contains AddonPackages.")

    def _set_path(self, path: str):
        self._path = path
        self.path_row.set_subtitle(path)
        self.problem_label.set_visible(False)
        self.count_label.set_text("Counting packages…")
        self.scan_button.set_sensitive(True)

        def count():
            return quick_count_vars(path)

        def done(n):
            self._var_count = n
            self.count_label.set_text(
                f"{n} .var files found in AddonPackages"
                if n
                else "No .var files yet — that's fine, you can install from the Hub."
            )

        def failed(err):
            self.count_label.set_text(f"Could not count packages: {err}")

        run_async(count, on_done=done, on_error=failed)

    def _problem(self, text: str):
        self.problem_label.set_text(text)
        self.problem_label.set_visible(True)
        self.scan_button.set_sensitive(False)

    def _browse(self, *_):
        dialog = Gtk.FileDialog(title="Select your VaM installation folder")
        if getattr(self, "_path", None):
            dialog.set_initial_folder(Gio.File.new_for_path(self._path))

        def finish(d, result):
            try:
                f = d.select_folder_finish(result)
            except GLib.Error as err:
                if err.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    return
                log.warning("folder dialog failed: %s", err)
                self._problem(f"Could not open the folder chooser: {err.message}")
                return
            picked = f.get_path()
            if not picked:
                self._problem(f"That location is not a local folder: {f.get_uri()}")
                return
            resolved = resolve_vam_dir(picked)
            log.info("chosen %s -> VaM folder %s", picked, resolved)
            if not resolved:
                self._problem(
                    f"No {ADDON_PACKAGES} folder found at {picked} or above it. "
                    f"Pick the VaM folder (the one containing {ADDON_PACKAGES}) or the {ADDON_PACKAGES} folder itself."
                )
                return
            self._set_path(resolved)

        dialog.select_folder(self.get_root(), None, finish)

    def _build_scanning(self):
        page = self._page("scanning")
        page.append(label("Scanning your library", "title-2"))
        self.progress = Gtk.ProgressBar(show_text=False)
        self.progress.set_margin_top(12)
        page.append(self.progress)
        self.progress_label = label("Starting…", "vb-dim")
        page.append(self.progress_label)
        self.progress_detail = label("", "vb-mono vb-dim")
        page.append(self.progress_detail)
        self.retry_box = hbox(8)
        self.retry_box.set_halign(Gtk.Align.END)
        self.retry_box.set_margin_top(12)
        self.retry_box.set_visible(False)
        other = Gtk.Button(label="Choose another folder")
        other.add_css_class("pill")
        other.connect("clicked", lambda *_: self.stack.set_visible_child_name("welcome"))
        self.retry_box.append(other)
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("suggested-action")
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self._start_scan())
        self.retry_box.append(retry)
        page.append(self.retry_box)

    def _start_scan(self):
        self.backend.lib.set_vam_dir(self._path)
        self.stack.set_visible_child_name("scanning")
        self.retry_box.set_visible(False)
        self.progress.set_fraction(0)
        self.progress_label.set_text("Starting…")
        self.progress_detail.set_text("")
        self._phase_done = 0.0

        def progress(p):
            on_main(self._on_progress, p)

        def work():
            summary = self.backend.lib.scan(progress)
            try:
                self.backend.enrich_hub(lambda d: on_main(self._on_hub_progress, d))
            except Exception as err:
                log.warning("Hub enrichment failed: %s", err)
            return summary

        run_async(work, on_done=self._scan_done, on_error=self._scan_failed)

    def _on_progress(self, p: dict):
        phase = p.get("phase", "")
        base = 0.0
        for name, w in SCAN_PHASE_WEIGHT.items():
            if name == phase:
                frac = (p["step"] / p["total"]) if p.get("total") else 0.0
                self.progress.set_fraction(min(1.0, base + w * frac) * (1 - HUB_WEIGHT))
                break
            base += w
        names = {
            "indexing": "Finding packages",
            "reading": "Reading packages",
            "graph": "Building dependency graph",
            "local": "Indexing loose content",
            "finalizing": "Finalizing",
        }
        self.progress_label.set_text(names.get(phase, phase))
        self.progress_detail.set_text(str(p.get("message") or "")[:70])

    def _on_hub_progress(self, d: dict):
        total = d.get("total") or 0
        frac = (d.get("current", 0) / total) if total else 1.0
        self.progress.set_fraction((1 - HUB_WEIGHT) + HUB_WEIGHT * min(1.0, frac))
        self.progress_label.set_text(
            "Fetching Hub metadata" if d.get("phase") == "fetching" else "Matching packages with the Hub"
        )
        self.progress_detail.set_text(f"{d.get('current', 0)}/{total}" if total else "")

    def _window_closing(self) -> bool:
        root = self.get_root()
        return bool(root is not None and getattr(root, "closing", False))

    def _scan_failed(self, err):
        log.warning("first-run scan failed: %s", err)
        self.progress_label.set_text(f"Scan failed: {err}")
        self.progress_detail.set_text("")
        self.retry_box.set_visible(True)
        if self._dismissed and not self._window_closing():
            self.on_finished()

    def _scan_done(self, summary):
        self._scan_summary = summary
        if self._dismissed:
            if not self._window_closing():
                self.on_finished()
            return
        s = self.backend.lib.stats()
        if s["total_count"] == 0:
            self.backend.lib.set_setting("auto_hide_deps", "1")
            self._show_done()
            return
        self.setup_summary.set_text(
            f"Found {s['total_count']} packages — {s['direct_count']} installed directly, {s['dep_count']} dependencies."
        )
        if summary.unreadable:
            self.setup_unreadable.set_text(f"{len(summary.unreadable)} file(s) could not be read and were skipped.")
            self.setup_unreadable.set_visible(True)
        self.stack.set_visible_child_name("setup")

    def _build_setup(self):
        page = self._page("setup")
        page.append(label("Dependency content", "title-2"))
        self.setup_summary = label("", "vb-dim", wrap=True)
        page.append(self.setup_summary)
        self.setup_unreadable = label("", "vb-dim", wrap=True)
        self.setup_unreadable.set_visible(False)
        page.append(self.setup_unreadable)
        group = Adw.PreferencesGroup()
        self.hide_choice = Gtk.CheckButton()
        hide_row = Adw.ActionRow(
            title="Hide dependency content (recommended)",
            subtitle="Scenes, looks, clothing and hair that only came along as dependencies are hidden from VaM's content browser. Packages stay installed and working.",
        )
        hide_row.add_prefix(self.hide_choice)
        hide_row.set_activatable_widget(self.hide_choice)
        keep = Gtk.CheckButton(group=self.hide_choice)
        keep_row = Adw.ActionRow(title="Keep everything visible", subtitle="Change this later in Settings.")
        keep_row.add_prefix(keep)
        keep_row.set_activatable_widget(keep)
        self.hide_choice.set_active(True)
        group.add(hide_row)
        group.add(keep_row)
        page.append(group)
        page.append(Gtk.Box(vexpand=True))
        btn = Gtk.Button(label="Continue")
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.END)
        btn.connect("clicked", lambda *_: self._apply_setup())
        page.append(btn)

    def _apply_setup(self):
        hide = self.hide_choice.get_active()
        self.backend.lib.set_setting("auto_hide_deps", "1" if hide else "0")
        if not hide:
            self._show_done()
            return
        self.stack.set_visible_child_name("scanning")
        self.progress_label.set_text("Hiding dependency content")
        self.progress_detail.set_text("")
        self.progress.set_fraction(0)

        def progress(d):
            on_main(
                lambda: (
                    self.progress.set_fraction((d["current"] / d["total"]) if d.get("total") else 1.0),
                    self.progress_detail.set_text(d.get("filename") or ""),
                )
            )

        run_async(
            lambda: self.backend.lib.apply_auto_hide_rule("deps", progress),
            on_done=lambda _: self._show_done(),
            on_error=lambda e: self._show_done(),
        )

    def _build_done(self):
        page = self._page("done")
        page.append(label("All set", "title-2"))
        self.done_summary = label("", "vb-dim", wrap=True)
        page.append(self.done_summary)
        page.append(Gtk.Box(vexpand=True))
        btn = Gtk.Button(label="Open VaM Backstage")
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.END)
        btn.connect("clicked", lambda *_: self._finish())
        page.append(btn)

    def _show_done(self):
        s = self.backend.lib.stats()
        self.done_summary.set_text(
            f"{s['total_count']} packages indexed ({s['direct_count']} direct, {s['dep_count']} dependencies), "
            f"{s['total_content']} content items. Missing dependency references: {s['missing_dep_count']}."
        )
        self.stack.set_visible_child_name("done")

    def _finish(self):
        self._finished = True
        self.close()
        self.on_finished()

    def _on_closed(self, *_):
        if self._finished:
            return
        self._dismissed = True
        if self._window_closing():
            return
        self.on_finished()
