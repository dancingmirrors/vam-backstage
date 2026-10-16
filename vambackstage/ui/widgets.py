from __future__ import annotations

import re
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from .models import human_size

CATEGORY_CLASS = {
    "Scenes": "scenes",
    "Looks": "looks",
    "Clothing": "clothing",
    "Hairstyles": "hairstyles",
    "Poses": "poses",
    "SubScenes": "subscenes",
}

STATE_LABEL = {"disabled": "DISABLED", "offloaded": "OFFLOADED", "archived": "ARCHIVED"}


def chip(text: str, style: str = "muted") -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("vb-chip")
    lbl.add_css_class(style)
    return lbl


def category_chip(category: str | None) -> Gtk.Label:
    if not category:
        return chip("OTHER", "other")
    return chip(category.upper(), CATEGORY_CLASS.get(category, "other"))


def label(
    text: str = "",
    css: str | None = None,
    xalign: float = 0.0,
    ellipsize: bool = True,
    lines: int = 1,
    selectable: bool = False,
    wrap: bool = False,
) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign)
    if ellipsize and not wrap:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if lines > 1:
        lbl.set_lines(lines)
    if css:
        for c in css.split():
            lbl.add_css_class(c)
    lbl.set_selectable(selectable)
    return lbl


def icon_button(icon: str, tooltip: str, on_click: Callable | None = None, flat: bool = True) -> Gtk.Button:
    btn = Gtk.Button.new_from_icon_name(icon)
    btn.set_tooltip_text(tooltip)
    if flat:
        btn.add_css_class("flat")
    if on_click:
        btn.connect("clicked", lambda *_: on_click())
    return btn


def strip_window_drag(root: Gtk.Widget) -> int:
    removed = 0
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.WindowHandle):
            controllers = w.observe_controllers()
            for c in [controllers.get_item(i) for i in range(controllers.get_n_items())]:
                if isinstance(c, (Gtk.GestureDrag, Gtk.GestureClick)):
                    w.remove_controller(c)
                    removed += 1
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return removed


class BoundedWidthLayout(Gtk.LayoutManager):

    def __init__(self, max_width: int):
        super().__init__()
        self.max_width = max_width

    def do_get_request_mode(self, widget):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, widget, orientation, for_size):
        minimum = natural = 0
        if orientation == Gtk.Orientation.HORIZONTAL:
            for child in _layout_children(widget):
                mn, nat, _, _ = child.measure(orientation, -1)
                minimum = max(minimum, min(mn, self.max_width))
                natural = max(natural, min(nat, self.max_width))
            return minimum, max(natural, minimum), -1, -1
        width = min(for_size, self.max_width) if for_size > 0 else -1
        for child in _layout_children(widget):
            mn, nat, _, _ = child.measure(orientation, width)
            minimum = max(minimum, mn)
            natural = max(natural, nat)
        return minimum, max(natural, minimum), -1, -1

    def do_allocate(self, widget, width, height, baseline):
        for child in _layout_children(widget):
            child.allocate(width, height, baseline, None)


class AspectLayout(Gtk.LayoutManager):

    def __init__(self, ratio: float, min_width: int):
        super().__init__()
        self.ratio = ratio
        self.min_width = min_width

    def do_get_request_mode(self, widget):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, widget, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return self.min_width, self.min_width, -1, -1
        width = for_size if for_size > 0 else self.min_width
        h = max(1, int(round(width * self.ratio)))
        return h, h, -1, -1

    def do_allocate(self, widget, width, height, baseline):
        for child in _layout_children(widget):
            child.allocate(width, height, baseline, None)


def _layout_children(widget):
    child = widget.get_first_child()
    while child is not None:
        if child.should_layout():
            yield child
        child = child.get_next_sibling()


PANEL_MAX_WIDTH = 440


