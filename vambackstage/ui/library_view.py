from __future__ import annotations

import logging
import time
from collections import Counter
from urllib.parse import urlsplit

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..core.bookmarks import bookmark_resource_id, is_bookmark_key
from ..core.content_types import VISIBLE_CATEGORIES
from ..core.licenses import canonicalize_license
from ..core.paths import LOCAL_PACKAGE_FILENAME, is_local_package
from ..hub.client import hub_resource_page_url
from .dispatch import Debouncer, run_async
from .launch import open_in_file_manager, open_url, open_url_externally
from .models import DictItem, human_size, sync_store
from .widgets import (
    DepRow,
    FilterList,
    PackageCard,
    Thumb,
    category_chip,
    chip,
    clear_box,
    copy_button,
    detail_panel,
    hbox,
    icon_button,
    label,
    name_button,
    scroll_to_top,
    vbox,
    view_blurb,
)

log = logging.getLogger(__name__)

STATUS_OPTIONS = [
    (None, "All packages"),
    ("direct", "Installed directly"),
    ("dependency", "Dependencies"),
    ("updates", "Updates available"),
    ("bookmarked", "Bookmarked"),
    ("missing", "Missing dependencies"),
    ("broken", "Broken"),
    ("corrupted", "Corrupted"),
    ("textures", "Texture problems"),
    ("dummy", "Dummy packages"),
    ("alias", "Aliases"),
    ("orphan", "Orphaned dependencies"),
    ("superseded", "Old versions"),
    ("kept", "Kept old versions"),
    ("disabled", "Disabled"),
    ("offloaded", "Offloaded"),
    ("archived", "Archived"),
    ("local", "Local only (not on Hub)"),
]
SORT_OPTIONS = [
    ("recent", "Recently added"),
    ("name", "Name"),
    ("author", "Author"),
    ("size", "Size"),
    ("deps", "Dependency count"),
]


