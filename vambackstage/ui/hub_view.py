from __future__ import annotations

import logging
import time

from gi.repository import Adw, Gio, GLib, Gtk

from ..core.licenses import LICENSE_FILTER_OPTIONS, hub_resource_license
from ..hub.client import HUB_PER_PAGE, hub_resource_page_url
from .dispatch import Debouncer, run_async
from .launch import open_url
from .models import DictItem, human_size
from .widgets import DepRow, Thumb, chip, clear_box, detail_panel, hbox, label, scroll_to_top, vbox, view_blurb

log = logging.getLogger(__name__)

HUB_TYPE_CLASS = {
    "Scenes": "scenes",
    "Looks": "looks",
    "Clothing": "clothing",
    "Hairstyles": "hairstyles",
    "Poses": "poses",
}


def _i(v) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


class HubCard(Gtk.Box):
    def __init__(self, loader):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.loader = loader
        self.rid: str | None = None
        self.installed: dict | None = None
        self.bookmarked = False
        self.add_css_class("vb-card")
        self.add_css_class("vb-hub-card")
        self.set_size_request(190, -1)
        self.thumb = Thumb(190, 142)
        self.badges = hbox(4)
        self.badges.set_halign(Gtk.Align.START)
        self.badges.set_valign(Gtk.Align.START)
        self.badges.set_margin_start(6)
        self.badges.set_margin_top(6)
        self.thumb.add_overlay(self.badges)
        self.state = hbox(4)
        self.state.set_halign(Gtk.Align.END)
        self.state.set_valign(Gtk.Align.END)
        self.state.set_margin_end(6)
        self.state.set_margin_bottom(6)
        self.thumb.add_overlay(self.state)
        self.append(self.thumb)
        body = vbox(2)
        body.add_css_class("vb-card-body")
        self.title = label("", "vb-card-title")
        self.sub = label("", "vb-card-sub")
        self.meta = label("", "vb-card-sub")
        body.append(self.title)
        body.append(self.sub)
        body.append(self.meta)
        self.append(body)

    def bind(self, r: dict, installed: dict | None, bookmarked: bool = False):
        self.title.set_text(r.get("title") or "")
        self.sub.set_text(f"{r.get('username') or '?'} · v{r.get('version_string') or '?'}")
        self.meta.set_text(f"{_i(r.get('download_count'))} downloads · {r.get('category') or ''}")
        clear_box(self.badges)
        t = r.get("type") or "Other"
        self.badges.append(chip(t.upper(), HUB_TYPE_CLASS.get(t, "other")))
        if r.get("category") and r["category"] != "Free":
            self.badges.append(chip(r["category"].upper(), "warn"))
        self.installed = installed
        self.bookmarked = bookmarked
        self._render_state()
        rid = r.get("resource_id")
        self.thumb.load(self.loader, f"hub-icon:{rid}" if rid else None)

    def set_installed(self, installed: dict | None) -> None:
        self.installed = installed
        self._render_state()

    def set_bookmarked(self, bookmarked: bool) -> None:
        self.bookmarked = bookmarked
        self._render_state()

    def _render_state(self) -> None:
        clear_box(self.state)
        installed = self.installed
        if installed:
            self.state.append(
                chip(
                    "INSTALLED" if installed.get("is_direct") else "DEPENDENCY",
                    "success" if installed.get("is_direct") else "dep",
                )
            )
        if self.bookmarked:
            self.state.append(chip("BOOKMARKED", "bookmark"))