def detail_panel(child: Gtk.Widget, on_close: Callable[[], None]) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    bar.set_margin_top(4)
    bar.set_margin_end(4)
    bar.append(Gtk.Box(hexpand=True))
    close = icon_button("window-close-symbolic", "Close panel (Escape)", on_close)
    close.add_css_class("circular")
    bar.append(close)
    box.append(bar)
    box.append(child)
    bin_ = Adw.Bin(child=box)
    bin_.set_layout_manager(BoundedWidthLayout(PANEL_MAX_WIDTH))
    return bin_


def scroll_to_top(scrolled: Gtk.ScrolledWindow) -> None:
    adj = scrolled.get_vadjustment()
    adj.set_value(adj.get_lower())
    GLib.idle_add(lambda: adj.set_value(adj.get_lower()) or False, priority=GLib.PRIORITY_LOW)


def name_button(text: str, on_click: Callable[[], None] | None, tooltip: str | None = None) -> Gtk.Button:
    btn = Gtk.Button()
    lbl = Gtk.Label(label=text, xalign=0.0)
    lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    btn.set_child(lbl)
    btn.add_css_class("flat")
    btn.add_css_class("vb-small")
    btn.set_halign(Gtk.Align.FILL)
    btn.set_hexpand(True)
    btn.set_tooltip_text(tooltip or text)
    if on_click:
        btn.connect("clicked", lambda *_: on_click())
    return btn


def view_blurb(text: str) -> Gtk.Label:
    lbl = label(text, "vb-dim vb-small", wrap=True)
    lbl.set_margin_top(2)
    lbl.set_margin_bottom(4)
    return lbl


def hbox(spacing: int = 6, **kw) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing, **kw)


def vbox(spacing: int = 6, **kw) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)


def clear_box(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class Thumb(Gtk.Overlay):

    def __init__(
        self,
        width: int = 180,
        height: int = 135,
        radius_class: str = "vb-thumb",
        follow_image: bool = False,
        max_ratio: float = 1.3,
    ):
        super().__init__()
        self._generation = 0
        self.follow_image = follow_image
        self.max_ratio = max_ratio
        self._default_ratio = height / float(width)
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN if follow_image else Gtk.ContentFit.COVER)
        self.picture.set_can_shrink(True)
        self.picture.add_css_class(radius_class)
        self._layout = AspectLayout(self._default_ratio, width)
        self._frame = Adw.Bin(child=self.picture)
        self._frame.set_layout_manager(self._layout)
        self.set_child(self._frame)
        self.placeholder = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        self.placeholder.set_pixel_size(28)
        self.placeholder.add_css_class("vb-thumb-placeholder")
        self.placeholder.set_halign(Gtk.Align.CENTER)
        self.placeholder.set_valign(Gtk.Align.CENTER)
        self.add_overlay(self.placeholder)

    def _fit_to(self, texture) -> None:
        if not self.follow_image:
            return
        ratio = self._default_ratio
        if texture is not None and texture.get_intrinsic_width() > 0:
            ratio = min(
                self.max_ratio, max(0.35, texture.get_intrinsic_height() / float(texture.get_intrinsic_width()))
            )
        if abs(ratio - self._layout.ratio) > 1e-3:
            self._layout.ratio = ratio
            self._frame.queue_resize()

    def load(self, loader, key: str | None) -> None:
        self._generation += 1
        gen = self._generation
        self.picture.set_paintable(None)
        self.placeholder.set_visible(True)
        if not key:
            return

        def deliver(texture):
            if gen != self._generation:
                return
            if texture is not None:
                self._fit_to(texture)
                self.picture.set_paintable(texture)
                self.placeholder.set_visible(False)

        loader.get(key, deliver)

    def set_texture(self, texture) -> None:
        self._generation += 1
        self._fit_to(texture)
        self.picture.set_paintable(texture)
        self.placeholder.set_visible(texture is None)


