from __future__ import annotations

import logging
import os

from gi.repository import Adw, Gio, GLib, Gtk

from ..core.paths import ADDON_PACKAGES
from ..core.scanner import resolve_vam_dir
from ..core.storage_state import disable_behavior_move_to, parse_disable_behavior
from . import hubweb
from .dispatch import run_async
from .launch import open_in_file_manager
from .models import human_size

log = logging.getLogger(__name__)

AUTO_HIDE_RULES = [
    (
        "auto_hide_deps",
        "deps",
        "Hide dependency content",
        "Scenes, looks, poses, clothing and hair from packages that are only installed as dependencies stay out of VaM's browser.",
    ),
    (
        "auto_hide_foreign_hair",
        "foreign_hair",
        "Hide stray hair",
        "Hair shipped inside packages that are not hair packages.",
    ),
    (
        "auto_hide_foreign_poses",
        "foreign_poses",
        "Hide stray poses",
        "Poses shipped inside packages that are not pose packages.",
    ),
    (
        "auto_hide_foreign_clothing",
        "foreign_clothing",
        "Hide stray clothing",
        "Clothing shipped inside packages that are not clothing packages.",
    ),
]


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(title="Settings", content_width=720, content_height=640)
        self.window = window
        self.backend = window.backend
        self.lib = window.backend.lib
        self.add(self._general_page())
        self.add(self._visibility_page())
        self.add(self._folders_page())
        self.add(self._maintenance_page())

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", name="general", icon_name="preferences-system-symbolic")
        group = Adw.PreferencesGroup(title="VaM installation")
        self.vam_row = Adw.ActionRow(title="VaM folder", subtitle=self.lib.vam_dir or "Not configured")
        self.vam_row.set_use_markup(False)
        self.vam_row.add_css_class("property")
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", self._change_vam_dir)
        self.vam_row.add_suffix(change)
        group.add(self.vam_row)
        page.add(group)

        look = Adw.PreferencesGroup(title="Appearance")
        schemes = ["dark", "light", "system"]
        row = Adw.ComboRow(title="Color scheme", model=Gtk.StringList.new(["Dark", "Light", "Follow system"]))
        row.set_selected(schemes.index(self.lib.get_setting("color_scheme") or "dark"))

        def on_scheme(r, _p):
            value = schemes[r.get_selected()]
            self.lib.set_setting("color_scheme", value)
            Adw.StyleManager.get_default().set_color_scheme(
                {"dark": Adw.ColorScheme.FORCE_DARK, "light": Adw.ColorScheme.FORCE_LIGHT}.get(
                    value, Adw.ColorScheme.DEFAULT
                )
            )

        row.connect("notify::selected", on_scheme)
        look.add(row)
        page.add(look)

        hub = Adw.PreferencesGroup(title="Hub")
        in_app = Adw.SwitchRow(title="Open Hub pages inside VaM Backstage")
        if hubweb.available():
            in_app.set_subtitle(
                "Resource pages open in a built-in WebKit view, and their Download buttons queue the package here. "
                "Off: pages open in your web browser, which is also the fix if the built-in view stays blank or crashes."
            )
            in_app.set_active(hubweb.enabled(self.lib))
            in_app.connect(
                "notify::active", lambda r, _p: self.lib.set_setting(hubweb.SETTING_KEY, "1" if r.get_active() else "0")
            )
        else:
            in_app.set_subtitle(hubweb.missing_hint())
            in_app.set_active(False)
            in_app.set_sensitive(False)
        hub.add(in_app)
        page.add(hub)

        lib_group = Adw.PreferencesGroup(title="Library")
        watch = Adw.SwitchRow(
            title="Watch the library folders",
            subtitle="Pick up packages added, removed or replaced outside Backstage within a few seconds, "
            "once a copy has finished. Off: press F5 to rescan.",
        )
        watch.set_active(self.lib.get_setting("watch_library") != "0")

        def on_watch(r, _p):
            self.lib.set_setting("watch_library", "1" if r.get_active() else "0")
            if r.get_active():
                self.backend.start_watcher()
            else:
                self.backend.stop_watcher()

        watch.connect("notify::active", on_watch)
        lib_group.add(watch)
        page.add(lib_group)

        deps = Adw.PreferencesGroup(title="Dependencies")
        strict = Adw.SwitchRow(
            title="Exact versions, like VaM",
            subtitle="A dependency on a specific version counts as missing unless that exact version is installed, "
            "even when another version is. Off: any installed version satisfies it.",
        )
        strict.set_active(self.lib.get_setting("strict_versions") != "0")
        strict.connect("notify::active", lambda r, _p: self.window.run_op(self.lib.set_strict_versions, r.get_active()))
        deps.add(strict)
        page.add(deps)

        win = Adw.PreferencesGroup(title="Window")
        tiling = Adw.SwitchRow(
            title="Tiling window manager mode",
            subtitle="No window frame or resize borders, and dragging a header bar never asks the compositor "
            "to move the window. Turning it off again takes effect after a restart.",
        )
        tiling.set_active(self.lib.get_setting("tiling_wm") == "1")

        def on_tiling(r, _p):
            on = r.get_active()
            self.lib.set_setting("tiling_wm", "1" if on else "0")
            if on:
                self.window.set_tiling_mode(True)
            else:
                self.window.app.tiling_mode = False
                self.window.set_decorated(True)

        tiling.connect("notify::active", on_tiling)
        win.add(tiling)
        page.add(win)
        return page

    def _change_vam_dir(self, *_):
        dialog = Gtk.FileDialog(title="Select your VaM installation folder")
        if self.lib.vam_dir:
            dialog.set_initial_folder(Gio.File.new_for_path(self.lib.vam_dir))

        def finish(d, result):
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            path = resolve_vam_dir(f.get_path() or "")
            if not path:
                self.window.toast(
                    f"No {ADDON_PACKAGES} folder found there. Pick the VaM folder or {ADDON_PACKAGES} itself."
                )
                return
            if path == self.lib.vam_dir:
                return
            self.backend.stop_watcher()
            self.lib.set_vam_dir(path)
            self.lib.set_setting("initial_scan_done", "0")
            self.vam_row.set_subtitle(path)
            self.window.sync_configured_state()
            self.connect("closed", lambda *_: self.window.show_first_run())
            self.close()

        dialog.select_folder(self.window, None, finish)

    def _visibility_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Visibility", name="visibility", icon_name="view-conceal-symbolic")
        group = Adw.PreferencesGroup(
            title="Content hidden from VaM's browser",
            description="Backstage writes the .hide marker files VaM itself reads. Packages are never modified.",
        )
        for key, rule_id, title, subtitle in AUTO_HIDE_RULES:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(self.lib.get_setting(key) == "1")
            row.connect("notify::active", self._on_rule_toggled, key, rule_id)
            group.add(row)
        page.add(group)
        return page

    def _on_rule_toggled(self, row, _pspec, key: str, rule_id: str):
        on = row.get_active()
        if (self.lib.get_setting(key) == "1") == on:
            return
        self.lib.set_setting(key, "1" if on else "0")
        dialog = Adw.AlertDialog(
            heading="Apply to existing content?",
            body=(
                "Hide the matching content that is currently visible in VaM?"
                if on
                else "Show the content this rule had hidden (unless another rule still hides it)?"
            ),
        )
        dialog.add_response("later", "Only for new packages")
        dialog.add_response("apply", "Apply now")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("later")

        def response(_d, r):
            if r != "apply":
                return
            fn = self.lib.apply_auto_hide_rule if on else self.lib.remove_auto_hide_rule
            self.window.run_op(fn, rule_id, success="Content visibility updated")

        dialog.connect("response", response)
        dialog.present(self)

    def _folders_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Folders", name="folders", icon_name="folder-symbolic")
        self.folders_group = Adw.PreferencesGroup(
            title="Offload and archive folders",
            description="Extra folders on the same drive as AddonPackages. Disabled packages can be moved to an offload folder "
            "(VaM loads faster), and an archive folder is cold storage whose packages never demand their dependencies.",
        )
        page.add(self.folders_group)
        self.behavior_group = Adw.PreferencesGroup(title="Disabling packages")
        page.add(self.behavior_group)
        self._folder_rows: list = []
        self._render_folders()
        return page

    def _render_folders(self):
        for r in self._folder_rows:
            self.folders_group.remove(r)
        self._folder_rows = []
        listing = self.lib.list_library_dirs()
        main_row = Adw.ActionRow(
            title="AddonPackages (main)",
            subtitle=f"{listing['main']['path']} · {listing['main']['package_count']} packages · {human_size(listing['main']['bytes'])}",
        )
        main_row.set_use_markup(False)
        main_row.add_css_class("property")
        open_main = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        open_main.set_valign(Gtk.Align.CENTER)
        open_main.add_css_class("flat")
        open_main.set_tooltip_text("Show in file manager")
        open_main.connect("clicked", lambda *_: open_in_file_manager(listing["main"]["path"]))
        main_row.add_suffix(open_main)
        self.folders_group.add(main_row)
        self._folder_rows.append(main_row)
        for d in listing["aux"]:
            row = Adw.ActionRow(
                title=os.path.basename(d["path"].rstrip("/\\")) or d["path"],
                subtitle=f"{d['path']} · {d['package_count']} packages · {human_size(d['bytes'])}"
                + ("" if d["exists"] else " · folder missing"),
            )
            row.set_use_markup(False)
            role = Gtk.Switch(
                valign=Gtk.Align.CENTER,
                active=d["archive"],
                tooltip_text="Archive role: cold storage, no dependency demands",
            )
            role.connect("notify::active", self._on_role, d["id"])
            row.add_suffix(Gtk.Label(label="Archive", css_classes=["vb-dim", "vb-small"]))
            row.add_suffix(role)
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            remove.set_valign(Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text("Unregister folder")
            remove.connect("clicked", self._remove_folder, d)
            row.add_suffix(remove)
            self.folders_group.add(row)
            self._folder_rows.append(row)
        add_row = Adw.ActionRow(
            title="Add an offload or archive folder…",
            activatable=True,
            subtitle="An empty folder on the same drive as AddonPackages. Its role is chosen afterwards.",
        )
        add_row.add_suffix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_row.connect("activated", self._add_folder)
        self.folders_group.add(add_row)
        self._folder_rows.append(add_row)
        for r in getattr(self, "_behavior_rows", []):
            self.behavior_group.remove(r)
        offload = [d for d in listing["aux"] if not d["archive"]]
        if not offload:
            fixed = Adw.ActionRow(
                title="Disabled packages stay in place, marked with an empty .var.disabled file (VaM-native)",
                subtitle="Add an offload folder above to get the option of moving them there instead.",
            )
            fixed.add_css_class("property")
            self.behavior_group.add(fixed)
            self._behavior_rows = [fixed]
            return

        def folder_name(d: dict) -> str:
            return os.path.basename(d["path"].rstrip("/\\")) or d["path"]

        options = ["Keep in place (.var.disabled marker)"] + [f"Move to {folder_name(d)}" for d in offload]
        ids = [None] + [d["id"] for d in offload]
        combo = Adw.ComboRow(title="When a package is disabled", model=Gtk.StringList.new(options))
        current = parse_disable_behavior(self.lib.get_setting("disable_behavior"))
        combo.set_selected(
            ids.index(current.aux_dir_id) if current.kind == "move-to" and current.aux_dir_id in ids else 0
        )

        def on_combo(r, _p):
            value = ids[r.get_selected()]
            self.lib.set_setting("disable_behavior", "suffix" if value is None else disable_behavior_move_to(value))

        combo.connect("notify::selected", on_combo)
        self.behavior_group.add(combo)
        self._behavior_rows = [combo]

    def _add_folder(self, *_):
        dialog = Gtk.FileDialog(title="Choose a folder on the same drive as AddonPackages")

        def finish(d, result):
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            if not f.get_path():
                self.window.toast("That location is not a local folder.")
                return
            try:
                self.lib.add_library_dir(f.get_path())
            except Exception as err:
                self.window.toast(str(err), 8)
                return
            self._render_folders()
            self.window.rescan()

        dialog.select_folder(self.window, None, finish)

    def _on_role(self, switch, _pspec, dir_id: int):
        archive = switch.get_active()

        def done(_n):
            self._render_folders()

        run_async(
            self.lib.set_library_dir_role,
            dir_id,
            archive,
            on_done=done,
            on_error=lambda e: self.window.toast(str(e), 8),
        )

    def _remove_folder(self, _btn, d: dict):
        def do_remove(force: bool):
            run_async(
                lambda: self.lib.remove_library_dir(d["id"], force=force),
                on_done=lambda _r: self._render_folders(),
                on_error=lambda e: self.window.toast(str(e), 8),
            )

        if d["package_count"]:
            self.window.confirm(
                "Folder still holds packages",
                f"{d['package_count']} package(s) live in {d['path']}. Unregistering keeps the files on disk but forgets them "
                "until the folder is added again (their settings are preserved).",
                "Unregister anyway",
                lambda: do_remove(True),
            )
        else:
            do_remove(False)

    def _maintenance_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Maintenance", name="maintenance", icon_name="emblem-system-symbolic")
        group = Adw.PreferencesGroup(title="Library")
        group.add(
            self._action_row(
                "Rescan library",
                "Index packages added or removed outside Backstage.",
                "Rescan",
                lambda: self.window.rescan(),
            )
        )
        group.add(
            self._action_row(
                "Verify package integrity",
                "Decompress every package and compare its CRC32 checksums, like `7z t`. "
                "Progress shows in the status bar; corrupted packages are listed at the end.",
                "Verify",
                self._integrity,
            )
        )
        group.add(
            self._action_row(
                "Check textures",
                "Read every image in every package and list the ones VaM's decoder (GDI+) cannot read: another "
                "format behind a .jpg or .png name, empty or truncated files, JPEG flavors it rejects. "
                "The integrity check cannot tell; those files are exactly what their authors packed.",
                "Check",
                self._textures,
            )
        )
        group.add(
            self._action_row(
                "Re-match packages with the Hub",
                "Refresh Hub metadata and thumbnails for every package.",
                "Refresh",
                self._enrich,
            )
        )
        self.placeholder_row = self._action_row("Stand-in packages", "Counting…", "Show", self._show_placeholders)
        group.add(self.placeholder_row)
        run_async(self.lib.placeholder_counts, on_done=self._placeholder_counts_done)
        n_ignored = len(self.backend.ignored_updates())
        self.ignored_row = self._action_row(
            "Ignored updates",
            f"{n_ignored} update(s) hidden from the updates list at your request.",
            "Forget",
            self._forget_ignored,
        )
        group.add(self.ignored_row)
        page.add(group)
        dev = Adw.PreferencesGroup(title="Data")
        db_row = Adw.ActionRow(title="Database", subtitle=self.lib.db.path)
        db_row.set_use_markup(False)
        db_row.add_css_class("property")
        dev.add(db_row)
        dev.add(
            self._action_row(
                "Forget deleted packages",
                "Drop remembered settings of packages whose files are gone.",
                "Forget",
                self._forget,
            )
        )
        page.add(dev)
        return page

    def _action_row(self, title, subtitle, button, cb) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        btn = Gtk.Button(label=button, valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: cb())
        row.add_suffix(btn)
        return row

    def _placeholder_counts_done(self, c: dict) -> None:
        self.placeholder_row.set_subtitle(
            f"{c['dummies']} dummy package(s) and {c['aliases']} alias(es) stand in for packages you do not have. "
            "Listed in the library under Dummy packages and Aliases, in case you find the real ones later."
        )

    def _forget_ignored(self):
        n = self.backend.clear_ignored_updates()
        self.ignored_row.set_subtitle("0 update(s) hidden from the updates list at your request.")
        self.window.toast(f"Forgot {n} ignored update(s); check for updates again to see them")

    def _show_placeholders(self):
        self.close()
        self.window.stack.set_visible_child_name("library")
        self.window.library.show_status("dummy")

    def _after_close(self, fn) -> None:
        handler = None

        def closed(*_):
            self.disconnect(handler)
            fn()

        handler = self.connect("closed", closed)
        self.close()

    def _integrity(self):
        self._after_close(self.window.verify_integrity)

    def _textures(self):
        self._after_close(self.window.check_textures)

    def _enrich(self):
        self.window.toast("Matching packages with the Hub…")
        self.window.run_op(
            self.backend.enrich_hub,
            then=lambda r: self.window.toast(f"{r['found']} of {r['total']} packages matched on the Hub"),
        )

    def _forget(self):
        n = self.lib.db.count_missing_packages()
        self.window.confirm(
            "Forget deleted packages?",
            f"{n} remembered package(s) whose files are gone will be dropped, including their labels and overrides.",
            "Forget",
            lambda: self.window.run_op(self.lib.db.forget_deleted_data, success="Forgotten"),
        )