class HubView(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window = window
        self.backend = window.backend
        self.loader = window.app.textures
        self.resources: list[dict] = []
        self.installed: dict[str, dict] = {}
        self.page = 0
        self.total_found = 0
        self.total_pages = 0
        self.filters: dict | None = None
        self._query_seq = 0
        self._loading = False

        side = vbox(8)
        side.add_css_class("vb-filter-panel")
        side.set_margin_start(10)
        side.set_margin_end(10)
        side.set_margin_top(8)
        self.search = Gtk.SearchEntry(placeholder_text="Search the Hub…")
        self.search.connect("activate", lambda *_: self.reload())
        self.search.connect("search-changed", self._on_search_changed)
        side.append(self.search)
        side.append(
            view_blurb(
                "Everything on the Virt-a-Mate Hub, installed or not. Installing queues the download "
                "together with the dependencies you do not have."
            )
        )
        side.append(label("TYPE", "vb-section-label"))
        self.type_dd = Gtk.DropDown.new_from_strings(["All"])
        self.type_dd.connect("notify::selected", lambda *_: self.reload())
        side.append(self.type_dd)
        side.append(label("PRICING", "vb-section-label"))
        self.category_dd = Gtk.DropDown.new_from_strings(["All", "Free", "Paid", "Paid Early-Access"])
        self.category_dd.connect("notify::selected", lambda *_: self.reload())
        side.append(self.category_dd)
        side.append(label("LICENSE", "vb-section-label"))
        self.license_dd = Gtk.DropDown.new_from_strings(list(LICENSE_FILTER_OPTIONS))
        self.license_dd.connect("notify::selected", lambda *_: self.reload())
        side.append(self.license_dd)
        side.append(label("SORT", "vb-section-label"))
        self.sort_dd = Gtk.DropDown.new_from_strings(["Latest Update"])
        self.sort_dd.connect("notify::selected", lambda *_: self.reload())
        side.append(self.sort_dd)
        side.append(label("AUTHOR", "vb-section-label"))
        self.author_entry = Gtk.Entry(placeholder_text="Hub username")
        self.author_entry.connect("activate", lambda *_: self.reload())
        side.append(self.author_entry)
        side.append(label("TAGS", "vb-section-label"))
        self.tags_entry = Gtk.Entry(placeholder_text="tag")
        self.tags_entry.connect("activate", lambda *_: self.reload())
        side.append(self.tags_entry)
        go = Gtk.Button(label="Search")
        go.add_css_class("suggested-action")
        go.connect("clicked", lambda *_: self.reload())
        go.set_margin_top(8)
        side.append(go)
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
        self.spinner = Gtk.Spinner()
        bar.append(self.spinner)
        bar.append(Gtk.Box(hexpand=True))
        self.more_btn = Gtk.Button(label="Load more")
        self.more_btn.add_css_class("flat")
        self.more_btn.set_visible(False)
        self.more_btn.connect("clicked", lambda *_: self.load_page(self.page + 1))
        bar.append(self.more_btn)
        center.append(bar)
        self.store = Gio.ListStore.new(DictItem)
        self.selection = Gtk.SingleSelection.new(self.store)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self._cards: dict[str, HubCard] = {}
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda f, it: it.set_child(HubCard(self.loader)))
        factory.connect("bind", self._bind_card)
        factory.connect("unbind", self._unbind_card)
        self.grid = Gtk.GridView.new(self.selection, factory)
        self.grid.set_max_columns(12)
        self.grid.set_margin_start(8)
        self.grid.set_margin_end(8)
        self.selection.connect("notify::selected-item", self._on_selected)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid, vexpand=True)
        self.scroll.get_vadjustment().connect("value-changed", self._on_scrolled)
        self.status_page = Adw.StatusPage(
            title="Hub",
            description="Search the VaM Hub to browse and install packages.",
            icon_name="network-workgroup-symbolic",
        )
        self.status_page.set_vexpand(True)
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("pill")
        retry.set_halign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_: self.reload())
        self.status_page.set_child(retry)
        self.center_stack = Gtk.Stack()
        self.center_stack.add_named(self.scroll, "grid")
        self.center_stack.add_named(self.status_page, "status")
        center.append(self.center_stack)
        self.append(center)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.detail = HubDetail(self)
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

        self._started = False
        self._search_timer = None
        self._installed_debounce = Debouncer(150, self._refresh_installed)
        window.bridge.connect("packages:updated", lambda _p: self._installed_debounce.trigger())
        self.queued_rids: set[str] = set()
        self._queued_debounce = Debouncer(150, self._refresh_queued)
        window.bridge.connect("downloads:updated", lambda _p: self._queued_debounce.trigger())
        self.bookmarked: set[str] = set()
        self._bookmarked_debounce = Debouncer(150, self._refresh_bookmarked)
        window.bridge.connect("bookmarks:updated", lambda _p: self._bookmarked_debounce.trigger())

    def _bind_card(self, _factory, item) -> None:
        card, rid = item.get_child(), item.get_item().key
        card.bind(item.get_item().data, self.installed.get(rid), rid in self.bookmarked)
        card.rid = rid
        self._cards[rid] = card

    def _unbind_card(self, _factory, item) -> None:
        card = item.get_child()
        if card.rid is not None and self._cards.get(card.rid) is card:
            del self._cards[card.rid]

    def on_shown(self):
        if not self._started:
            self._started = True
            self._refresh_queued()
            self._refresh_bookmarked()
            self._load_filters()

    def _refresh_bookmarked(self):
        def done(rids):
            self.bookmarked = rids
            for rid, card in self._cards.items():
                card.set_bookmarked(rid in rids)
            self.detail.refresh_state()

        run_async(self.backend.lib.bookmarked_resource_ids, on_done=done)

    def _refresh_queued(self):

        def work():
            return {
                str(d["hub_resource_id"])
                for d in self.backend.downloads.list()
                if d.get("hub_resource_id") and d["status"] in ("queued", "active")
            }

        def done(rids):
            self.queued_rids = rids
            self.detail.refresh_state()

        run_async(work, on_done=done)

    def _load_filters(self):
        def done(f):
            self.filters = f
            self.type_dd.set_model(Gtk.StringList.new(["All"] + list(f.get("type") or [])))
            self.sort_dd.set_model(Gtk.StringList.new(list(f.get("sort") or ["Latest Update"])))
            self.reload()

        def failed(err):
            self._show_status("Hub unavailable", f"Could not load Hub filters: {err}")

        run_async(self.backend.hub.get_filters, on_done=done, on_error=failed)

    def _show_status(self, title: str, description: str):
        self.status_page.set_title(title)
        self.status_page.set_description(GLib.markup_escape_text(description))
        self.center_stack.set_visible_child_name("status")

    def _on_search_changed(self, entry):
        if self._search_timer:
            GLib.source_remove(self._search_timer)
        self._search_timer = GLib.timeout_add(450, self._search_fire)

    def _search_fire(self):
        self._search_timer = None
        self.reload()
        return False

    def _params(self, page: int) -> dict:
        p = {"page": page, "perpage": HUB_PER_PAGE}
        q = self.search.get_text().strip()
        if q:
            p["search"] = q
        t = self.type_dd.get_selected_item()
        if t and t.get_string() != "All":
            p["type"] = t.get_string()
        c = self.category_dd.get_selected_item()
        if c and c.get_string() != "All":
            p["category"] = c.get_string()
        lic = self.license_dd.get_selected_item()
        if lic and lic.get_string() != "Any":
            p["license"] = lic.get_string()
        s = self.sort_dd.get_selected_item()
        if s:
            p["sort"] = s.get_string()
        if self.author_entry.get_text().strip():
            p["username"] = self.author_entry.get_text().strip()
        if self.tags_entry.get_text().strip():
            p["tags"] = self.tags_entry.get_text().strip()
        return p

    def search_for(self, text: str) -> None:
        if self.search.get_text() == text:
            self.reload()
        else:
            self.search.set_text(text)
        self.search.grab_focus()

    def _on_scrolled(self, adj):
        if self._loading or not self.resources or self.page >= self.total_pages:
            return
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 600:
            self.load_page(self.page + 1)

    def reload(self):
        self.resources = []
        self.store.remove_all()
        self.page = 0
        self.load_page(1)

    def load_page(self, page: int):
        if self._loading and page > 1:
            return
        self._loading = True
        self._query_seq += 1
        seq = self._query_seq
        self.spinner.start()
        params = self._params(page)

        def work():
            res = self.backend.hub.search_resources(params)
            rids = [str(r.get("resource_id")) for r in res["resources"] if r.get("resource_id")]
            with self.backend.lib.lock:
                installed = self.backend.lib.store.hub_install_snapshot(rids)
            return res, installed

        def done(result):
            if seq != self._query_seq:
                return
            self._loading = False
            self.spinner.stop()
            res, installed = result
            self.installed.update(installed)
            self.page = page
            self.total_found = res["total_found"]
            self.total_pages = res["total_pages"]
            self.resources.extend(res["resources"])
            self.store.splice(
                self.store.get_n_items(), 0, [DictItem(r, str(r.get("resource_id"))) for r in res["resources"]]
            )
            self.count_label.set_text(f"{len(self.resources)} of about {self.total_found} results")
            self.more_btn.set_visible(page < self.total_pages)
            self.center_stack.set_visible_child_name("grid")
            if not self.resources:
                self._show_status("No results", "Nothing on the Hub matches this search.")

        def failed(err):
            if seq != self._query_seq:
                return
            self._loading = False
            self.spinner.stop()
            self._show_status("Hub unavailable", str(err))

        run_async(work, on_done=done, on_error=failed)

    def _refresh_installed(self):
        if not self.resources:
            return
        rids = [str(r.get("resource_id")) for r in self.resources if r.get("resource_id")]

        def work():
            with self.backend.lib.lock:
                return self.backend.lib.store.hub_install_snapshot(rids)

        def done(installed):
            self.installed = installed
            for rid, card in self._cards.items():
                card.set_installed(installed.get(rid))
            self.detail.refresh_library_state()

        run_async(work, on_done=done)

    def clear_selection(self) -> None:
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def _on_selected(self, *_):
        item = self.selection.get_selected_item()
        self.detail_scroll.set_visible(item is not None)
        if item:
            self.detail.show(item.data)
            scroll_to_top(self.detail_scroll)

    def open_resource(self, rid) -> None:
        rid = str(rid or "")
        if not rid or rid == "null":
            return
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).key == rid:
                self.selection.set_selected(i)
                self.grid.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                return

        def done(detail):
            if not detail:
                self.window.toast(f"Resource {rid} was not found on the Hub")
                return
            self.detail_scroll.set_visible(True)
            self.detail.show(detail)
            scroll_to_top(self.detail_scroll)

        run_async(
            self.backend.hub.get_resource_detail,
            rid,
            on_done=done,
            on_error=lambda e: self.window.toast(f"Could not load the resource: {e}", 8),
        )