class PackageCard(Gtk.Box):

    def __init__(self, loader, width: int = 190):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.loader = loader
        self.add_css_class("vb-card")
        self.set_size_request(width, -1)
        self.thumb = Thumb(width, int(width * 0.72))
        self.badges = hbox(4)
        self.badges.set_halign(Gtk.Align.START)
        self.badges.set_valign(Gtk.Align.START)
        self.badges.set_margin_start(6)
        self.badges.set_margin_top(6)
        self.thumb.add_overlay(self.badges)
        self.state_badges = hbox(4)
        self.state_badges.set_halign(Gtk.Align.END)
        self.state_badges.set_valign(Gtk.Align.END)
        self.state_badges.set_margin_end(6)
        self.state_badges.set_margin_bottom(6)
        self.thumb.add_overlay(self.state_badges)
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

    def bind(self, pkg: dict) -> None:
        if pkg.get("is_local"):
            self._bind_local(pkg)
            return
        name = pkg.get("hub_display_name") or pkg.get("title") or pkg["package_name"].split(".")[-1]
        self.title.set_text(name)
        self.title.set_tooltip_text(pkg["filename"])
        self.sub.set_text(f"{pkg['creator']} · v{pkg['version']}")
        bits = []
        if pkg.get("dep_count"):
            bits.append(f"{pkg['dep_count']} deps")
        if pkg.get("missing_deps"):
            bits.append(f"{pkg['missing_deps']} missing")
        if pkg.get("content_count"):
            bits.append(f"{pkg['content_count']} items")
        bits.append(human_size(pkg.get("size_bytes")))
        self.meta.set_text(" · ".join(bits))
        clear_box(self.badges)
        self.badges.append(category_chip(pkg.get("type")))
        if not pkg.get("is_direct"):
            self.badges.append(chip("DEP", "dep"))
        if pkg.get("is_dummy"):
            self.badges.append(chip("DUMMY", "muted"))
        if pkg.get("is_alias"):
            self.badges.append(chip("ALIAS", "muted"))
        if pkg.get("is_unpacked"):
            self.badges.append(chip("UNPACKED", "muted"))
        clear_box(self.state_badges)
        state = pkg.get("storage_state")
        if state in STATE_LABEL:
            self.state_badges.append(chip(STATE_LABEL[state], "muted"))
        if pkg.get("is_corrupted"):
            self.state_badges.append(chip("CORRUPT", "error"))
        elif pkg.get("missing_deps"):
            self.state_badges.append(chip("MISSING DEPS", "warn"))
        elif pkg.get("is_orphan"):
            self.state_badges.append(chip("ORPHAN", "warn"))
        elif pkg.get("is_superseded"):
            self.state_badges.append(chip("OLD VERSION", "muted"))
        if state == "enabled":
            self.remove_css_class("disabled")
        else:
            self.add_css_class("disabled")
        self.thumb.load(self.loader, "pkg:" + pkg["filename"])

    def _bind_local(self, pkg: dict) -> None:
        self.title.set_text("Loose scenes and presets")
        self.title.set_tooltip_text("Your own files under Saves and Custom, outside any package")
        self.sub.set_text("Saves, Custom")
        bits = [f"{pkg['missing_deps']} missing"] if pkg.get("missing_deps") else []
        if pkg.get("content_count"):
            bits.append(f"{pkg['content_count']} items")
        self.meta.set_text(" · ".join(bits))
        clear_box(self.badges)
        self.badges.append(chip("LOCAL", "muted"))
        clear_box(self.state_badges)
        if pkg.get("missing_deps"):
            self.state_badges.append(chip("MISSING DEPS", "warn"))
        self.remove_css_class("disabled")
        self.thumb.load(self.loader, None)