class LibraryView(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window = window
        self.backend = window.backend
        self.loader = window.app.textures
        self.packages: list[dict] = []
        self.by_filename: dict[str, DictItem] = {}
        self.updates: dict[str, dict] = {}
        self.texture_problems: dict[str, list[dict]] = {}
        self._filter_inputs: tuple[frozenset, frozenset] = (frozenset(), frozenset())
        self.bookmarks: list[dict] = []
        self.search_text = ""
        self.status: str | None = None
        self.type_filter: str | None = None
        self.author: str | None = None
        self.sort_key = "recent"
        self._bound_cards: dict[str, PackageCard] = {}
        self._pending_select: str | None = None
        self.global_counts: dict = {"missing_unique": 0, "orphan": 0}
        self._facet_debounce = Debouncer(80, self._refresh_filters)

        side = vbox(2)
        side.add_css_class("vb-filter-panel")
        side.set_size_request(230, -1)
        side.set_margin_start(10)
        side.set_margin_end(10)
        side.set_margin_top(8)
        self.search = Gtk.SearchEntry(placeholder_text="Search your packages…")
        self.search.connect("search-changed", self._on_search)
        side.append(self.search)
        side.append(view_blurb("Your .var packages: what you installed, what they depend on, and what is missing."))
        self.status_list = FilterList("Status", self._on_status, show_more_after=20)
        self.type_list = FilterList("Type", self._on_type, show_more_after=20)
        self.author_list = FilterList("Author", self._on_author, show_more_after=10)
        side.append(self.status_list)
        side.append(self.type_list)
        side.append(self.author_list)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=side)
        side_scroll.set_size_request(250, -1)
        self.append(side_scroll)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        center = vbox(0)
        center.set_hexpand(True)
        bar = hbox(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        self.count_label = label("", "vb-dim")
        bar.append(self.count_label)
        self.reinstall_shown_btn = Gtk.Button(label="Reinstall all shown…")
        self.reinstall_shown_btn.add_css_class("flat")
        self.reinstall_shown_btn.set_tooltip_text(
            "Download every package the list shows again, with everything each depends on, at the versions they "
            "name: versions asked for that you do not have are added next to the ones you have. With no filter "
            "that is your whole library; filter by status, type, author or search to narrow it. "
            "The plan is shown before anything is downloaded, and what the Hub no longer has is skipped."
        )
        self.reinstall_shown_btn.connect("clicked", lambda *_: self.reinstall_shown())
        bar.append(self.reinstall_shown_btn)
        bar.append(Gtk.Box(hexpand=True))
        self.todo_row = hbox(4)
        self.todo_row.set_margin_start(12)
        self.todo_row.set_margin_end(12)
        self.todo_row.set_margin_bottom(4)
        self.todo_row.set_visible(False)
        self.install_missing_btn = Gtk.Button(label="Missing deps")
        self.install_missing_btn.add_css_class("flat")
        self.install_missing_btn.set_tooltip_text(
            "List what is missing, install what the Hub has, create dummies for the rest"
        )
        self.install_missing_btn.connect("clicked", lambda *_: self.show_missing_deps())
        self.install_missing_btn.set_visible(False)
        self.updates_btn = Gtk.Button(label="Updates")
        self.updates_btn.add_css_class("flat")
        self.updates_btn.set_tooltip_text("Packages with a newer version on the Hub: pick which to download")
        self.updates_btn.connect("clicked", lambda *_: self.show_updates())
        self.updates_btn.set_visible(False)
        self.remove_orphans_btn = Gtk.Button(label="Orphans")
        self.remove_orphans_btn.add_css_class("flat")
        self.remove_orphans_btn.set_tooltip_text("Dependencies nothing needs any more: list and remove them")
        self.remove_orphans_btn.connect("clicked", lambda *_: self.remove_orphans())
        self.remove_orphans_btn.set_visible(False)
        self.old_versions_btn = Gtk.Button(label="Old versions")
        self.old_versions_btn.add_css_class("flat")
        self.old_versions_btn.set_tooltip_text(
            "Older versions a newer installed version supersedes and nothing names exactly: list and remove them"
        )
        self.old_versions_btn.connect("clicked", lambda *_: self.remove_superseded())
        self.old_versions_btn.set_visible(False)
        self.bookmarks_btn = Gtk.Button(label="Bookmarks")
        self.bookmarks_btn.add_css_class("flat")
        self.bookmarks_btn.set_tooltip_text(
            "Hub resources you noted for later: paid or external ones to chase down, free ones to install when you get to them"
        )
        self.bookmarks_btn.connect("clicked", lambda *_: self.show_status("bookmarked"))
        self.bookmarks_btn.set_visible(False)
        self._todo_buttons = (
            self.install_missing_btn,
            self.updates_btn,
            self.remove_orphans_btn,
            self.old_versions_btn,
            self.bookmarks_btn,
        )
        for b in self._todo_buttons:
            self.todo_row.append(b)
            b.connect(
                "notify::visible",
                lambda *_: self.todo_row.set_visible(any(x.get_visible() for x in self._todo_buttons)),
            )
        bar.append(label("Sort", "vb-dim"))
        self.sort_dropdown = Gtk.DropDown.new_from_strings([t for _, t in SORT_OPTIONS])
        self.sort_dropdown.connect("notify::selected", self._on_sort)
        bar.append(self.sort_dropdown)
        center.append(bar)
        center.append(self.todo_row)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda *_: self._on_banner())
        self._banner_action = None
        center.append(self.banner)

        self.store = Gio.ListStore.new(DictItem)
        self.filter = Gtk.CustomFilter.new(self._match)
        self.filtered = Gtk.FilterListModel.new(self.store, self.filter)
        self.sorter = Gtk.CustomSorter.new(self._compare)
        self.sorted = Gtk.SortListModel.new(self.filtered, self.sorter)
        self.selection = Gtk.SingleSelection.new(self.sorted)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_card)
        factory.connect("bind", self._bind_card)
        factory.connect("unbind", self._unbind_card)
        self.grid = Gtk.GridView.new(self.selection, factory)
        self.grid.set_max_columns(12)
        self.grid.set_min_columns(1)
        self.grid.add_css_class("navigation-sidebar")
        self.grid.set_margin_start(8)
        self.grid.set_margin_end(8)
        self.grid.connect("activate", lambda *_: None)
        self.selection.connect("notify::selected-item", self._on_selected)
        self.grid_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid, vexpand=True)
        self.empty = vbox(10, visible=False, vexpand=True, valign=Gtk.Align.CENTER)
        self.empty_text = label("No packages match the search and filters.", "vb-dim", xalign=0.5)
        self.empty_text.set_halign(Gtk.Align.CENTER)
        self.empty.append(self.empty_text)
        self.clear_btn = Gtk.Button(label="Clear search and filters", halign=Gtk.Align.CENTER)
        self.clear_btn.add_css_class("pill")
        self.clear_btn.connect("clicked", lambda *_: self.clear_filters())
        self.empty.append(self.clear_btn)
        self.hub_search_btn = Gtk.Button(label="Search the Hub", halign=Gtk.Align.CENTER, visible=False)
        self.hub_search_btn.add_css_class("pill")
        self.hub_search_btn.connect("clicked", lambda *_: self.window.search_hub(self.search.get_text().strip()))
        self.empty.append(self.hub_search_btn)
        center.append(self.grid_scroll)
        center.append(self.empty)
        self.append(center)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.detail = PackageDetail(self)
        self.detail_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=detail_panel(self.detail, self.clear_selection)
        )
        self.detail_scroll.set_size_request(360, -1)
        self.detail_scroll.set_hexpand(False)
        self.detail_scroll.add_css_class("vb-detail")
        self.detail_scroll.set_visible(False)
        self.append(self.detail_scroll)
        escape = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.BUBBLE)
        escape.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(lambda *_: self.clear_selection() or True),
            )
        )
        self.add_controller(escape)

        self._stale = False
        self._refresh_debounce = Debouncer(150, self.refresh, 3000, lambda: self.backend.downloads.busy)
        window.bridge.connect("packages:updated", lambda _p: self._refresh_debounce.trigger())
        window.bridge.connect("bookmarks:updated", lambda _p: self._refresh_debounce.trigger())
        window.bridge.connect("contents:updated", lambda _p: self.detail.refresh())
        window.bridge.connect("thumbnails:updated", self._on_thumbs_updated)
        self.status_list.set_options([(v, t, None) for v, t in STATUS_OPTIONS], None)

    def on_shown(self) -> None:
        if self._stale:
            self.refresh()

    def refresh_soon(self) -> None:
        self._refresh_debounce.hurry()

    def refresh(self) -> None:
        if not self.backend.lib.is_configured:
            return
        self._stale = self.window.stack.get_visible_child() is not self
        if self._stale:
            return

        def load():
            lib = self.backend.lib
            return lib.list_packages(), lib.status_counts(), lib.local_summary(), lib.bookmarks()

        run_async(load, on_done=self._loaded)

    def _loaded(self, result):
        packages, counts, local_row, bookmarks = result
        self.packages = packages
        self.bookmarks = bookmarks
        self.bookmarks_btn.set_visible(bool(bookmarks))
        self.bookmarks_btn.set_label(f"Bookmarks ({len(bookmarks)})")
        known = {p["filename"] for p in packages}
        self.updates = {k: u for k, u in self.updates.items() if k in known and u.get("hub_filename") not in known}
        self.updates_btn.set_visible(bool(self.updates))
        self.updates_btn.set_label(f"Updates ({len(self.updates)})")
        self.texture_problems = {
            fn: v for fn, v in self.texture_problems.items() if fn in known or is_local_package(fn)
        }
        show_local = bool(local_row) and (
            bool(local_row["missing_deps"]) or LOCAL_PACKAGE_FILENAME in self.texture_problems
        )
        self.rows = ([local_row] if show_local else []) + packages + bookmarks
        self.global_counts = counts
        selected = self.selected_filename()
        self.by_filename, changed = sync_store(self.store, self.rows, lambda p: p["filename"], self.by_filename)
        for item in changed:
            card = self._bound_cards.get(item.key)
            if card is not None:
                card.bind(item.data)
        filter_inputs = (frozenset(self.updates), frozenset(self.texture_problems))
        if filter_inputs != self._filter_inputs:
            self._filter_inputs = filter_inputs
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._refresh_filters()
        self._update_count()
        pending = self._pending_select
        if pending and pending in self.by_filename:
            self._pending_select = None
            self.select_filename(pending, scroll=True)
        elif selected and selected in self.by_filename:
            if self.selected_filename() != selected and not self._reselect(selected):
                self.detail_scroll.set_visible(True)
                self.detail.show_package(selected)
        elif selected:
            self.clear_selection()
        self.detail.refresh()

    def _reselect(self, filename: str) -> bool:
        for i in range(self.sorted.get_n_items()):
            if self.sorted.get_item(i).key == filename:
                self.selection.set_selected(i)
                return True
        return False

    def _refresh_filters(self) -> None:
        pk = self.rows
        status_pool = [
            d
            for d in pk
            if self._type_ok(d, self.type_filter) and self._author_ok(d, self.author) and self._search_ok(d)
        ]
        count_map = {v: sum(1 for d in status_pool if self._status_ok(d, v)) for v, _ in STATUS_OPTIONS}
        always = (None, "direct", "dependency", "missing", "broken", "disabled")
        options = [(v, t, count_map[v]) for v, t in STATUS_OPTIONS if v in always or count_map[v] or v == self.status]
        self._status_options = options
        self.status_list.set_options(options, self.status)
        pk = self.packages + self.bookmarks
        type_pool = [
            d for d in pk if self._status_ok(d, self.status) and self._author_ok(d, self.author) and self._search_ok(d)
        ]
        type_counts = Counter((d["type"] or "Other") for d in type_pool)
        if self.type_filter:
            type_counts.setdefault(self.type_filter, 0)
        types = [(None, "All types", len(type_pool))] + [
            (t, t, n) for t, n in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        self.type_list.set_options(types, self.type_filter)
        author_pool = [
            d
            for d in pk
            if self._status_ok(d, self.status) and self._type_ok(d, self.type_filter) and self._search_ok(d)
        ]
        authors = Counter(d["creator"] for d in author_pool if d["creator"])
        if self.author:
            authors.setdefault(self.author, 0)
        author_opts = [(None, "All authors", len(author_pool))] + [
            (a, a, n) for a, n in sorted(authors.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        ]
        self.author_list.set_options(author_opts, self.author)
        counts = self.global_counts
        self.install_missing_btn.set_visible(counts["missing_unique"] > 0)
        self.install_missing_btn.set_label(f"Missing deps ({counts['missing_unique']})")
        self.install_missing_btn.set_tooltip_text(
            f"{counts['missing_unique']} referenced package(s) are not installed in the version asked for, "
            "counting references from your packages and from your loose scenes and presets. "
            "Lists them; installs what the Hub has, aliases or dummies for the rest."
        )
        self.remove_orphans_btn.set_visible(counts["orphan"] > 0)
        self.remove_orphans_btn.set_label(f"Orphans ({counts['orphan']})")
        self.old_versions_btn.set_visible(counts.get("superseded", 0) > 0)
        self.old_versions_btn.set_label(f"Old versions ({counts.get('superseded', 0)})")
        self._update_banner()

    def _update_banner(self) -> None:
        c = self.global_counts or {}
        title = button = action = None
        if self.status == "missing" and c.get("missing_unique"):
            n = c["missing_unique"]
            title = f"{n} referenced package{'s are' if n != 1 else ' is'} not installed. Install what the Hub has in one go; the rest is listed under Missing deps."
            button, action = "Install all from the Hub", "install_missing"
        elif self.status == "orphan" and c.get("orphan"):
            n = c["orphan"]
            title = f"{n} dependenc{'ies are' if n != 1 else 'y is'} needed by nothing any more."
            button, action = "Remove all", "remove_orphans"
        elif self.status == "superseded" and c.get("superseded"):
            n = c["superseded"]
            title = (
                f"{n} older version{'s' if n != 1 else ''} nothing names by number, each with a newer version installed. "
                "Keep this version, in a package's panel, exempts one you want to keep."
            )
            button, action = "Remove all", "remove_superseded"
        elif self.status == "updates" and self.updates:
            n = len(self.updates)
            title = f"{n} package{'s have' if n != 1 else ' has'} a newer version on the Hub."
            button, action = "Update…", "show_updates"
        elif self.status == "textures" and self.texture_problems:
            n = len(self.texture_problems)
            title = (
                f"{n} {'entries ship' if n != 1 else 'entry ships'} images VaM cannot load, or that look suspect, "
                "as of the last texture check. Each one lists its files in the panel."
            )
            button, action = "Check again", "check_textures"
        elif self.status == "bookmarked" and self.bookmarks:
            n = len(self.bookmarks)
            k = len(self._installable_bookmarks())
            title = (
                f"{n} Hub resource{'s' if n != 1 else ''} noted for later."
                + (f" {k} can be installed from the Hub now." if k else "")
                + " Paid or externally hosted ones: open the page, get the file from the creator and drop it into "
                "AddonPackages; a bookmark clears itself once its package is in your library."
            )
            button, action = (f"Install {k} from Hub", "install_bookmarks") if k else ("", "bookmarks_info")
        self._banner_action = action
        if action:
            self.banner.set_title(title)
            self.banner.set_button_label(button)
        self.banner.set_revealed(bool(action))

    def _on_banner(self) -> None:
        if self._banner_action == "install_missing":
            self.install_all_missing()
        elif self._banner_action == "remove_orphans":
            self.remove_orphans()
        elif self._banner_action == "remove_superseded":
            self.remove_superseded()
        elif self._banner_action == "show_updates":
            self.show_updates()
        elif self._banner_action == "check_textures":
            self.window.check_textures()
        elif self._banner_action == "install_bookmarks":
            self.install_bookmarks()

    def clear_filters(self) -> None:
        self.status = self.type_filter = self.author = None
        self.search_text = ""
        self.search.set_text("")
        self._refilter()

    def _update_count(self):
        n = self.sorted.get_n_items()
        if self.status == "bookmarked":
            self.count_label.set_text(f"{n} bookmark{'s' if n != 1 else ''}")
        else:
            self.count_label.set_text(f"{n} of {len(self.packages)} packages")
        self.reinstall_shown_btn.set_visible(n > 0 and self.status != "bookmarked")
        self.empty.set_visible(n == 0 and len(self.rows) > 0)
        self.grid_scroll.set_visible(n > 0 or len(self.rows) == 0)
        q = self.search.get_text().strip()
        if n == 0:
            status_name = next((t for v, t in STATUS_OPTIONS if v == self.status), None)
            if not q and status_name and not self.type_filter and not self.author:
                self.empty_text.set_text(f"Nothing under “{status_name}” now.")
                self.clear_btn.set_label("Show all packages")
            else:
                self.empty_text.set_text("No packages match the search and filters.")
                self.clear_btn.set_label("Clear search and filters")
        self.hub_search_btn.set_visible(bool(q))
        self.hub_search_btn.set_label(f"Search the Hub for “{q}”" if q else "Search the Hub")

    def _status_ok(self, d: dict, status: str | None) -> bool:
        if d.get("is_bookmark"):
            return status == "bookmarked"
        if status == "bookmarked":
            return False
        if status == "textures":
            return d["filename"] in self.texture_problems
        if d.get("is_local"):
            return status in ("missing", "broken") and bool(d["missing_deps"])
        if status != "archived" and d["storage_state"] == "archived":
            return False
        if status is None:
            return True
        if status == "direct":
            return bool(d["is_direct"])
        if status == "dependency":
            return not d["is_direct"]
        if status == "missing":
            return bool(d["missing_deps"])
        if status == "corrupted":
            return bool(d["is_corrupted"])
        if status == "dummy":
            return bool(d.get("is_dummy"))
        if status == "alias":
            return bool(d.get("is_alias"))
        if status == "broken":
            return bool(
                d["is_corrupted"] or d["missing_deps"] or (d["storage_state"] == "enabled" and d["inactive_deps"])
            )
        if status == "orphan":
            return bool(d["is_orphan"])
        if status == "superseded":
            return bool(d.get("is_superseded"))
        if status == "kept":
            return bool(d.get("keep_version"))
        if status in ("disabled", "offloaded", "archived"):
            return d["storage_state"] == status
        if status == "local":
            return bool(d["is_local_only"])
        if status == "updates":
            return d["filename"] in self.updates
        return True

    @staticmethod
    def _type_ok(d: dict, type_filter: str | None) -> bool:
        if d.get("is_local"):
            return not type_filter
        return not type_filter or (d["type"] or "Other") == type_filter

    @staticmethod
    def _author_ok(d: dict, author: str | None) -> bool:
        if d.get("is_local"):
            return not author
        return not author or d["creator"] == author

    def _search_ok(self, d: dict) -> bool:
        if not self.search_text:
            return True
        hay = " ".join(
            filter(None, [d["filename"], d.get("title"), d.get("hub_display_name"), d["creator"], d.get("hub_tags")])
        ).lower()
        return all(term in hay for term in self.search_text.split())

    def _match(self, item, *_):
        d = item.data
        return (
            self._status_ok(d, self.status)
            and self._type_ok(d, self.type_filter)
            and self._author_ok(d, self.author)
            and self._search_ok(d)
        )

    def _compare(self, a, b, *_):
        x, y = a.data, b.data
        if bool(x.get("is_local")) != bool(y.get("is_local")):
            return -1 if x.get("is_local") else 1
        if self.sort_key == "name":
            kx, ky = (x.get("hub_display_name") or x.get("title") or x["filename"]).lower(), (
                y.get("hub_display_name") or y.get("title") or y["filename"]
            ).lower()
        elif self.sort_key == "author":
            kx, ky = (x["creator"].lower(), (x.get("title") or "").lower()), (
                y["creator"].lower(),
                (y.get("title") or "").lower(),
            )
        elif self.sort_key == "size":
            kx, ky = -x["size_bytes"], -y["size_bytes"]
        elif self.sort_key == "deps":
            kx, ky = (-x["dep_count"], x["filename"]), (-y["dep_count"], y["filename"])
        else:
            kx, ky = (-x["first_seen_at"], -x["file_mtime"], x["filename"]), (
                -y["first_seen_at"],
                -y["file_mtime"],
                y["filename"],
            )
        return -1 if kx < ky else (1 if kx > ky else 0)

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_count()
        self._facet_debounce.trigger()

    def _on_search(self, entry):
        self.search_text = entry.get_text().strip().lower()
        self._refilter()

    def _on_status(self, value):
        self.status = value
        self._refilter()
        self._update_banner()

    def show_status(self, value: str | None) -> None:
        self.status = value
        options = getattr(self, "_status_options", None) or [(v, t, None) for v, t in STATUS_OPTIONS]
        if not any(v == value for v, _, _ in options):
            options = options + [(v, t, None) for v, t in STATUS_OPTIONS if v == value]
        self.status_list.set_options(options, value)
        self._refilter()
        self._update_banner()

    def _on_type(self, value):
        self.type_filter = value
        self._refilter()

    def _on_author(self, value):
        self.author = value
        self._refilter()

    def _on_sort(self, dropdown, _pspec):
        self.sort_key = SORT_OPTIONS[dropdown.get_selected()][0]
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)

    def focus_search(self):
        self.search.grab_focus()

    def _setup_card(self, _factory, item):
        item.set_child(PackageCard(self.loader))

    def _bind_card(self, _factory, item):
        card = item.get_child()
        data = item.get_item().data
        card.bind(data)
        self._bound_cards[data["filename"]] = card

    def _unbind_card(self, _factory, item):
        data = item.get_item().data
        if self._bound_cards.get(data["filename"]) is item.get_child():
            del self._bound_cards[data["filename"]]

    def _on_thumbs_updated(self, payload):
        keys = (payload or {}).get("keys") or []
        self.loader.invalidate(keys)
        for key in keys:
            card = self._bound_cards.get(key[4:]) if key.startswith("pkg:") else None
            if card:
                card.thumb.load(self.loader, key)

    def selected_filename(self) -> str | None:
        item = self.selection.get_selected_item()
        return item.key if item else None

    def clear_selection(self) -> None:
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        self.detail_scroll.set_visible(False)
        self.detail.show_package(None)

    def _on_selected(self, *_):
        item = self.selection.get_selected_item()
        self.detail_scroll.set_visible(item is not None)
        self.detail.show_package(item.key if item else None)
        if item:
            scroll_to_top(self.detail_scroll)

    def select_filename(self, filename: str, scroll: bool = True) -> None:
        if is_local_package(filename) and filename not in self.by_filename:
            self.window.show_content_for_package(filename)
            return
        if filename not in self.by_filename:
            self._pending_select = filename
            return
        for i in range(self.sorted.get_n_items()):
            if self.sorted.get_item(i).key == filename:
                self.selection.set_selected(i)
                if scroll:
                    self.grid.scroll_to(i, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                return
        self.status = "missing" if is_local_package(filename) else ("bookmarked" if is_bookmark_key(filename) else None)
        self.type_filter = None
        self.author = None
        self.search.set_text("")
        self.status_list.select(self.status)
        self.type_list.select(None)
        self.author_list.select(None)
        self._refilter()
        for i in range(self.sorted.get_n_items()):
            if self.sorted.get_item(i).key == filename:
                self.selection.set_selected(i)
                self.grid.scroll_to(i, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                return

    def show_missing_deps(self):
        if not self.install_missing_btn.get_sensitive():
            return
        self.install_missing_btn.set_sensitive(False)

        def done(rows):
            self.install_missing_btn.set_sensitive(True)
            self._missing_dialog(rows)

        def failed(e):
            self.install_missing_btn.set_sensitive(True)
            self.window.toast(f"Could not list missing dependencies: {e}", 8)

        run_async(self.backend.missing_deps, on_done=done, on_error=failed)

    def _missing_dialog(self, rows: list[dict]):
        if not rows:
            self.window.toast("No missing dependencies.")
            return
        on_hub = [r for r in rows if r.get("hub") and not r["hub"].get("installed_locally")]
        shadowed = [r for r in rows if r.get("is_fallback")]
        near = [r for r in rows if not r.get("is_fallback") and r.get("near_matches")]
        dummy_ok = [r for r in rows if not r.get("hub") and not r.get("is_fallback") and not r.get("near_matches")]
        listing = vbox(4)
        listing.set_size_request(700, -1)
        for r in rows:
            line = hbox(8)
            ref_lbl = label(r["ref"], "vb-mono", selectable=True, ellipsize=False, wrap=True)
            ref_lbl.set_width_chars(26)
            ref_lbl.set_hexpand(True)
            line.append(ref_lbl)
            line.append(copy_button(r["ref"]))
            needed_by = [x for x in (r.get("needed_by") or []) if isinstance(x, dict)]
            n = len(needed_by)
            who = [x.get("name") or x.get("filename") for x in needed_by]
            who += [f"  {p}" for p in (r.get("local_files") or [])]
            text = (f"needed by {who[0]}" + (f" +{n - 1}" if n > 1 else "")) if who else f"needed by {n}"
            needed = Gtk.Button(label=text)
            needed.add_css_class("flat")
            needed.add_css_class("vb-small")
            needed.get_child().set_ellipsize(Pango.EllipsizeMode.END)
            needed.get_child().set_max_width_chars(24)
            needed.set_tooltip_text("\n".join(who[:30]) + "\nShow in the library")
            needed.connect(
                "clicked",
                lambda _b, fn=(needed_by[0]["filename"] if needed_by else None): (
                    (dialog.close(), self.select_filename(fn)) if fn else None
                ),
            )
            line.append(needed)
            if r.get("hub") and not r["hub"].get("installed_locally"):
                c = chip(
                    "ON THE HUB" if r["hub"].get("is_exact") else f"HUB HAS V{r['hub'].get('hub_version')}", "success"
                )
            elif r.get("is_fallback"):
                c = chip(f"V{r['fallback_version']} INSTALLED", "warn")
            elif r.get("near_matches"):
                c = chip("SAME NAME, OTHER CREATOR", "warn")
                c.set_tooltip_text(
                    "You have "
                    + ", ".join(r["near_matches"][:3])
                    + ".\nThe same package renamed, or someone else's fork "
                    "with the same name: VaM matches the full name either way. Decide under Options…."
                )
            else:
                c = chip("NOT ON THE HUB", "error")
            c.set_halign(Gtk.Align.END)
            c.set_hexpand(True)
            line.append(c)
            opts = Gtk.Button(label="Options…")
            opts.add_css_class("flat")
            opts.add_css_class("vb-small")
            opts.set_tooltip_text("Install another version, alias, or a dummy for this one reference")
            opts.connect("clicked", lambda _b, ref=r["ref"]: (dialog.close(), self.detail._make_dummy(ref)))
            line.append(opts)
            listing.append(line)
        scroller = Gtk.ScrolledWindow(
            child=listing, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=320
        )
        parts = [f"{len(rows)} reference{'s are' if len(rows) != 1 else ' is'} not satisfied."]
        if on_hub:
            parts.append(f"{len(on_hub)} can be installed from the Hub.")
        if dummy_ok:
            parts.append(
                f"{len(dummy_ok)} not on the Hub: an empty dummy package can stand in for each so VaM stops reporting it."
            )
        if shadowed:
            parts.append(
                f"{len(shadowed)} name a version of a package you have in another version: an alias (a hard-linked copy of "
                "the version you have under the missing name) makes files referenced through it come from the real one."
            )
        if near:
            parts.append(
                f"{len(near)} name a package you have under another creator name: the same file renamed, or a fork with "
                "the same name. Options… on the row offers an alias if you know it is the same thing."
            )
        dialog = Adw.AlertDialog(heading="Missing dependencies", body=" ".join(parts))
        dialog.set_extra_child(scroller)
        dialog.add_response("cancel", "Close")
        if on_hub:
            dialog.add_response("install", f"Install {len(on_hub)} from Hub")
            dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        if dummy_ok:
            dialog.add_response("dummies", f"Create {len(dummy_ok)} dumm{'ies' if len(dummy_ok) != 1 else 'y'}")
        if shadowed:
            dialog.add_response("shadowed", f"Alias {len(shadowed)} to the installed version")
        dialog.set_close_response("cancel")

        def on_response(_d, r):
            if r == "install":
                self.install_all_missing()
            elif r == "dummies":
                self.window.run_op(
                    self.backend.lib.create_dummies, [x["ref"] for x in dummy_ok], then=self.report_dummies
                )
            elif r == "shadowed":
                self.window.run_op(
                    self.backend.lib.create_aliases, [x["ref"] for x in shadowed], then=self.report_aliases
                )

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def report_aliases(self, res: dict):
        n = len(res["created"])
        msg = f"Created {n} alias{'es' if n != 1 else ''}" + (
            ": " + ", ".join(f"{c['filename']} → {c['source']}" for c in res["created"][:3]) if n else ""
        )
        if res["skipped"]:
            msg += "; skipped " + "; ".join(f"{ref} ({why})" for ref, why in res["skipped"][:3])
        self.window.toast(msg, 8)

    def report_dummies(self, res: dict):
        n = len(res["created"])
        msg = f"Created {n} dummy package{'s' if n != 1 else ''}"
        if res["skipped"]:
            msg += "; skipped " + "; ".join(f"{ref} ({why})" for ref, why in res["skipped"][:3])
        self.window.toast(msg, 8)

    def install_all_missing(self):
        self.window.run_op(self.backend.downloads.enqueue_install_all_missing, then=self._report_install)

    def reinstall_shown(self):
        shown = [self.sorted.get_item(i) for i in range(self.sorted.get_n_items())]
        roots = [it.key for it in shown if not is_bookmark_key(it.key)]
        if not roots:
            self.window.toast("No packages are shown.")
            return
        if len(roots) == 1:
            d = next(it.data for it in shown if it.key == roots[0])
            name = d.get("hub_display_name") or d.get("title") or d["package_name"]
            heading = f"Reinstall {name} with its dependencies?"
        else:
            heading = f"Reinstall {len(roots)} packages with their dependencies?"
        self.window.reinstall_with_deps(roots, heading=heading)

    def _installable_bookmarks(self) -> list[dict]:
        return [b for b in self.bookmarks if b.get("is_downloadable") and not b.get("installed_filename")]

    def install_bookmarks(self):
        rids = [b["resource_id"] for b in self._installable_bookmarks()]
        if not rids:
            self.window.toast("None of the bookmarks can be downloaded from the Hub.")
            return

        def done(r):
            msg = f"Queued {r['queued']} download(s)"
            if r["already"]:
                msg += f", {r['already']} already in your library or downloading"
            if r["failed"]:
                msg += "; failed: " + "; ".join(f"{rid}: {why}" for rid, why in r["failed"][:3])
            self.window.toast(msg, 10)

        self.window.run_op(self.backend.install_bookmarks, rids, then=done)

    def _report_install(self, r: dict) -> None:
        parts = [f"Queued {r['queued']} download(s)"]
        if r.get("already_installed"):
            parts.append(
                f"the Hub's file for {', '.join(r['already_installed'][:3])} is already installed (another version)"
            )
        if r.get("unresolved_deps"):
            parts.append(
                f"the Hub does not serve {', '.join(r['unresolved_deps'][:3])}"
                + (" …" if len(r["unresolved_deps"]) > 3 else "")
                + "; use Options… in the missing list for an alias or a dummy"
            )
        self.window.toast("; ".join(parts), 10)

    def remove_orphans(self):
        self.show_status("orphan")
        counts = self.backend.lib.status_counts()
        size = human_size(self.backend.lib.store.get_orphan_total_size())
        self.window.confirm(
            "Remove orphaned dependencies?",
            f"{counts['orphan']} package(s) that nothing depends on will be deleted from disk ({size}).",
            "Remove",
            lambda: self.window.run_op(
                self.backend.lib.remove_orphans,
                then=lambda r: self.window.toast(
                    f"Removed {r['count']} package(s), freed {human_size(r['freed_bytes'])}"
                ),
            ),
        )

    def remove_superseded(self):
        self.show_status("superseded")
        counts = self.backend.lib.status_counts()
        size = human_size(self.backend.lib.store.get_superseded_total_size())
        self.window.confirm(
            "Remove old versions?",
            f"{counts['superseded']} older version(s) will be deleted from disk ({size}). Each has a newer version installed "
            "and enabled, and nothing refers to the old version by number; references to the newest or to a minimum "
            "version already use the newer one.",
            "Remove",
            lambda: self.window.run_op(
                self.backend.lib.remove_superseded,
                then=lambda r: self.window.toast(
                    f"Removed {r['count']} old version(s), freed {human_size(r['freed_bytes'])}"
                ),
            ),
        )

    def check_updates(self):
        def done(updates):
            if updates is None:
                self.window.toast("Could not reach the Hub package index.")
                return
            self.updates = updates
            self.refresh()
            if updates:
                self.show_updates()
            else:
                self.window.toast("Everything is up to date.")

        run_async(
            self.backend.check_updates,
            True,
            on_done=done,
            on_error=lambda e: self.window.toast(f"Update check failed: {e}"),
        )

    def show_updates(self):
        if not self.updates:
            self.window.toast("No updates known; use Check for updates in the menu.")
            return
        entries = [dict(u, filename=fn) for fn, u in sorted(self.updates.items(), key=lambda kv: kv[0].lower())]
        self.updates_btn.set_sensitive(False)

        def done(probe):
            self.updates_btn.set_sensitive(True)
            self._updates_dialog(entries, probe)

        def failed(err):
            self.updates_btn.set_sensitive(True)
            self._updates_dialog(entries, {})

        run_async(self.backend.probe_updates, entries, on_done=done, on_error=failed)

    def _updates_dialog(self, entries: list[dict], probe: dict):
        checks: list[tuple[Gtk.CheckButton, dict]] = []
        listing = vbox(4)
        listing.set_size_request(600, -1)
        blocked = 0
        for u in entries:
            p = probe.get(u.get("hub_filename") or "", {})
            downloadable = p.get("downloadable", True)
            line = hbox(8)
            cb = Gtk.CheckButton(active=downloadable)
            line.append(cb)
            name = u.get("package_name") or u["filename"]
            lbl = label(f"{name.split('.')[-1]}  v{u['current_version']} → v{u['hub_version']}", "vb-small")
            lbl.set_tooltip_text(f"{u['filename']} → {u.get('hub_filename')}")
            lbl.set_hexpand(True)
            line.append(lbl)
            if not downloadable:
                blocked += 1
                c = chip("NO DOWNLOAD ON THE HUB" if p.get("found") else "NOT ON THE HUB BY NAME", "warn")
                c.set_tooltip_text(
                    "The Hub's index lists this version, but the Hub offers no file for it (paid, or hosted elsewhere). "
                    "Open its page, or tick it and Ignore to stop seeing it."
                    if p.get("found")
                    else "The index lists this file but the Hub does not find it by name. Tick it and Ignore to stop seeing it."
                )
                line.append(c)
                rid = p.get("resource_id") or u.get("hub_resource_id")
                if rid:
                    b = Gtk.Button(label="Open on Hub")
                    b.add_css_class("flat")
                    b.add_css_class("vb-small")
                    b.connect("clicked", lambda _b, r=rid: open_url(hub_resource_page_url(r)))
                    line.append(b)
            else:
                line.append(label(u.get("hub_filename") or "", "vb-mono vb-dim vb-small"))
            listing.append(line)
            checks.append((cb, u))
        scroller = Gtk.ScrolledWindow(
            child=listing, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=360
        )
        body = "The newer version is downloaded as a package you chose; the version you have stays until you remove it."
        if blocked:
            body += (
                f" {blocked} of them cannot be downloaded from the Hub: open the page to get it from the author, "
                "or tick it and choose Ignore so it stops being listed (a newer version will show up again)."
            )
        dialog = Adw.AlertDialog(
            heading=f"{len(entries)} update{'s' if len(entries) != 1 else ''} on the Hub", body=body
        )
        dialog.set_extra_child(scroller)
        dialog.add_response("cancel", "Close")
        dialog.add_response("ignore", "Ignore ticked")
        dialog.add_response("selected", "Update ticked")
        dialog.add_response("all", "Update all")
        dialog.set_response_appearance("all", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("all")
        dialog.set_close_response("cancel")

        def on_response(_d, r):
            if r == "cancel":
                return
            chosen = [u for cb, u in checks if r == "all" or cb.get_active()]
            if not chosen:
                self.window.toast("Nothing ticked.")
                return
            if r == "ignore":
                self.window.run_op(
                    self.backend.ignore_updates,
                    chosen,
                    then=lambda n: (
                        self._drop_updates(chosen),
                        self.window.toast(
                            f"Ignoring {n} update(s) until a newer version appears; "
                            "Settings › Maintenance forgets the list",
                            8,
                        ),
                    ),
                )
                return
            can = [u for u in chosen if probe.get(u.get("hub_filename") or "", {}).get("downloadable", True)]
            cannot = [u for u in chosen if u not in can]

            def done(res):
                msg = f"Queued {res['queued']} update(s)"
                if res["already"]:
                    msg += f", {res['already']} already installed"
                if cannot:
                    msg += f"; {len(cannot)} not downloadable from the Hub (open their pages, or ignore them)"
                if res["failed"]:
                    msg += "; failed: " + "; ".join(f"{fn}: {why}" for fn, why in res["failed"][:3])
                self.window.toast(msg, 10)

            if can:
                self.window.run_op(self.backend.enqueue_updates, can, then=done)
            else:
                done({"queued": 0, "already": 0, "failed": []})

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def set_texture_problems(self, result: dict, replace_all: bool) -> None:
        if replace_all:
            self.texture_problems = {}
        else:
            for fn in result.get("checked_files") or []:
                self.texture_problems.pop(fn, None)
        for p in result.get("problems") or []:
            self.texture_problems.setdefault(p["filename"], []).append(p)
        self.refresh()

    def show_texture_problems(self) -> None:
        self.show_status("textures")

    def _drop_updates(self, entries: list[dict]) -> None:
        for u in entries:
            self.updates.pop(u.get("filename"), None)
        self.updates_btn.set_visible(bool(self.updates))
        self.updates_btn.set_label(f"Updates ({len(self.updates)})")
        self._refilter()


class PackageDetail(Gtk.Box):

    def __init__(self, view: LibraryView):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.view = view
        self.window = view.window
        self.backend = view.backend
        self.filename: str | None = None
        self.detail: dict | None = None
        self.set_margin_start(14)
        self.set_margin_end(14)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.thumb = Thumb(330, 200, follow_image=True)
        self.append(self.thumb)
        self.title = label("", "vb-detail-title", wrap=True)
        self.title.set_selectable(True)
        self.append(self.title)
        self.sub = label("", "vb-detail-sub", wrap=True)
        self.sub.set_selectable(True)
        self.append(self.sub)
        self.chips = hbox(4)
        self.append(self.chips)
        self.actions = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=4,
            column_spacing=4,
            row_spacing=4,
            homogeneous=False,
        )
        self.append(self.actions)
        self.description = label("", "vb-dim", wrap=True)
        self.description.set_selectable(True)
        self.append(self.description)
        self.note_box = vbox(2, visible=False)
        self.note_box.append(label("NOTE TO SELF", "vb-section-label"))
        self.note_entry = Gtk.Entry(placeholder_text="Where to get it, which tier, what for…")
        self.note_entry.connect("activate", lambda *_: self._save_note())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._save_note())
        self.note_entry.add_controller(focus)
        self.note_box.append(self.note_entry)
        self.append(self.note_box)
        self.textures_box = vbox(2)
        self.append(self.textures_box)
        self.deps_box = vbox(2)
        self.append(self.deps_box)
        self.dependents_box = vbox(2)
        self.append(self.dependents_box)
        self.contents_box = vbox(2)
        self.append(self.contents_box)

    def show_package(self, filename: str | None):
        self.filename = filename
        self.refresh()

    def refresh(self):
        if not self.filename:
            return
        fn = self.filename
        if is_bookmark_key(fn):
            run_async(self.backend.lib.bookmark_detail, bookmark_resource_id(fn), on_done=lambda d: self._render(fn, d))
            return
        run_async(self.backend.lib.package_detail, fn, on_done=lambda d: self._render(fn, d))

    def _button(self, text: str, cb, style: str | None = None, tooltip: str | None = None) -> Gtk.Button:
        btn = Gtk.Button(label=text)
        if style:
            btn.add_css_class(style)
        if tooltip:
            btn.set_tooltip_text(tooltip)
        btn.connect("clicked", lambda *_: cb())
        self.actions.append(btn)
        return btn

    def _render(self, fn: str, d: dict | None):
        if fn != self.filename:
            return
        self.detail = d
        if d is None:
            self.title.set_text("")
            return
        self.note_box.set_visible(bool(d.get("is_bookmark")))
        if d.get("is_bookmark"):
            self._render_bookmark(fn, d)
            return
        if d.get("is_local"):
            self._render_local(fn, d)
            return
        self.thumb.set_visible(True)
        self.thumb.load(self.view.loader, "pkg:" + fn)
        self.title.set_text(d.get("hub_display_name") or d.get("title") or d["package_name"])
        hub_v = d.get("hub_version_string")
        self.sub.set_text(
            f"{d['creator']} · version {d['version']}"
            + (f" (v{hub_v.lstrip('vV')} on the Hub)" if hub_v else "")
            + f" · {human_size(d['size_bytes'])}\n{fn}"
        )
        self.sub.set_tooltip_text(
            "The number in a .var name is VaM's per-upload counter; the Hub label is the author's own version."
            if hub_v
            else None
        )
        clear_box(self.chips)
        self.chips.append(category_chip(d.get("type")))
        self.chips.append(chip("DIRECT" if d["is_direct"] else "DEPENDENCY", "success" if d["is_direct"] else "dep"))
        if d["storage_state"] != "enabled":
            self.chips.append(chip(d["storage_state"].upper(), "muted"))
        if d.get("license"):
            self.chips.append(chip(canonicalize_license(d["license"]) or d["license"], "muted"))
        if d["is_corrupted"]:
            self.chips.append(chip("CORRUPTED", "error"))
        if d.get("is_dummy"):
            self.chips.append(chip("DUMMY", "muted"))
        if d.get("is_alias"):
            self.chips.append(chip("ALIAS", "muted"))
        if d.get("is_unpacked"):
            c = chip("UNPACKED", "muted")
            c.set_tooltip_text(
                "A folder named like the .var, as VaM's Package Manager leaves it after Unpack; VaM loads it like the "
                "archive. Edit files… changes what is inside, Repack makes an archive of it again."
            )
            self.chips.append(c)
        if d.get("is_superseded"):
            c = chip("OLD VERSION", "muted")
            c.set_tooltip_text(
                f"Superseded by {d.get('superseded_by')}; nothing refers to this version by number, so it only takes space. "
                "Keep this version exempts it."
            )
            self.chips.append(c)
        if d.get("keep_version"):
            c = chip("KEPT", "muted")
            c.set_tooltip_text(
                "Kept on purpose: this older version stays out of Old versions and is never removed with them."
            )
            self.chips.append(c)
        self._texture_chip(fn)

        self._clear_actions()
        state = d["storage_state"]
        if state == "enabled":
            self._button("Disable", self._toggle_enabled)
        else:
            self._button("Enable", self._toggle_enabled, "suggested-action")
        if d["is_direct"]:
            self._button(
                "Mark as dependency",
                self._demote,
                tooltip="Treat it as only needed by other packages: its content is hidden in VaM by the dependency rule, "
                "and once nothing needs it any more it counts as an orphan.",
            )
        else:
            self._button(
                "Mark as installed directly",
                self._promote,
                "suggested-action",
                tooltip="Treat it as a package you chose: it counts as installed directly, is never an orphan, "
                "and its content is shown in VaM.",
            )
        if d.get("is_superseded"):
            self._button(
                "Keep this version",
                lambda: self._set_keep(True),
                tooltip="An older version you want although a newer one is installed and nothing names this one by "
                "number: it leaves Old versions, and Remove all never touches it.",
            )
        elif d.get("keep_version"):
            self._button(
                "Stop keeping",
                lambda: self._set_keep(False),
                tooltip="Treat it like any other old version again: listed under Old versions while a newer version is "
                "installed and nothing names it by number.",
            )
        if d.get("inactive_deps") and state == "enabled":
            self._button(f"Enable {d['inactive_deps']} inactive deps", self._enable_deps)
        if d.get("missing_deps_total"):
            self._button(f"Install {d['missing_deps_total']} missing", self._install_missing, "suggested-action")
        self._button("Uninstall", self._uninstall, "destructive-action")
        if d.get("hub_resource_id") or d.get("is_hub_replaceable"):
            self._button("Reinstall", self._reinstall)
        if d["deps"] and not (d.get("is_dummy") or d.get("is_alias")):
            name = d.get("hub_display_name") or d.get("title") or d["package_name"]
            self._button(
                "Reinstall with dependencies…",
                lambda: self.window.reinstall_with_deps([fn], heading=f"Reinstall {name} with its dependencies?"),
                tooltip="Download this package and everything it depends on again, at the versions it names: "
                "a version it asks for that you do not have is added next to the one you have, so VaM loads the "
                "version this package was made with. Shows the plan before anything is downloaded.",
            )
        self._button("Show in folder", self._open_folder)
        if d.get("hub_resource_id"):
            self._button("Open on Hub", lambda: open_url(hub_resource_page_url(d["hub_resource_id"])))
        if d.get("content_count"):
            self._button("Browse content", lambda: self.window.show_content_for_package(fn))
        if d.get("is_dummy") or d.get("is_alias"):
            self._placeholder_actions(fn, d)
        else:
            self._button(
                "Check textures",
                lambda: self.window.check_textures([fn], include_deps=True),
                tooltip="Read every image in this package and its dependencies and list the ones VaM's decoder "
                "cannot read: another format behind a .jpg or .png name, an empty or truncated file, a JPEG "
                "flavor it rejects. VaM reports such a texture as a GDI+ error without naming the file.",
            )
            self._unpack_actions(d)

        self.description.set_text(d.get("description") or "")
        self.description.set_visible(bool(d.get("description")))
        if d.get("is_dummy") or d.get("is_alias"):
            self.description.set_text(self._placeholder_text(fn, d))
            self.description.set_visible(True)
        self._render_texture_problems(fn)

        clear_box(self.deps_box)
        if d["deps"]:
            self.deps_box.append(label(f"DEPENDENCIES ({d['deps_total']})", "vb-section-label"))
            self._render_deps(d["deps"], 0, set())
        clear_box(self.dependents_box)
        if d["dependents"]:
            self.dependents_box.append(label(f"USED BY ({len(d['dependents'])})", "vb-section-label"))
            for dep in d["dependents"]:
                row = hbox(6)
                shown = dep.get("display") or dep["filename"]
                row.append(
                    name_button(
                        shown, lambda f=dep["filename"]: self.view.select_filename(f), f"{shown}\nShow in the library"
                    )
                )
                if dep.get("storage_state") != "enabled":
                    row.append(chip(dep["storage_state"].upper(), "muted"))
                self.dependents_box.append(row)
        clear_box(self.contents_box)
        if d["contents"]:
            self.contents_box.append(label(f"CONTENT ({len(d['contents'])})", "vb-section-label"))
            by_cat: dict[str, list[dict]] = {}
            for c in d["contents"]:
                by_cat.setdefault(c.get("category") or "Other", []).append(c)
            order = list(VISIBLE_CATEGORIES) + ["SubScenes", "Other"]
            for cat in sorted(by_cat, key=lambda c: order.index(c) if c in order else 99):
                items = by_cat[cat]
                head = hbox(6)
                head.append(category_chip(cat if cat != "Other" else None))
                head.append(label(f"{len(items)}", "vb-dim vb-small"))
                head.append(Gtk.Box(hexpand=True))
                hide_all = icon_button(
                    "view-conceal-symbolic", "Hide all in VaM", lambda items=items: self._set_hidden(items, True)
                )
                show_all = icon_button(
                    "view-reveal-symbolic", "Show all in VaM", lambda items=items: self._set_hidden(items, False)
                )
                head.append(show_all)
                head.append(hide_all)
                self.contents_box.append(head)
                for c in items[:200]:
                    self.contents_box.append(self._content_row(c))

    def _render_local(self, fn: str, d: dict):
        self.thumb.set_visible(False)
        self.title.set_text("Loose scenes and presets")
        self.sub.set_text(f"Your own files under Saves and Custom, outside any package · {d['content_count']} items")
        clear_box(self.chips)
        self.chips.append(chip("LOCAL", "muted"))
        self._texture_chip(fn)
        self._clear_actions()
        if d.get("missing_deps_total"):
            self._button(f"Install {d['missing_deps_total']} missing", self._install_missing, "suggested-action")
        if d["deps"]:
            self._button(
                "Reinstall dependencies…",
                lambda: self.window.reinstall_with_deps(
                    [fn], heading="Reinstall what your loose scenes and presets use?"
                ),
                tooltip="Download every package your loose scenes and presets refer to again, at the versions "
                "they name. Shows the plan before anything is downloaded.",
            )
        self._button("Browse content", lambda: self.window.show_content_for_package(fn))
        self.description.set_text(
            "Packages referenced by these files count as dependencies: nothing they need is an orphan, "
            "and what they reference but you do not have is listed here."
        )
        self.description.set_visible(True)
        self._render_texture_problems(fn)
        clear_box(self.deps_box)
        if d["deps"]:
            self.deps_box.append(label(f"REFERENCED PACKAGES ({d['deps_total']})", "vb-section-label"))
            self._render_deps(d["deps"], 0, set())
        clear_box(self.dependents_box)
        files = d.get("files_missing") or []
        if files:
            self.dependents_box.append(label(f"FILES MISSING SOMETHING ({len(files)})", "vb-section-label"))
            for f in files:
                row = hbox(6)
                row.add_css_class("vb-dep-row")
                row.append(
                    name_button(
                        f["path"],
                        lambda p=f["path"]: self.window.show_content_item("__local__", p),
                        f"{f['path']}\nMissing: " + ", ".join(f["missing"]) + "\nShow this file",
                    )
                )
                row.append(chip(f"{len(f['missing'])} MISSING", "warn"))
                self.dependents_box.append(row)
        clear_box(self.contents_box)

    def _clear_actions(self) -> None:
        child = self.actions.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.actions.remove(child)
            child = nxt

    def _render_bookmark(self, fn: str, d: dict):
        rid = d["resource_id"]
        self.thumb.set_visible(True)
        self.thumb.load(self.view.loader, f"hub-icon:{rid}")
        self.title.set_text(d["title"])
        parts = [f"by {d['creator']}" if d.get("creator") else "on the Hub"]
        if d.get("version_string"):
            parts.append(f"version {d['version_string']}")
        if d.get("category"):
            parts.append(d["category"])
        when = time.strftime("%Y-%m-%d", time.localtime(d["created_at"])) if d.get("created_at") else None
        self.sub.set_text(" · ".join(parts) + (f"\nBookmarked {when}" if when else ""))
        self.sub.set_tooltip_text(None)
        clear_box(self.chips)
        self.chips.append(category_chip(d.get("type")))
        self.chips.append(chip("BOOKMARK", "bookmark"))
        if d.get("category") and d["category"] != "Free":
            self.chips.append(chip(d["category"].upper(), "warn"))
        if d.get("license"):
            self.chips.append(chip(canonicalize_license(d["license"]) or d["license"], "muted"))
        installed = d.get("installed_filename")
        if installed:
            c = chip("IN LIBRARY", "success")
            c.set_tooltip_text(f"{installed} is a package of this resource.")
            self.chips.append(c)
        self._clear_actions()
        if installed:
            self._button(
                "Show in library",
                lambda: self.view.select_filename(installed),
                "suggested-action",
                tooltip=f"Show {installed} in the library",
            )
        elif d.get("is_downloadable"):
            self._button(
                "Install from Hub",
                lambda: self._install_bookmark(d),
                "suggested-action",
                tooltip="Queue the download together with the dependencies you do not have, as Install on the Hub "
                "page does. The bookmark clears itself once the package arrives.",
            )
        self._button("Open on Hub", lambda: open_url(hub_resource_page_url(rid)))
        link = d.get("external_link")
        if link:
            host = (urlsplit(link).hostname or "link").removeprefix("www.")
            self._button(f"Open {host}", lambda: open_url_externally(link), tooltip=link)
        if d.get("package_names"):
            self._button(
                "Copy package name",
                lambda: self._copy_name(d["package_names"][0]),
                tooltip="Copy the package name for searching elsewhere (Patreon, Discord).",
            )
        self._button(
            "Remove bookmark",
            lambda: self._remove_bookmark(d),
            tooltip="Forget this note. Bookmarks also clear themselves once a package of the resource is in your library.",
        )
        if installed:
            text = (
                f"A package of this resource is already in your library: {installed}. A bookmark clears itself when "
                "its package arrives after it was noted; this one was there first, so remove the bookmark once it "
                "has served its purpose."
            )
        elif d.get("is_downloadable"):
            text = (
                "Noted for later from the Hub. The Hub serves this file, so Install from Hub queues it whenever you "
                "are ready; the bookmark clears itself once the package is in your library."
            )
        else:
            text = (
                "Noted for later from the Hub. The Hub offers no download for it (paid, or hosted elsewhere): get the "
                "file from the creator's page, drop it into AddonPackages, and the bookmark clears itself once a "
                "package of this resource is in your library."
            )
        if d.get("tag_line"):
            text = d["tag_line"] + "\n\n" + text
        self.description.set_text(text)
        self.description.set_visible(True)
        typing = bool(self.note_entry.get_state_flags() & Gtk.StateFlags.FOCUS_WITHIN)
        if not typing and self.note_entry.get_text() != (d.get("note") or ""):
            self.note_entry.set_text(d.get("note") or "")
        clear_box(self.textures_box)
        clear_box(self.deps_box)
        files = d.get("hub_filenames") or []
        sizes: dict[str, int] = {}
        for f in (d.get("snapshot") or {}).get("hubFiles") or []:
            if isinstance(f, dict) and f.get("filename") and str(f.get("file_size") or "").isdigit():
                name = str(f["filename"])
                sizes[name if name.lower().endswith(".var") else name + ".var"] = int(f["file_size"])
        if files:
            self.deps_box.append(label(f"FILES ON THE HUB ({len(files)})", "vb-section-label"))
            for name in files:
                row = hbox(6)
                row.add_css_class("vb-dep-row")
                lbl = label(name, "vb-mono vb-small", selectable=True)
                lbl.set_hexpand(True)
                row.append(lbl)
                if name in sizes:
                    row.append(label(human_size(sizes[name]), "vb-dim vb-small"))
                row.append(copy_button(name))
                self.deps_box.append(row)
        elif d.get("package_names"):
            self.deps_box.append(label(f"PACKAGES ({len(d['package_names'])})", "vb-section-label"))
            for name in d["package_names"]:
                row = hbox(6)
                row.add_css_class("vb-dep-row")
                lbl = label(name, "vb-mono vb-small", selectable=True)
                lbl.set_hexpand(True)
                row.append(lbl)
                row.append(copy_button(name))
                self.deps_box.append(row)
        clear_box(self.dependents_box)
        clear_box(self.contents_box)

    def _save_note(self) -> None:
        d = self.detail
        if not d or not d.get("is_bookmark"):
            return
        text = self.note_entry.get_text().strip()
        if text == (d.get("note") or ""):
            return
        d["note"] = text
        run_async(
            self.backend.lib.set_bookmark_note,
            d["resource_id"],
            text,
            on_error=lambda e: self.window.toast(f"Could not save the note: {e}", 8),
        )

    def _install_bookmark(self, d: dict) -> None:
        def done(res):
            n = res["inserted"]
            if n:
                msg = f"Queued {n} download(s)"
            elif res.get("already_queued"):
                msg = "Already downloading"
            else:
                msg = "Already in your library"
            if res.get("unresolved_deps"):
                k = len(res["unresolved_deps"])
                msg += f"; {k} dependenc{'y' if k == 1 else 'ies'} not available on the Hub"
            self.window.toast(msg, 8)

        self.window.run_op(self.backend.downloads.enqueue_install, d["resource_id"], then=done)

    def _remove_bookmark(self, d: dict) -> None:
        self.window.run_op(self.backend.lib.remove_bookmark, d["resource_id"], success="Bookmark removed")

    def _texture_chip(self, fn: str) -> None:
        problems = self.view.texture_problems.get(fn) or []
        if not problems:
            return
        bad = any(p["severity"] == "error" for p in problems)
        c = chip("BAD TEXTURES" if bad else "SUSPECT TEXTURES", "error" if bad else "warn")
        c.set_tooltip_text("From the last texture check; the files are listed below.")
        self.chips.append(c)

    def _render_texture_problems(self, fn: str) -> None:
        clear_box(self.textures_box)
        problems = self.view.texture_problems.get(fn) or []
        if not problems:
            return
        self.textures_box.append(label(f"TEXTURE PROBLEMS ({len(problems)})", "vb-section-label"))
        for p in problems[:200]:
            row = vbox(1)
            row.add_css_class("vb-dep-row")
            head = hbox(6)
            path = label(p["path"], "vb-mono", selectable=True)
            path.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            path.set_hexpand(True)
            path.set_tooltip_text(p["path"])
            head.append(path)
            bad = p["severity"] == "error"
            head.append(chip("CANNOT LOAD" if bad else "SUSPECT", "error" if bad else "warn"))
            row.append(head)
            row.append(label(p["message"], "vb-dim vb-small", wrap=True))
            self.textures_box.append(row)

    @staticmethod
    def _placeholder_text(fn: str, d: dict) -> str:
        ref = fn[:-4] if fn.lower().endswith(".var") else fn
        if d.get("is_dummy"):
            return (
                f"A stand-in: an empty package named {ref} so VaM stops reporting it as missing. Nothing is inside. "
                "If you find the real package (the author's Patreon or Discord, an old download), drop it anywhere in "
                "AddonPackages or use Replace with file…: the stand-in is removed automatically."
            )
        return (
            f"A stand-in: a copy (hard link) of the version of {d['package_name']} you have, under the name {ref}, "
            "so files referenced through this version come from the one you have. Drop the real file into AddonPackages "
            "or use Replace with file… to swap it for the real thing."
        )

    def _placeholder_actions(self, fn: str, d: dict) -> None:
        ref = fn[:-4] if fn.lower().endswith(".var") else fn
        creator, _, rest = d["package_name"].partition(".")
        self._button(
            "Search the Hub",
            lambda: self.window.search_hub(rest or creator),
            tooltip="Search the Hub for this package: the author may have re-uploaded it, or a newer version may do.",
        )
        self._button(
            "Copy name",
            lambda: self._copy_name(ref),
            tooltip="Copy the package name for searching elsewhere (Patreon, Discord).",
        )
        self._button(
            "Fetch from Hub",
            lambda: self.window.run_op(
                self.backend.downloads.enqueue_reinstall,
                fn,
                then=lambda _r: self.window.toast(
                    f"Asked the Hub for {fn}; the stand-in is replaced if it serves the file"
                ),
            ),
            tooltip="Ask the Hub for this exact file now; if it serves it, the download replaces the stand-in.",
        )
        self._button(
            "Replace with file…",
            lambda: self._replace_placeholder(fn),
            tooltip="Pick the real .var: it replaces the stand-in and keeps its role and settings.",
        )

    def _unpack_actions(self, d: dict) -> None:
        fn = d["filename"]
        if d.get("is_unpacked"):
            self._button(
                "Edit files…",
                self._edit_files,
                tooltip="Browse the unpacked folder and edit its JSON files (meta.json, scenes, presets) as a tree, "
                "so the syntax cannot break: drop a bogus dependency, fix a reference, remove a broken entry.",
            )
            self._button(
                "Repack",
                self._repack,
                tooltip=f"Build {fn} again from the folder, with every edit made there, and remove the folder.",
            )
        elif not d["is_corrupted"]:
            self._button(
                "Unpack",
                self._unpack,
                tooltip="Extract the archive into a folder of the same name, as Unpack in VaM's Package Manager does; "
                f"the archive is kept beside it as {fn}.orig. Unpacked, its files can be edited here.",
            )
        if d.get("has_backup"):
            self._button(
                "Restore original",
                self._restore_original,
                tooltip=f"Put {fn}.orig, the archive as it was before unpacking, back in place of the "
                + ("folder" if d.get("is_unpacked") else "current archive")
                + ".",
            )

    def _unpack(self):
        d = self.detail
        if not d:
            return
        fn = d["filename"]
        body = (
            f"{fn} is extracted into a folder of the same name, which VaM loads just like the archive (this is what "
            f"Unpack in VaM's Package Manager does). The archive stays beside it as {fn}.orig, so the original can be "
            "restored at any time. Once unpacked, its JSON files (meta.json, scenes, presets) can be edited here; "
            "Repack builds a new archive from the folder."
        )
        if d.get("has_backup"):
            body += (
                f"\n\nA backup {fn}.orig already exists and stays the original; the current archive is replaced by "
                "the folder holding its contents."
            )
        body += f"\n\nSize: {human_size(d['size_bytes'])}."
        self.window.confirm(
            "Unpack package?",
            body,
            "Unpack",
            lambda: self.window.run_op(
                self.backend.lib.unpack_package,
                fn,
                then=lambda r: self.window.toast(f"Unpacked {fn}: {r['files']} files ({human_size(r['bytes'])})", 6),
            ),
            destructive=False,
        )

    def _repack(self):
        d = self.detail
        if not d:
            return
        fn = d["filename"]
        dialog = Adw.AlertDialog(
            heading="Repack package?",
            body=f"A new {fn} is built from the folder's files, with every edit made there, and the folder is removed.",
        )
        keep = Gtk.CheckButton(label=f"Keep the original archive as {fn}.orig")
        keep.set_tooltip_text(
            "Otherwise the backup from unpacking is deleted, as VaM does after a repack: the new archive is the package. "
            "Kept, Restore original can still bring the pre-edit archive back."
        )
        if d.get("has_backup"):
            dialog.set_extra_child(keep)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("repack", "Repack")
        dialog.set_response_appearance("repack", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("repack")
        dialog.set_close_response("cancel")

        def on_response(_d, r):
            if r != "repack":
                return
            self.window.run_op(
                self.backend.lib.repack_package,
                fn,
                bool(d.get("has_backup")) and keep.get_active(),
                then=lambda res: self.window.toast(
                    f"Repacked {fn}: {res['files']} files, {human_size(res['bytes'])}"
                    + ("; the backup was removed" if res["backup_removed"] else ""),
                    6,
                ),
            )

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _restore_original(self):
        d = self.detail
        if not d:
            return
        fn = d["filename"]
        if d.get("is_unpacked"):
            body = (
                f"The folder {fn} is deleted and the backup {fn}.orig takes its place as the package, "
                "so every change made since unpacking is lost."
            )
        else:
            body = f"The current {fn} is replaced by the backup {fn}.orig, the archive as it was before unpacking."
        self.window.confirm(
            "Restore the original archive?",
            body,
            "Restore",
            lambda: self.window.run_op(
                self.backend.lib.restore_original,
                fn,
                then=lambda _r: self.window.toast(f"Restored the original {fn}", 6),
            ),
        )

    def _edit_files(self):
        d = self.detail
        if not d:
            return
        fn = d["filename"]
        has_backup = bool(d.get("has_backup"))

        def opened(folder):
            from .package_editor import PackageEditorDialog

            PackageEditorDialog(self.window, fn, folder, has_backup).present(self.window)

        self.window.run_op(self.backend.lib.unpacked_folder, fn, then=opened)

    def _copy_name(self, text: str) -> None:
        from .launch import copy_to_clipboard

        self.window.toast(f"Copied {text}" if copy_to_clipboard(text) else f"Clipboard unavailable; the name is {text}")

    def _replace_placeholder(self, fn: str) -> None:
        dialog = Gtk.FileDialog(title=f"Pick the real {fn}")
        flt = Gtk.FileFilter()
        flt.set_name("VaM packages (*.var)")
        flt.add_pattern("*.var")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)
        dialog.set_default_filter(flt)

        def finish(dlg, result):
            try:
                f = dlg.open_finish(result)
            except GLib.Error as err:
                if not err.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                    self.window.toast(f"Could not open the file chooser: {err.message}", 8)
                return
            path = f.get_path()
            if not path:
                self.window.toast("That is not a local file", 6)
                return
            self.window.run_op(
                self.backend.lib.replace_placeholder,
                fn,
                path,
                then=lambda r: self.window.toast(f"{fn}: the real package replaced the {r['replaced']}"),
            )

        dialog.open(self.window, None, finish)

    def _render_deps(self, nodes: list[dict], depth: int, seen: set[str]):
        for node in nodes:
            self.deps_box.append(
                DepRow(
                    node,
                    depth,
                    self.view.select_filename,
                    strict=self.backend.lib.store.strict_versions,
                    on_dummy=self._make_dummy,
                )
            )
            key = node.get("resolved")
            if node.get("children") and key not in seen and depth < 6:
                seen.add(key)
                self._render_deps(node["children"], depth + 1, seen)

    def _content_row(self, c: dict) -> Gtk.Box:
        row = hbox(6)
        row.add_css_class("vb-dep-row")
        name = label(c["display_name"], "vb-small")
        name.set_hexpand(True)
        name.set_tooltip_text(c["internal_path"])
        if c.get("hidden"):
            name.add_css_class("vb-dim")
        row.append(name)
        if c.get("tag"):
            row.append(chip(c["tag"]["label"].upper(), "muted"))
        eye = icon_button(
            "view-conceal-symbolic" if c["hidden"] else "view-reveal-symbolic",
            "Show in VaM" if c["hidden"] else "Hide in VaM",
            lambda: self._toggle_hidden(c),
        )
        star = icon_button(
            "starred-symbolic" if c["favorite"] else "non-starred-symbolic",
            "Remove favorite" if c["favorite"] else "Mark favorite",
            lambda: self._toggle_fav(c),
        )
        row.append(eye)
        row.append(star)
        return row

    def _toggle_enabled(self):
        d = self.detail
        if not d:
            return
        cascade = d.get("cascade_disable_deps") or []
        if d["storage_state"] == "enabled" and cascade:
            names = ", ".join(x["name"] for x in cascade[:5]) + ("…" if len(cascade) > 5 else "")
            self.window.confirm(
                "Disable package?",
                f"{len(cascade)} dependenc{'y' if len(cascade) == 1 else 'ies'} only this package needs will be disabled too: {names}",
                "Disable",
                lambda: self.window.run_op(self.backend.lib.toggle_enabled, d["filename"]),
                destructive=False,
            )
        else:
            self.window.run_op(self.backend.lib.toggle_enabled, d["filename"])

    def _promote(self):
        self.window.run_op(self.backend.lib.promote, self.filename, success="Marked as installed directly")

    def _demote(self):
        self.window.run_op(self.backend.lib.demote, self.filename, success="Marked as dependency")

    def _enable_deps(self):
        self.window.run_op(self.backend.lib.enable_deps, self.filename)

    def _set_keep(self, keep: bool):
        self.window.run_op(
            self.backend.lib.set_keep_version,
            self.filename,
            keep,
            success="Kept: it stays out of Old versions" if keep else "No longer kept",
        )

    def _install_missing(self):
        def done(r):
            unresolved = r.get("unresolved_deps") or []
            already = r.get("already_installed") or []
            if not r["queued"] and (unresolved or already):
                parts = []
                if already:
                    parts.append(
                        f"the Hub's version of {', '.join(already[:3])} is already installed but a different version is named"
                    )
                if unresolved:
                    parts.append(f"the Hub has no {', '.join(unresolved[:3])}")
                self.window.toast(
                    "Nothing queued: " + "; ".join(parts) + ". Use Make dummy on that dependency row.", 10
                )
            else:
                self.window.toast(
                    f"Queued {r['queued']} download(s)" + (f", {len(unresolved)} not on the Hub" if unresolved else "")
                )

        self.window.run_op(self.backend.downloads.enqueue_install_missing, self.filename, then=done)

    def _uninstall(self):
        d = self.detail
        if not d:
            return
        fn = d["filename"]
        dependents = [x.get("filename", x) if isinstance(x, dict) else str(x) for x in d["dependents"]]
        if dependents:
            shown = ", ".join(dependents[:6]) + (f" and {len(dependents) - 6} more" if len(dependents) > 6 else "")
            body = (
                f"{fn} is a dependency of {len(dependents)} package{'s' if len(dependents) != 1 else ''}: {shown}.\n\n"
            )
            body += (
                "Marking it as a dependency keeps the file and hides its content from VaM. "
                if d["is_direct"]
                else "It is already marked as a dependency. "
            )
            body += "Deleting it anyway leaves those packages with a missing dependency."
            dialog = Adw.AlertDialog(heading="Still needed by other packages", body=body)
            dialog.add_response("cancel", "Cancel")
            if d["is_direct"]:
                dialog.add_response("demote", "Mark as dependency")
                dialog.set_response_appearance("demote", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_default_response("demote")
            dialog.add_response("delete", "Delete anyway")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_close_response("cancel")

            def on_response(_d, r):
                if r == "demote":
                    self.window.run_op(self.backend.lib.uninstall, fn, then=self._uninstall_done)
                elif r == "delete":
                    self.window.run_op(
                        self.backend.lib.force_remove,
                        [fn],
                        then=lambda _r: self.window.toast(
                            f"Deleted {fn}; {len(dependents)} package(s) now have a missing dependency", 6
                        ),
                    )

            dialog.connect("response", on_response)
            dialog.present(self.window)
            return
        extra = ""
        if d["removable_deps"]:
            n = len(d["removable_deps"])
            extra = f" {n} dependenc{'y' if n == 1 else 'ies'} nothing else needs will be removed too when the Hub can restore them ({human_size(d['removable_size'])})."
        self.window.confirm(
            "Uninstall package?",
            f"{fn} will be deleted from disk.{extra}",
            "Uninstall",
            lambda: self.window.run_op(self.backend.lib.uninstall, fn, then=self._uninstall_done),
        )

    def _uninstall_done(self, result: dict) -> None:
        r = (result.get("results") or [{}])[0]
        if r.get("deleted"):
            n = r["deleted"] - 1
            self.window.toast(
                f"Deleted {r['filename']}"
                + (f" and {n} dependenc{'y' if n == 1 else 'ies'} nothing else needed" if n else ""),
                6,
            )
        elif r.get("demoted"):
            n = len(r.get("needed_by") or [])
            self.window.toast(
                (
                    f"Kept as a dependency: {n} package(s) still need it"
                    if not r.get("already_dependency")
                    else f"Not removed: {n} package(s) still need it"
                ),
                6,
            )
        elif r.get("relocated_to_archive"):
            self.window.toast(f"Moved {r['filename']} to the archive", 6)
        else:
            self.window.toast("Uninstalled")

    def _make_dummy(self, ref: str):
        from ..core.dummy import DUMMIES_SUBDIR, dummy_filename_for_ref
        from ..core.graph import parse_dep_ref

        parsed = parse_dep_ref(ref)
        name = dummy_filename_for_ref(parsed) if parsed else ref
        installed = sorted(self.backend.lib.store.group_index.get(parsed.package_name, [])) if parsed else []
        near = self.backend.lib.store.near_matches(parsed) if parsed and not installed else []
        hub_entry = (
            self.backend.packages_json.index.get(parsed.package_name)
            if parsed and self.backend.packages_json.index
            else None
        )
        hub_newer = (
            hub_entry if hub_entry and hub_entry.get("filename") not in self.backend.lib.store.package_index else None
        )
        body = (
            f"An empty package {name} written to AddonPackages/{DUMMIES_SUBDIR} makes VaM stop reporting {ref} as missing. "
            "It has no content, so anything that actually loads files from it will still fail. "
            "If the real package turns up on the Hub later, Reinstall replaces the dummy."
        )
        if hub_newer:
            body = (
                f"The Hub no longer has {ref} but it has v{hub_newer.get('version')} of the package, which often works. "
                "Installing that is the first thing to try.\n\n"
            ) + body
        if near:
            body = (
                f"You have a package with the same name under another creator name: {near[0]}. That may be the same package "
                "renamed by whoever passed it on, or someone else's fork with the same name; VaM matches the full name either way. "
                f"If it is the same thing, an alias (a hard-linked copy of {near[0]} named {name}) makes the reference resolve. "
                "If you are not sure, a dummy is the honest choice.\n\n"
            ) + body
        best = installed[-1] if installed else None
        if best:
            body += (
                f"\n\nYou have {', '.join(installed[:3])} installed. An alias is a copy of {best} under the name {name} "
                "(a hard link, no extra space), so files referenced through the missing version come from the one you have. "
                "A dummy instead leaves those references empty; it declares the installed version as its dependency so it is not orphaned."
            )
        dialog = Adw.AlertDialog(heading="Dependency not available", body=body)
        dialog.add_response("cancel", "Cancel")
        if hub_newer:
            dialog.add_response("install", f"Install v{hub_newer.get('version')} from Hub")
            dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("install")
        if best:
            dialog.add_response(
                "alias",
                f"Alias {best[:-4].rsplit('.', 1)[-1] and 'v' + best[:-4].rsplit('.', 1)[-1]} as {name[:-4].rsplit('.', 1)[-1] and 'v' + name[:-4].rsplit('.', 1)[-1]}",
            )
            if not hub_newer:
                dialog.set_response_appearance("alias", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_default_response("alias")
        elif near:
            dialog.add_response("alias-near", f"Alias {near[0][:-4]} as {name[:-4]}")
        dialog.add_response("dummy", "Create dummy")
        if not installed and not near and not hub_newer:
            dialog.set_response_appearance("dummy", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("dummy")
        dialog.set_close_response("cancel")
        parent = self.filename

        def on_response(_d, r):
            if r == "install":
                self.window.run_op(
                    self.backend.downloads.enqueue_install_refs,
                    [ref],
                    parent,
                    then=lambda res: self.window.toast(
                        (
                            f"Queued {res['queued']} download(s)"
                            if res["queued"]
                            else "The Hub did not serve it after all"
                        ),
                        6,
                    ),
                )
            elif r == "alias":
                self.window.run_op(self.backend.lib.create_aliases, [ref], then=self.view.report_aliases)
            elif r == "alias-near":
                self.window.run_op(
                    self.backend.lib.create_aliases, [ref], {ref: near[0]}, then=self.view.report_aliases
                )
            elif r == "dummy":
                self.window.run_op(
                    self.backend.lib.create_dummies, [ref], bool(installed), then=self.view.report_dummies
                )

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _reinstall(self):
        d = self.detail
        if not d:
            return
        fn = d["filename"]
        self.window.confirm(
            "Download again from the Hub?",
            f"{fn} will be downloaded again and the local file replaced once the download verifies. "
            "Its folder, enabled state, settings and hidden/favourite marks are kept."
            + (
                " It is currently unpacked (a folder); the download replaces the folder with the archive."
                if d.get("is_unpacked")
                else ""
            ),
            "Reinstall",
            lambda: self.window.run_op(
                self.backend.downloads.enqueue_reinstall,
                fn,
                then=lambda _r: self.window.toast(f"Queued re-download of {fn}"),
            ),
            destructive=False,
        )

    def _open_folder(self):
        path = self.backend.lib.package_path(self.filename or "")
        if path:
            open_in_file_manager(path)

    def _toggle_hidden(self, c: dict):
        self.window.run_op(self.backend.lib.toggle_hidden, c["package_filename"], c["internal_path"])

    def _toggle_fav(self, c: dict):
        self.window.run_op(self.backend.lib.toggle_favorite, c["package_filename"], c["internal_path"])

    def _set_hidden(self, items: list[dict], hidden: bool):
        self.window.run_op(
            self.backend.lib.set_hidden_batch, [(c["package_filename"], c["internal_path"]) for c in items], hidden
        )