class HubDetail(Gtk.Box):
    def __init__(self, view: HubView):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.view = view
        self.backend = view.backend
        self.resource: dict | None = None
        self.detail: dict | None = None
        for m in ("start", "end"):
            getattr(self, f"set_margin_{m}")(14)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.thumb = Thumb(330, 220, follow_image=True)
        self.append(self.thumb)
        self.title = label("", "vb-detail-title", wrap=True)
        self.title.set_selectable(True)
        self.append(self.title)
        self.sub = label("", "vb-detail-sub", wrap=True)
        self.append(self.sub)
        self.chips = hbox(4)
        self.append(self.chips)
        actions = hbox(6)
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.connect("clicked", lambda *_: self.install())
        actions.append(self.install_btn)
        self.open_btn = Gtk.Button(label="Open on Hub")
        self.open_btn.connect(
            "clicked",
            lambda *_: open_url(hub_resource_page_url(self.resource["resource_id"])) if self.resource else None,
        )
        actions.append(self.open_btn)
        self.bookmark_btn = Gtk.Button(label="Bookmark")
        self.bookmark_btn.connect("clicked", lambda *_: self.toggle_bookmark())
        actions.append(self.bookmark_btn)
        self.append(actions)
        self.stats = label("", "vb-dim vb-small", wrap=True)
        self.append(self.stats)
        self.tagline = label("", "vb-dim", wrap=True)
        self.tagline.set_selectable(True)
        self.append(self.tagline)
        self.files_box = vbox(2)
        self.append(self.files_box)
        self.deps_box = vbox(2)
        self.append(self.deps_box)
        self._seq = 0

    def show(self, r: dict):
        self.resource = r
        self.detail = None
        rid = r.get("resource_id")
        self.thumb.load(self.view.loader, f"hub-icon:{rid}" if rid else None)
        self.title.set_text(r.get("title") or "")
        self.sub.set_text(f"by {r.get('username') or '?'} · version {r.get('version_string') or '?'}")
        clear_box(self.chips)
        t = r.get("type") or "Other"
        self.chips.append(chip(t.upper(), HUB_TYPE_CLASS.get(t, "other")))
        if r.get("category"):
            self.chips.append(chip(r["category"].upper(), "warn" if r["category"] != "Free" else "muted"))
        lic = hub_resource_license(r)
        if lic:
            self.chips.append(chip(lic, "muted"))
        self.stats.set_text(
            f"{_i(r.get('download_count'))} downloads · {_i(r.get('view_count'))} views · rating {r.get('rating_avg') or '0'} ({_i(r.get('rating_count'))})"
            + (
                f" · updated {time.strftime('%Y-%m-%d', time.localtime(_i(r.get('last_update'))))}"
                if _i(r.get("last_update"))
                else ""
            )
        )
        self.tagline.set_text(r.get("tag_line") or "")
        clear_box(self.files_box)
        clear_box(self.deps_box)
        self.deps_box.append(label("Loading dependencies…", "vb-dim vb-small"))
        self.refresh_state()
        self._seq += 1
        seq = self._seq
        if not rid:
            return

        def done(detail):
            if seq != self._seq:
                return
            self.detail = detail
            self._render_detail(detail)

        def failed(err):
            if seq != self._seq:
                return
            clear_box(self.deps_box)
            self.deps_box.append(label(f"Could not load details: {err}", "vb-dim vb-small", wrap=True))

        run_async(self.backend.hub.get_resource_detail, rid, on_done=done, on_error=failed)

    def _render_detail(self, d: dict):
        clear_box(self.files_box)
        files = d.get("hubFiles") or []
        if files:
            self.files_box.append(label("FILES", "vb-section-label"))
            for f in files:
                row = hbox(6)
                name = label(f.get("filename") or "?", "vb-mono vb-small")
                name.set_hexpand(True)
                row.append(name)
                row.append(label(human_size(_i(f.get("file_size"))), "vb-dim vb-small"))
                self.files_box.append(row)
        else:
            self.files_box.append(
                label(
                    "No downloadable files are listed for this resource (paid or external).",
                    "vb-dim vb-small",
                    wrap=True,
                )
            )
        clear_box(self.deps_box)
        deps = d.get("dependencies") or {}
        entries = [e for arr in deps.values() for e in (arr or []) if isinstance(e, dict)]
        if entries:
            self.deps_box.append(label(f"DEPENDENCIES ({len(entries)})", "vb-section-label"))
            with self.backend.lib.lock:
                idx = self.backend.lib.store.package_index
                groups = self.backend.lib.store.group_index
            from ..core.graph import resolve_ref

            for e in entries:
                ref = e.get("filename") or e.get("packageName") or "?"
                res = resolve_ref(ref, idx, groups) if ref else None
                resolution = res.resolution if res else "missing"
                if resolution == "missing" and e.get("downloadUrl") in (None, "null", ""):
                    resolution = "missing"
                dep_rid = e.get("resource_id")
                node = {
                    "ref": ref,
                    "resolved": res.resolved if res else None,
                    "resolution": resolution,
                    "storage_state": (
                        (idx.get(res.resolved) or {}).get("storage_state") if res and res.resolved else None
                    ),
                    "hub_resource_id": dep_rid if dep_rid not in (None, "", "null") else None,
                }
                if not node["resolved"] and e.get("downloadUrl") not in (None, "null", ""):
                    node["resolution"] = "on-hub"
                self.deps_box.append(
                    DepRow(
                        node,
                        0,
                        self.view.window.select_package,
                        on_hub=self.view.open_resource if node["hub_resource_id"] else None,
                    )
                )
        self.refresh_state()

    def refresh_library_state(self):
        if self.detail is not None:
            self._render_detail(self.detail)
        else:
            self.refresh_state()

    def refresh_state(self):
        r = self.resource
        if not r:
            return
        rid = str(r.get("resource_id"))
        installed = self.view.installed.get(rid)
        queued = rid in self.view.queued_rids
        if queued:
            self.install_btn.set_label("Downloading…")
            self.install_btn.set_sensitive(False)
        elif installed and installed.get("is_placeholder"):
            self.install_btn.set_label("Install (replaces the stand-in)")
            self.install_btn.set_tooltip_text("A dummy or alias stands in for this file; the real one takes its place.")
            self.install_btn.set_sensitive(True)
        elif installed and installed.get("is_direct"):
            self.install_btn.set_label("Installed")
            self.install_btn.set_sensitive(False)
        elif installed:
            self.install_btn.set_label("Mark as installed directly")
            self.install_btn.set_tooltip_text("Installed as a dependency; treat it as a package you chose.")
            self.install_btn.set_sensitive(True)
        else:
            self.install_btn.set_label("Install")
            self.install_btn.set_sensitive(True)
        if rid in self.view.bookmarked:
            self.bookmark_btn.set_label("Remove bookmark")
            self.bookmark_btn.set_tooltip_text("Listed in your library under Bookmarked; forget it.")
        else:
            self.bookmark_btn.set_label("Bookmark")
            self.bookmark_btn.set_tooltip_text(
                "Note it for later: it is listed in your library under Bookmarked, with the creator's link and a "
                "note of your own, until a package of it arrives. Handy for paid or externally hosted resources."
            )

    def toggle_bookmark(self):
        r = self.resource
        if not r or not r.get("resource_id"):
            return
        rid = str(r["resource_id"])
        if rid in self.view.bookmarked:
            self.view.window.run_op(self.backend.lib.remove_bookmark, rid, success="Bookmark removed")
            return
        self.bookmark_btn.set_sensitive(False)

        def done(res):
            self.bookmark_btn.set_sensitive(True)
            self.view.window.toast(f"Bookmarked {res['title']}: listed in the library under Bookmarked")

        def failed(err):
            self.bookmark_btn.set_sensitive(True)
            self.view.window.toast(f"Could not bookmark it: {err}", 8)

        run_async(self.backend.bookmark_resource, rid, self.detail or r, on_done=done, on_error=failed)

    def install(self):
        r = self.resource
        if not r:
            return
        rid = str(r.get("resource_id"))
        installed = self.view.installed.get(rid)
        if installed and not installed.get("is_direct") and not installed.get("is_placeholder"):
            self.view.window.run_op(self.backend.lib.promote, installed["filename"], success="Added to your library")
            return
        self.install_btn.set_sensitive(False)

        def done(res):
            n = res["inserted"]
            msg = f"Queued {n} download(s)" if n else "Nothing to download"
            if res.get("unresolved_deps"):
                msg += f"; {len(res['unresolved_deps'])} dependenc{'y' if len(res['unresolved_deps']) == 1 else 'ies'} not available on the Hub"
            self.view.window.toast(msg)
            self.refresh_state()

        def failed(err):
            self.view.window.toast(f"Install failed: {err}", 8)
            self.refresh_state()

        run_async(self.backend.downloads.enqueue_install, rid, self.detail, on_done=done, on_error=failed)