class ContentCard(Gtk.Box):
    def __init__(self, loader, size: int = 150):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.loader = loader
        self.add_css_class("vb-card")
        self.set_size_request(size, -1)
        self.thumb = Thumb(size, size)
        self.badges = hbox(4)
        self.badges.set_halign(Gtk.Align.START)
        self.badges.set_valign(Gtk.Align.START)
        self.badges.set_margin_start(6)
        self.badges.set_margin_top(6)
        self.thumb.add_overlay(self.badges)
        self.flags = hbox(2)
        self.flags.set_halign(Gtk.Align.END)
        self.flags.set_valign(Gtk.Align.START)
        self.flags.set_margin_end(4)
        self.flags.set_margin_top(4)
        self.thumb.add_overlay(self.flags)
        self.append(self.thumb)
        body = vbox(2)
        body.add_css_class("vb-card-body")
        self.title = label("", "vb-card-title")
        self.sub = label("", "vb-card-sub")
        body.append(self.title)
        body.append(self.sub)
        self.append(body)

    def bind(self, item: dict) -> None:
        self.title.set_text(item["display_name"])
        self.title.set_tooltip_text(item["internal_path"])
        owner = item.get("package_title") or item.get("package_name") or ""
        self.sub.set_text(
            f"{item.get('creator') or 'Local'} · {owner}" if owner else (item.get("creator") or "Local content")
        )
        clear_box(self.badges)
        self.badges.append(category_chip(item.get("category")))
        tag = item.get("tag")
        if tag:
            self.badges.append(chip(tag["label"].upper(), "muted"))
        clear_box(self.flags)
        if item.get("missing_refs"):
            n = len(item["missing_refs"])
            c = chip("MISSING DEPS", "warn")
            c.set_tooltip_text(f"{n} referenced package(s) not installed:\n" + "\n".join(item["missing_refs"][:12]))
            self.flags.append(c)
        if item.get("hidden"):
            img = Gtk.Image.new_from_icon_name("view-conceal-symbolic")
            img.set_tooltip_text("Hidden in VaM")
            self.flags.append(img)
        if item.get("favorite"):
            img = Gtk.Image.new_from_icon_name("starred-symbolic")
            img.set_tooltip_text("Favorite")
            self.flags.append(img)
        if item.get("hidden") or item.get("storage_state") != "enabled":
            self.add_css_class("disabled")
        else:
            self.remove_css_class("disabled")
        key = f"ct:{item['package_filename']}\0{item['thumbnail_path']}" if item.get("thumbnail_path") else None
        self.thumb.load(self.loader, key)


class FilterList(Gtk.Box):

    def __init__(self, title: str, on_change: Callable[[str | None], None], show_more_after: int = 8):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.on_change = on_change
        self.show_more_after = show_more_after
        self.append(label(title.upper(), "vb-section-label"))
        self._rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.append(self._rows)
        self._more = Gtk.Button(label="Show more")
        self._more.add_css_class("flat")
        self._more.add_css_class("vb-small")
        self._more.connect("clicked", self._toggle_more)
        self._more.set_visible(False)
        self.append(self._more)
        self._expanded = False
        self._group: Gtk.CheckButton | None = None
        self._buttons: dict[str | None, Gtk.CheckButton] = {}
        self._selected: str | None = None
        self._updating = False

    def set_options(self, options: list[tuple[str | None, str, int | None]], selected: str | None = ...) -> None:
        self._updating = True
        if selected is not ...:
            self._selected = selected
        clear_box(self._rows)
        self._group = Gtk.CheckButton()
        self._buttons = {}
        for i, (value, text, count) in enumerate(options):
            row = Gtk.CheckButton()
            box = hbox(6)
            box.append(label(text))
            if count is not None:
                c = label(str(count), "count")
                c.set_hexpand(True)
                c.set_xalign(1.0)
                box.append(c)
            row.set_child(box)
            row.add_css_class("vb-filter-row")
            row.set_group(self._group)
            row.set_active(value == self._selected)
            row.connect("toggled", self._on_toggled, value)
            row.set_visible(self._expanded or i < self.show_more_after or value == self._selected)
            self._rows.append(row)
            self._buttons[value] = row
        self._more.set_visible(len(options) > self.show_more_after)
        self._more.set_label("Show less" if self._expanded else f"Show {len(options) - self.show_more_after} more")
        self._updating = False

    def _toggle_more(self, *_):
        self._expanded = not self._expanded
        for i, (value, btn) in enumerate(self._buttons.items()):
            btn.set_visible(self._expanded or i < self.show_more_after or value == self._selected)
        self._more.set_label(
            "Show less" if self._expanded else f"Show {len(self._buttons) - self.show_more_after} more"
        )

    def _on_toggled(self, btn: Gtk.CheckButton, value) -> None:
        if self._updating or not btn.get_active():
            return
        self._selected = value
        self.on_change(value)

    @property
    def selected(self):
        return self._selected

    def select(self, value) -> None:
        self._selected = value
        btn = self._buttons.get(value)
        if btn:
            self._updating = True
            btn.set_active(True)
            self._updating = False


