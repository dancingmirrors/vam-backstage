from __future__ import annotations

import logging
from collections import Counter

from gi.repository import Gio, Gtk

from ..core.content_types import VISIBLE_CATEGORIES
from .dispatch import Debouncer, run_async
from .models import DictItem, sync_store
from .widgets import (
    ContentCard,
    DepRow,
    FilterList,
    Thumb,
    category_chip,
    chip,
    clear_box,
    detail_panel,
    hbox,
    icon_button,
    label,
    scroll_to_top,
    vbox,
    view_blurb,
)

log = logging.getLogger(__name__)

VISIBILITY_OPTIONS = [
    (None, "All items"),
    ("visible", "Visible in VaM"),
    ("hidden", "Hidden in VaM"),
    ("favorites", "Favorites"),
]
PKG_STATE_OPTIONS = [
    ("enabled", "Enabled packages"),
    (None, "All packages"),
    ("disabled", "Disabled packages"),
    ("archived", "Archived packages"),
]


class ContentView(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window = window
        self.backend = window.backend
        self.loader = window.app.textures
        self.items: list[dict] = []
        self.search_text = ""
        self.category: str | None = None
        self.visibility: str | None = None
        self.pkg_state: str | None = "enabled"
        self.package_filter: str | None = None
        self.deps_filter: str | None = None
        self._pending_select: str | None = None
        self._bound_cards: dict[str, ContentCard] = {}
        self._loaded_once = False

        side = vbox(2)
        side.add_css_class("vb-filter-panel")
        side.set_margin_start(10)
        side.set_margin_end(10)
        side.set_margin_top(8)
        self.search = Gtk.SearchEntry(placeholder_text="Search your scenes, looks, poses…")
        self.search.connect("search-changed", self._on_search)
        side.append(self.search)
        side.append(
            view_blurb(
                "What is inside your packages and Saves folders: scenes, looks, poses, clothing and hair, "
                "and whether VaM shows or hides each one. The Hub tab searches what is not installed yet."
            )
        )
        self.pkg_chip = hbox(4)
        self.pkg_chip.set_visible(False)
        self.pkg_chip_label = label("", "vb-small")
        self.pkg_chip.append(self.pkg_chip_label)
        self.pkg_chip.append(icon_button("window-close-symbolic", "Clear package filter", self._clear_package))
        side.append(self.pkg_chip)
        self.category_list = FilterList("Category", self._on_category, 20)
        self.visibility_list = FilterList("Visibility", self._on_visibility, 20)
        self.deps_list = FilterList("Dependencies", self._on_deps, 20)
        self.state_list = FilterList("Package state", self._on_state, 20)
        side.append(self.category_list)
        side.append(self.visibility_list)
        side.append(self.deps_list)
        side.append(self.state_list)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=side)
        side_scroll.set_size_request(250, -1)
        self.append(side_scroll)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        center = vbox(0)
        center.set_hexpand(True)
        bar = hbox(8)
        for m in ("start", "end"):
            getattr(bar, f"set_margin_{m}")(12)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        self.count_label = label("", "vb-dim")
        bar.append(self.count_label)
        bar.append(Gtk.Box(hexpand=True))
        self.hide_btn = Gtk.Button(label="Hide all shown")
        self.hide_btn.add_css_class("flat")
        self.hide_btn.connect("clicked", lambda *_: self._bulk_hidden(True))
        self.show_btn = Gtk.Button(label="Show all shown")
        self.show_btn.add_css_class("flat")
        self.show_btn.connect("clicked", lambda *_: self._bulk_hidden(False))
        bar.append(self.show_btn)
        bar.append(self.hide_btn)
        center.append(bar)

        self.store = Gio.ListStore.new(DictItem)
        self.filter = Gtk.CustomFilter.new(self._match)
        self.filtered = Gtk.FilterListModel.new(self.store, self.filter)
        self.selection = Gtk.SingleSelection.new(self.filtered)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda f, it: it.set_child(ContentCard(self.loader)))
        factory.connect("bind", self._bind_card)
        factory.connect("unbind", self._unbind_card)
        self.grid = Gtk.GridView.new(self.selection, factory)
        self.grid.set_max_columns(14)
        self.grid.set_margin_start(8)
        self.grid.set_margin_end(8)
        self.selection.connect("notify::selected-item", self._on_selected)
        self.grid_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid, vexpand=True)
        center.append(self.grid_scroll)
        self.empty = vbox(10, visible=False, vexpand=True, valign=Gtk.Align.CENTER)
        self.empty_text = label("Nothing in your library matches.", "vb-dim", xalign=0.5)
        self.empty_text.set_halign(Gtk.Align.CENTER)
        self.empty.append(self.empty_text)
        clear_btn = Gtk.Button(label="Clear search and filters", halign=Gtk.Align.CENTER)
        clear_btn.add_css_class("pill")
        clear_btn.connect("clicked", lambda *_: self.clear_filters())
        self.empty.append(clear_btn)
        self.hub_search_btn = Gtk.Button(label="Search the Hub", halign=Gtk.Align.CENTER, visible=False)
        self.hub_search_btn.add_css_class("pill")
        self.hub_search_btn.connect("clicked", lambda *_: self.window.search_hub(self.search.get_text().strip()))
        self.empty.append(self.hub_search_btn)
        center.append(self.empty)
        self.append(center)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.detail = ContentDetail(self)
        self.detail_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=detail_panel(self.detail, self.clear_selection)
        )
        self.detail_scroll.set_size_request(330, -1)
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

        self._stale = True
        self._refresh_debounce = Debouncer(200, self.refresh, 3000, lambda: self.backend.downloads.busy)
        window.bridge.connect("contents:updated", lambda _p: self._refresh_debounce.trigger())
        window.bridge.connect("packages:updated", lambda _p: self._refresh_debounce.trigger())
        self.visibility_list.set_options([(v, t, None) for v, t in VISIBILITY_OPTIONS], None)
        self.state_list.set_options([(v, t, None) for v, t in PKG_STATE_OPTIONS], "enabled")

    def on_shown(self):
        if self._stale:
            self.refresh()

    def refresh_soon(self):
        self._refresh_debounce.hurry()

    def refresh(self):
        if not self.backend.lib.is_configured:
            return
        self._stale = self.window.stack.get_visible_child() is not self
        if self._stale:
            return
        self._loaded_once = True

        run_async(self.backend.lib.list_contents, on_done=self._loaded)

    def _loaded(self, items):
        self.items = items
        type_counts = Counter(c["category"] for c in items)
        hidden = sum(1 for c in items if c["hidden"])
        vis = {
            "all": len(items),
            "visible": len(items) - hidden,
            "hidden": hidden,
            "favorites": sum(1 for c in items if c["favorite"]),
        }
        selected = self.selected_key()
        self.by_key, changed = sync_store(
            self.store, items, lambda c: f"{c['package_filename']}\0{c['internal_path']}", getattr(self, "by_key", {})
        )
        for item in changed:
            card = self._bound_cards.get(item.key)
            if card is not None:
                card.bind(item.data)
        cats = [(None, "All categories", len(items))] + [
            (c, c, type_counts.get(c, 0)) for c in VISIBLE_CATEGORIES if type_counts.get(c)
        ]
        self.category_list.set_options(cats, self.category)
        self.visibility_list.set_options(
            [
                (None, "All items", vis["all"]),
                ("visible", "Visible in VaM", vis["visible"]),
                ("hidden", "Hidden in VaM", vis["hidden"]),
                ("favorites", "Favorites", vis["favorites"]),
            ],
            self.visibility,
        )
        n_missing = sum(1 for c in items if c.get("missing_refs"))
        if not n_missing and self.deps_filter:
            self.deps_filter = None
            self.filter.changed(Gtk.FilterChange.LESS_STRICT)
        self.deps_list.set_options(
            [(None, "All items", len(items)), ("missing", "Missing something", n_missing)], self.deps_filter
        )
        self._update_count()
        pending, self._pending_select = self._pending_select, None
        for want in (pending, selected):
            if not want or (want is not pending and self.selected_key() == want):
                continue
            for i in range(self.filtered.get_n_items()):
                if self.filtered.get_item(i).key == want:
                    self.selection.set_selected(i)
                    if want is pending:
                        self.grid.scroll_to(i, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                    break
            else:
                continue
            break
        self.detail.refresh()

    def _bind_card(self, _factory, list_item):
        item = list_item.get_item()
        list_item.get_child().bind(item.data)
        self._bound_cards[item.key] = list_item.get_child()

    def _unbind_card(self, _factory, list_item):
        key = list_item.get_item().key
        if self._bound_cards.get(key) is list_item.get_child():
            del self._bound_cards[key]

    def select_item(self, package_filename: str, internal_path: str) -> None:
        key = f"{package_filename}\0{internal_path}"
        self._pending_select = key
        if self._loaded_once and key in getattr(self, "by_key", {}):
            self._loaded(self.items)

    def _update_count(self):
        n = self.filtered.get_n_items()
        self.count_label.set_text(f"{n} of {len(self.items)} items")
        self.empty.set_visible(n == 0 and len(self.items) > 0)
        self.grid_scroll.set_visible(n > 0 or len(self.items) == 0)
        q = self.search.get_text().strip()
        self.empty_text.set_text(
            f"Nothing installed matches “{q}”." if q else "Nothing in your library matches these filters."
        )
        self.hub_search_btn.set_visible(bool(q))
        self.hub_search_btn.set_label(f"Search the Hub for “{q}”" if q else "Search the Hub")

    def clear_filters(self):
        self.category = self.visibility = self.deps_filter = None
        self.deps_list.select(None)
        self.pkg_state = "enabled"
        self.search.set_text("")
        self.search_text = ""
        self.category_list.select(None)
        self.visibility_list.select(None)
        self.state_list.select("enabled")
        self.filter_package(None)

    def _match(self, item, *_):
        c = item.data
        if self.package_filter and c["package_filename"] != self.package_filter:
            return False
        if self.category and c["category"] != self.category:
            return False
        if self.visibility == "visible" and c["hidden"]:
            return False
        if self.visibility == "hidden" and not c["hidden"]:
            return False
        if self.visibility == "favorites" and not c["favorite"]:
            return False
        if self.pkg_state and c["storage_state"] != self.pkg_state:
            return False
        if self.deps_filter == "missing" and not c.get("missing_refs"):
            return False
        if self.search_text:
            hay = f"{c['display_name']} {c['creator']} {c['package_name']} {c['internal_path']}".lower()
            if not all(t in hay for t in self.search_text.split()):
                return False
        return True

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_count()

    def _on_search(self, entry):
        self.search_text = entry.get_text().strip().lower()
        self._refilter()

    def _on_category(self, v):
        self.category = v
        self._refilter()

    def _on_visibility(self, v):
        self.visibility = v
        self._refilter()

    def _on_state(self, v):
        self.pkg_state = v
        self._refilter()

    def _on_deps(self, v):
        self.deps_filter = v
        self._refilter()

    def filter_package(self, filename: str | None):
        self.package_filter = filename
        self.pkg_chip.set_visible(bool(filename))
        self.pkg_chip_label.set_text(f"Package: {filename}" if filename else "")
        if filename:
            self.pkg_state = None
            self.state_list.select(None)
        if not self._loaded_once:
            self.refresh()
        else:
            self._refilter()

    def _clear_package(self):
        self.filter_package(None)

    def selected_key(self):
        item = self.selection.get_selected_item()
        return item.key if item else None

    def clear_selection(self) -> None:
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def _on_selected(self, *_):
        item = self.selection.get_selected_item()
        self.detail_scroll.set_visible(item is not None)
        self.detail.show(item.data if item else None)
        if item:
            scroll_to_top(self.detail_scroll)

    def _shown_items(self) -> list[dict]:
        return [self.filtered.get_item(i).data for i in range(self.filtered.get_n_items())]

    def _bulk_hidden(self, hidden: bool):
        items = self._shown_items()
        if not items:
            return
        pairs = [(c["package_filename"], c["internal_path"]) for c in items]
        self.window.confirm(
            f"{'Hide' if hidden else 'Show'} {len(pairs)} item(s) in VaM?",
            "Only the .hide marker files VaM reads are written; nothing else changes.",
            "Hide" if hidden else "Show",
            lambda: self.window.run_op(self.backend.lib.set_hidden_batch, pairs, hidden),
            destructive=False,
        )


class ContentDetail(Gtk.Box):
    def __init__(self, view: ContentView):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.view = view
        self.item: dict | None = None
        for m in ("start", "end"):
            getattr(self, f"set_margin_{m}")(14)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.thumb = Thumb(300, 300, "vb-content-thumb", follow_image=True)
        self.append(self.thumb)
        self.title = label("", "vb-detail-title", wrap=True)
        self.title.set_selectable(True)
        self.append(self.title)
        self.chips = hbox(4)
        self.append(self.chips)
        self.path = label("", "vb-mono vb-dim", wrap=True)
        self.path.set_selectable(True)
        self.append(self.path)
        row = hbox(6)
        self.hide_btn = Gtk.Button()
        self.hide_btn.connect("clicked", lambda *_: self._toggle("hidden"))
        self.fav_btn = Gtk.Button()
        self.fav_btn.connect("clicked", lambda *_: self._toggle("favorite"))
        row.append(self.hide_btn)
        row.append(self.fav_btn)
        self.append(row)
        self.owner_box = vbox(4)
        self.append(self.owner_box)
        self.refs_box = vbox(2)
        self.append(self.refs_box)

    def show(self, item: dict | None):
        self.item = item
        self.refresh()

    def refresh(self):
        c = self.item
        if not c:
            return
        key = f"{c['package_filename']}\0{c['internal_path']}"
        fresh = self.view.by_key.get(key) if hasattr(self.view, "by_key") else None
        if fresh:
            c = self.item = fresh.data
        self.thumb.load(
            self.view.loader, f"ct:{c['package_filename']}\0{c['thumbnail_path']}" if c.get("thumbnail_path") else None
        )
        self.title.set_text(c["display_name"])
        clear_box(self.chips)
        self.chips.append(category_chip(c.get("category")))
        if c.get("tag"):
            self.chips.append(chip(c["tag"]["label"].upper(), "muted"))
        if c["hidden"]:
            self.chips.append(chip("HIDDEN", "muted"))
        if c["favorite"]:
            self.chips.append(chip("FAVORITE", "success"))
        if c["storage_state"] != "enabled":
            self.chips.append(chip(c["storage_state"].upper(), "warn"))
        self.path.set_text(c["internal_path"])
        self.hide_btn.set_label("Show in VaM" if c["hidden"] else "Hide in VaM")
        self.fav_btn.set_label("Unfavorite" if c["favorite"] else "Favorite")
        clear_box(self.owner_box)
        if c["package_filename"] == "__local__":
            self.owner_box.append(label("Loose file in your VaM folder (not from a package).", "vb-dim", wrap=True))
        else:
            self.owner_box.append(label("PACKAGE", "vb-section-label"))
            self.owner_box.append(label(f"{c.get('package_title') or c['package_name']} · {c['creator']}", wrap=True))
            self.owner_box.append(label(c["package_filename"], "vb-mono vb-dim", wrap=True))
            b = Gtk.Button(label="Show in Library")
            b.set_halign(Gtk.Align.START)
            b.connect("clicked", lambda *_: self.view.window.select_package(c["package_filename"]))
            self.owner_box.append(b)
            if not c["is_direct"]:
                self.owner_box.append(label("This item came with a dependency package.", "vb-dim vb-small", wrap=True))
        self._render_refs(c)

    def _render_refs(self, c: dict) -> None:
        clear_box(self.refs_box)
        strict = self.view.backend.lib.store.strict_versions
        make_dummy = self.view.window.library.detail._make_dummy
        if c["package_filename"] == "__local__":
            nodes = c.get("refs") or []
            if not nodes:
                return
            missing = sum(1 for n in nodes if n.get("missing"))
            self.refs_box.append(
                label(
                    f"REFERENCED PACKAGES ({len(nodes)})" + (f" · {missing} MISSING" if missing else ""),
                    "vb-section-label",
                )
            )
            for n in sorted(nodes, key=lambda n: (not n.get("missing"), n["ref"].lower())):
                self.refs_box.append(DepRow(n, 0, self.view.window.select_package, strict=strict, on_dummy=make_dummy))
            b = Gtk.Button(label="Reinstall what it uses…")
            b.set_halign(Gtk.Align.START)
            b.set_tooltip_text(
                "Download the packages this file refers to, and what they depend on, again at the versions it "
                "names: a version it asks for that you do not have is added next to the one you have. "
                "Shows the plan before anything is downloaded."
            )
            b.connect(
                "clicked",
                lambda *_: self.view.window.reinstall_with_deps(
                    local_paths=[c["internal_path"]], heading=f"Reinstall what {c['display_name']} uses?"
                ),
            )
            self.refs_box.append(b)
            return
        missing = c.get("missing_refs") or []
        if missing:
            self.refs_box.append(label(f"ITS PACKAGE IS MISSING ({len(missing)})", "vb-section-label"))
            for ref in missing:
                self.refs_box.append(
                    DepRow(
                        {"ref": ref, "resolved": None, "resolution": "missing"},
                        0,
                        None,
                        strict=strict,
                        on_dummy=make_dummy,
                    )
                )

    def _toggle(self, field: str):
        c = self.item
        if not c:
            return
        fn = self.view.backend.lib.toggle_hidden if field == "hidden" else self.view.backend.lib.toggle_favorite
        self.view.window.run_op(fn, c["package_filename"], c["internal_path"])