class DepRow(Gtk.Box):

    STATUS = {
        "exact": ("Installed", "success"),
        "latest": ("Installed", "success"),
        "fallback": ("Fallback", "warn"),
        "missing": ("Missing", "error"),
        "invalid": ("Invalid", "error"),
        "on-hub": ("On Hub", "muted"),
    }

    def __init__(
        self,
        node: dict,
        depth: int,
        on_select: Callable[[str], None] | None,
        strict: bool = True,
        on_dummy: Callable[[str], None] | None = None,
        on_hub: Callable[[str], None] | None = None,
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.add_css_class("vb-dep-row")
        self.set_margin_start(depth * 14)
        name = node.get("display") or node.get("ref") or node.get("filename") or "?"
        resolution = node.get("resolution")
        if strict and resolution == "fallback":
            m = re.search(r"\.(\d+)\.var$", node.get("resolved") or "", flags=re.IGNORECASE)
            name = f"{name}  (v{m.group(1)} installed instead)" if m else name
        if node.get("resolved") and on_select:
            self.append(
                name_button(name, lambda: on_select(node["resolved"]), f"{name}\nShow this package in the library")
            )
        elif resolution == "on-hub" and on_hub:
            self.append(
                name_button(
                    name, lambda: on_hub(node.get("hub_resource_id")), f"{name}\nShow this resource on the Hub page"
                )
            )
        else:
            lbl = label(name, "vb-small")
            lbl.set_hexpand(True)
            lbl.set_tooltip_text(name)
            lbl.set_selectable(True)
            self.append(lbl)
        text, style = self.STATUS.get(resolution, ("?", "muted"))
        if strict and resolution == "fallback":
            text, style = "Missing", "error"
        state = node.get("storage_state")
        if node.get("resolved") and state and state != "enabled" and not (strict and resolution == "fallback"):
            text, style = STATE_LABEL.get(state, state).title(), "muted"
            self.add_css_class("inactive")
        if resolution == "missing" or (strict and resolution == "fallback"):
            self.add_css_class("missing")
        elif resolution == "fallback":
            self.add_css_class("fallback")
        c = chip(text, style)
        c.set_halign(Gtk.Align.END)
        self.append(c)
        if on_dummy and (resolution == "missing" or (strict and resolution == "fallback")):
            b = Gtk.Button(label="Make dummy")
            b.add_css_class("flat")
            b.add_css_class("vb-small")
            b.set_tooltip_text("Create an empty package with this name so VaM stops reporting it as missing")
            b.connect("clicked", lambda *_: on_dummy(node.get("ref") or ""))
            self.append(b)
        ref_text = node.get("ref") or node.get("resolved") or ""
        if ref_text:
            self.append(copy_button(ref_text))


def copy_button(text: str) -> Gtk.Button:
    from .launch import copy_to_clipboard

    btn = icon_button("edit-copy-symbolic", f"Copy “{text}”", lambda: copy_to_clipboard(text))
    btn.add_css_class("vb-small")
    btn.set_valign(Gtk.Align.CENTER)
    return btn
