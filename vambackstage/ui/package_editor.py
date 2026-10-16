from __future__ import annotations

import json
import logging
import math
import os
import re

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ..core.jsonedit import (
    KIND_JSON,
    KIND_TEXT,
    JsonDocument,
    delete_at,
    find_matches,
    format_path,
    insert_at,
    list_package_files,
    load_json_document,
    rename_key,
    replace_in_package,
    replace_text,
    save_json_document,
    set_at,
)
from .dispatch import Debouncer, run_async
from .launch import open_in_file_manager
from .models import human_size
from .widgets import hbox, icon_button, label, vbox

log = logging.getLogger(__name__)

MAX_TEXT_PREVIEW = 2 * 1024 * 1024
PREVIEW_CHARS = 100
TYPE_NAMES = ("String", "Number", "Boolean", "Null", "Object", "Array")
_INT_RE = re.compile(r"^[+-]?\d+$")


def type_name(v) -> str:
    if isinstance(v, dict):
        return "Object"
    if isinstance(v, list):
        return "Array"
    if isinstance(v, bool):
        return "Boolean"
    if v is None:
        return "Null"
    if isinstance(v, (int, float)):
        return "Number"
    return "String"


def preview(v) -> str:
    if isinstance(v, dict):
        return f"{len(v)} {'key' if len(v) == 1 else 'keys'}"
    if isinstance(v, list):
        return f"{len(v)} {'item' if len(v) == 1 else 'items'}"
    if isinstance(v, str):
        s = v.replace("\n", "⏎")
        return s[:PREVIEW_CHARS] + ("…" if len(s) > PREVIEW_CHARS else "")
    return json.dumps(v)


def parse_number(text: str):
    t = text.strip()
    if _INT_RE.match(t):
        return int(t)
    try:
        f = float(t)
    except ValueError:
        raise ValueError(f"“{text}” is not a number") from None
    if not math.isfinite(f):
        raise ValueError("JSON has no infinity or NaN")
    return f


class FileNode(GObject.Object):

    __gtype_name__ = "VbPackageFileNode"

    def __init__(self, name: str, rel: str, is_dir: bool, size: int = 0, kind: str | None = None):
        super().__init__()
        self.name = name
        self.rel = rel
        self.is_dir = is_dir
        self.size = size
        self.kind = kind
        self.children: list[FileNode] = []


def build_file_tree(files) -> list[FileNode]:
    root = FileNode("", "", True)
    folders: dict[str, FileNode] = {"": root}
    for f in files:
        parts = f.path.split("/")
        parent = root
        for i, part in enumerate(parts[:-1]):
            rel = "/".join(parts[: i + 1])
            node = folders.get(rel)
            if node is None:
                node = folders[rel] = FileNode(part, rel, True)
                parent.children.append(node)
            parent = node
        parent.children.append(FileNode(parts[-1], f.path, False, f.size, f.kind))

    def sort(node: FileNode) -> None:
        node.children.sort(key=lambda c: (c.rel != "meta.json", not c.is_dir, c.name.lower()))
        for c in node.children:
            if c.is_dir:
                c.size = sum(x.size for x in c.children) if c.children else 0
                sort(c)

    sort(root)
    return root.children


class JsonNode(GObject.Object):

    __gtype_name__ = "VbJsonNode"

    def __init__(self, editor: JsonEditor, parent: JsonNode | None, key):
        super().__init__()
        self.editor = editor
        self.parent = parent
        self.key = key
        self.store: Gio.ListStore | None = None

    @property
    def value(self):
        if self.parent is None:
            return self.editor.doc.data if self.editor.doc else None
        try:
            return self.parent.value[self.key]
        except (KeyError, IndexError, TypeError):
            return None

    @property
    def path(self) -> tuple:
        parts = []
        node = self
        while node.parent is not None:
            parts.append(node.key)
            node = node.parent
        return tuple(reversed(parts))

    @property
    def is_container(self) -> bool:
        return isinstance(self.value, (dict, list))

    def index_in_parent(self) -> int:
        if self.parent is None:
            return 0
        pv = self.parent.value
        return list(pv).index(self.key) if isinstance(pv, dict) else int(self.key)

    def children(self) -> Gio.ListStore | None:
        v = self.value
        if not isinstance(v, (dict, list)):
            return None
        if self.store is None:
            self.store = Gio.ListStore.new(JsonNode)
            keys = list(v.keys()) if isinstance(v, dict) else list(range(len(v)))
            self.store.splice(0, 0, [JsonNode(self.editor, self, k) for k in keys])
        return self.store


class JsonEditor(Gtk.Box):

    def __init__(self, dialog: PackageEditorDialog):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.dialog = dialog
        self.window = dialog.window
        self.doc: JsonDocument | None = None
        self.root_node: JsonNode | None = None
        self._bound: dict[JsonNode, tuple[Gtk.Label, Gtk.Label, Gtk.Label]] = {}
        self._matches: list[tuple] = []
        self._match_index = -1
        self._search_generation = 0
        self.current: JsonNode | None = None

        bar = hbox(6)
        for m in ("start", "end", "top", "bottom"):
            getattr(bar, f"set_margin_{m}")(6)
        self.search = Gtk.SearchEntry(placeholder_text="Find a key or text (a file name VaM complains about, say)…")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self._search_debounce.trigger())
        self.search.connect("activate", lambda *_: self._step_match(1))
        self.search.connect("next-match", lambda *_: self._step_match(1))
        self.search.connect("previous-match", lambda *_: self._step_match(-1))
        bar.append(self.search)
        self.match_label = label("", "vb-dim vb-small")
        bar.append(self.match_label)
        bar.append(icon_button("go-up-symbolic", "Previous match (Shift+Enter)", lambda: self._step_match(-1)))
        bar.append(icon_button("go-down-symbolic", "Next match (Enter)", lambda: self._step_match(1)))
        replace = Gtk.Button(label="Replace…")
        replace.add_css_class("flat")
        replace.set_tooltip_text(
            "Replace text in every value and key of this file, or of every JSON file in the package: "
            "an old package reference, a renamed dependency"
        )
        replace.connect("clicked", lambda *_: self.show_replace())
        bar.append(replace)
        self.append(bar)
        self._search_debounce = Debouncer(250, self._run_search)

        self.root_store = Gio.ListStore.new(JsonNode)
        self.tree = Gtk.TreeListModel.new(self.root_store, False, False, self._children_of)
        self.selection = Gtk.SingleSelection.new(self.tree)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.connect("notify::selected-item", self._on_selected)
        self.view = Gtk.ColumnView.new(self.selection)
        self.view.add_css_class("data-table")
        self.view.set_reorderable(False)
        key_col = Gtk.ColumnViewColumn.new("Key", self._factory("key"))
        key_col.set_resizable(True)
        key_col.set_fixed_width(300)
        value_col = Gtk.ColumnViewColumn.new("Value", self._factory("value"))
        value_col.set_expand(True)
        value_col.set_resizable(True)
        type_col = Gtk.ColumnViewColumn.new("Type", self._factory("type"))
        type_col.set_fixed_width(80)
        for c in (key_col, value_col, type_col):
            self.view.append_column(c)
        scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.append(scroller)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.inspector = vbox(6)
        self.inspector.add_css_class("vb-filter-panel")
        for m in ("start", "end", "top", "bottom"):
            getattr(self.inspector, f"set_margin_{m}")(0)
        inner = vbox(6)
        for m in ("start", "end", "top", "bottom"):
            getattr(inner, f"set_margin_{m}")(10)
        self.inspector.append(inner)
        head = hbox(6)
        self.path_label = label("", "vb-mono vb-dim", selectable=True)
        self.path_label.set_ellipsize(Pango.EllipsizeMode.START)
        self.path_label.set_hexpand(True)
        head.append(self.path_label)
        self.parent_btn = Gtk.Button(label="Parent")
        self.parent_btn.add_css_class("flat")
        self.parent_btn.set_tooltip_text(
            "Select the entry that contains this one: to delete a whole entry (an atom, a storable) "
            "after finding a value inside it"
        )
        self.parent_btn.connect("clicked", lambda *_: self._select_parent())
        head.append(self.parent_btn)
        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.set_tooltip_text("Remove this entry and everything inside it")
        self.delete_btn.connect("clicked", lambda *_: self._delete_current())
        head.append(self.delete_btn)
        inner.append(head)
        self.empty_hint = label("Select an entry to edit it.", "vb-dim")
        inner.append(self.empty_hint)

        self.key_row = hbox(6)
        self.key_row.append(label("Key", "vb-dim"))
        self.key_entry = Gtk.Entry()
        self.key_entry.set_hexpand(True)
        self.key_entry.connect("activate", lambda *_: self._apply())
        self.key_row.append(self.key_entry)
        inner.append(self.key_row)

        self.value_row = hbox(6)
        self.value_row.append(label("Type", "vb-dim"))
        self.type_dropdown = Gtk.DropDown.new_from_strings(list(TYPE_NAMES))
        self.type_dropdown.set_valign(Gtk.Align.START)
        self.type_dropdown.connect("notify::selected", lambda *_: self._show_type(TYPE_NAMES[self._type_index()]))
        self.value_row.append(self.type_dropdown)
        self.value_stack = Gtk.Stack(hexpand=True)
        self.value_stack.set_vhomogeneous(False)
        self.string_view = Gtk.TextView()
        self.string_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.string_view.set_monospace(True)
        self.string_view.set_accepts_tab(False)
        self.string_view.set_tooltip_text("The text, exactly as VaM will read it (Ctrl+Enter applies)")
        self.string_view.add_css_class("vb-json-text")
        string_scroller = Gtk.ScrolledWindow(
            child=self.string_view, propagate_natural_height=True, max_content_height=140, min_content_height=60
        )
        string_scroller.add_css_class("frame")
        self.value_stack.add_named(string_scroller, "String")
        self.number_entry = Gtk.Entry(placeholder_text="A number, like 1 or 0.25")
        self.number_entry.connect("activate", lambda *_: self._apply())
        self.value_stack.add_named(self.number_entry, "Number")
        self.bool_switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self.value_stack.add_named(self.bool_switch, "Boolean")
        self.value_stack.add_named(label("null (no value)", "vb-dim"), "Null")
        self.container_label = label("", "vb-dim")
        self.value_stack.add_named(self.container_label, "Object")
        self.value_row.append(self.value_stack)
        inner.append(self.value_row)
        ctrl_enter = Gtk.ShortcutController()
        ctrl_enter.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>Return"),
                Gtk.CallbackAction.new(lambda *_: self._apply() or True),
            )
        )
        self.string_view.add_controller(ctrl_enter)

        self.add_row = hbox(6)
        self.add_row.append(label("Add entry", "vb-dim"))
        self.add_key = Gtk.Entry(placeholder_text="key")
        self.add_key.connect("activate", lambda *_: self._add_child())
        self.add_row.append(self.add_key)
        self.add_type = Gtk.DropDown.new_from_strings(list(TYPE_NAMES))
        self.add_type.connect("notify::selected", lambda *_: self._sync_add_row())
        self.add_row.append(self.add_type)
        self.add_value = Gtk.Entry(placeholder_text="value")
        self.add_value.set_hexpand(True)
        self.add_value.connect("activate", lambda *_: self._add_child())
        self.add_row.append(self.add_value)
        add_btn = Gtk.Button(label="Add")
        add_btn.connect("clicked", lambda *_: self._add_child())
        self.add_row.append(add_btn)
        inner.append(self.add_row)

        actions = hbox(6)
        actions.append(Gtk.Box(hexpand=True))
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.set_tooltip_text("Put the key, type and value into the document (Save then writes the file)")
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        actions.append(self.apply_btn)
        inner.append(actions)
        self.append(self.inspector)
        self._show_node(None)

    def load(self, doc: JsonDocument | None) -> None:
        self.doc = doc
        self._bound.clear()
        self._matches = []
        self._match_index = -1
        self.match_label.set_text("")
        self.root_node = JsonNode(self, None, None) if doc else None
        self.root_store.splice(0, self.root_store.get_n_items(), [self.root_node] if self.root_node else [])
        if self.root_node is not None:
            row = self.tree.get_child_row(0)
            if row is not None and self.root_node.is_container:
                row.set_expanded(True)
        self._show_node(None)
        if self.search.get_text():
            self._run_search()

    def _children_of(self, item):
        return item.children() if isinstance(item, JsonNode) else None

    def _factory(self, column: str) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_cell, column)
        factory.connect("bind", self._bind_cell, column)
        factory.connect("unbind", self._unbind_cell, column)
        return factory

    def _setup_cell(self, _factory, item, column: str) -> None:
        lbl = label("", "vb-mono" if column == "value" else ("vb-dim vb-small" if column == "type" else None))
        if column == "key":
            expander = Gtk.TreeExpander()
            expander.set_child(lbl)
            item.set_child(expander)
        else:
            item.set_child(lbl)

    def _bind_cell(self, _factory, item, column: str) -> None:
        row = item.get_item()
        node = row.get_item()
        widget = item.get_child()
        if column == "key":
            widget.set_list_row(row)
            lbl = widget.get_child()
        else:
            lbl = widget
        labels = self._bound.get(node) or (None, None, None)
        idx = ("key", "value", "type").index(column)
        updated = list(labels)
        updated[idx] = lbl
        self._bound[node] = tuple(updated)
        self._fill_label(node, column, lbl)

    def _unbind_cell(self, _factory, item, column: str) -> None:
        row = item.get_item()
        node = row.get_item()
        if column == "key":
            item.get_child().set_list_row(None)
        labels = self._bound.get(node)
        if labels:
            idx = ("key", "value", "type").index(column)
            updated = list(labels)
            updated[idx] = None
            if any(updated):
                self._bound[node] = tuple(updated)
            else:
                self._bound.pop(node, None)

    def _fill_label(self, node: JsonNode, column: str, lbl: Gtk.Label) -> None:
        v = node.value
        if column == "key":
            if node.parent is None:
                text = os.path.basename(self.doc.path) if self.doc else ""
            else:
                text = f"[{node.key}]" if isinstance(node.key, int) else str(node.key)
            lbl.set_text(text)
            lbl.set_tooltip_text(text)
        elif column == "value":
            lbl.set_text(preview(v))
            if isinstance(v, (dict, list)):
                lbl.add_css_class("vb-dim")
            else:
                lbl.remove_css_class("vb-dim")
            lbl.set_tooltip_text(v if isinstance(v, str) and len(v) <= 2000 else None)
        else:
            lbl.set_text(type_name(v))

    def _refresh_node(self, node: JsonNode | None) -> None:
        if node is None:
            return
        labels = self._bound.get(node)
        if not labels:
            return
        for column, lbl in zip(("key", "value", "type"), labels, strict=True):
            if lbl is not None:
                self._fill_label(node, column, lbl)

    def _row_for(self, node: JsonNode) -> Gtk.TreeListRow | None:
        row = self.tree.get_child_row(0)
        chain = []
        n = node
        while n is not None and n.parent is not None:
            chain.append(n)
            n = n.parent
        for n in reversed(chain):
            if row is None or not row.get_expanded():
                return None
            row = row.get_child_row(n.index_in_parent())
        return row

    def select_node(self, node: JsonNode) -> None:
        row = self.tree.get_child_row(0)
        chain = []
        n = node
        while n is not None and n.parent is not None:
            chain.append(n)
            n = n.parent
        for n in reversed(chain):
            if row is None:
                return
            if not row.get_expanded():
                row.set_expanded(True)
            row = row.get_child_row(n.index_in_parent())
        if row is None:
            return
        pos = row.get_position()
        self.selection.set_selected(pos)
        if hasattr(self.view, "scroll_to"):
            self.view.scroll_to(pos, None, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)

    def node_at(self, path: tuple) -> JsonNode | None:
        node = self.root_node
        for key in path:
            if node is None:
                return None
            store = node.children()
            if store is None:
                return None
            pv = node.value
            try:
                idx = list(pv).index(key) if isinstance(pv, dict) else int(key)
            except (ValueError, TypeError):
                return None
            node = store.get_item(idx) if 0 <= idx < store.get_n_items() else None
        return node

    def select_path(self, path: tuple) -> None:
        node = self.node_at(path)
        if node is not None:
            self.select_node(node)

    def _run_search(self) -> None:
        text = self.search.get_text().strip()
        self._search_generation += 1
        gen = self._search_generation
        if not text or self.doc is None:
            self._matches = []
            self._match_index = -1
            self.match_label.set_text("")
            return
        data = self.doc.data

        def done(paths):
            if gen != self._search_generation:
                return
            self._matches = paths
            self._match_index = -1
            if paths:
                self._step_match(1)
            else:
                self.match_label.set_text("no match")

        def failed(_err):
            if gen == self._search_generation:
                self._search_debounce.trigger()

        run_async(find_matches, data, text, on_done=done, on_error=failed)

    def _step_match(self, delta: int) -> None:
        if not self._matches:
            if self.search.get_text().strip():
                self._run_search()
            return
        self._match_index = (self._match_index + delta) % len(self._matches)
        self.match_label.set_text(f"{self._match_index + 1} of {len(self._matches)}")
        self.select_path(self._matches[self._match_index])

    def show_replace(self) -> None:
        if self.doc is None:
            return
        box = vbox(8)
        box.set_size_request(460, -1)
        find = self.replace_find = Gtk.Entry(placeholder_text="text to find", text=self.search.get_text())
        repl = self.replace_with = Gtk.Entry(placeholder_text="replacement")
        for lbl, entry in (("Find", find), ("Replace with", repl)):
            row = hbox(8)
            lab = label(lbl, "vb-dim")
            lab.set_width_chars(12)
            row.append(lab)
            entry.set_hexpand(True)
            row.append(entry)
            box.append(row)
        in_keys = self.replace_in_keys = Gtk.CheckButton(
            label="In keys (dependency names in meta.json are keys)", active=True
        )
        in_values = self.replace_in_values = Gtk.CheckButton(label="In values", active=True)
        whole = self.replace_whole_package = Gtk.CheckButton(
            label="In every JSON file of the package (each changed file is saved at once)"
        )
        box.append(in_keys)
        box.append(in_values)
        box.append(whole)
        dialog = Adw.AlertDialog(
            heading="Replace text",
            body="Plain text, case-sensitive, inside string values and object keys; the structure is never touched.",
        )
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("replace", "Replace")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("replace")
        dialog.set_close_response("cancel")

        def on_response(_d, r):
            if r != "replace":
                return
            old, new = find.get_text(), repl.get_text()
            if not old:
                self.window.toast("Nothing to find.")
                return
            if whole.get_active():
                self.dialog.replace_everywhere(old, new, in_keys.get_active(), in_values.get_active())
                return
            root, n = replace_text(
                self.doc.data, old, new, in_keys=in_keys.get_active(), in_values=in_values.get_active()
            )
            if not n:
                self.window.toast(f"“{old}” does not occur in this file.")
                return
            self.doc.data = root
            self.load(self.doc)
            self.dialog.set_dirty(True)
            self.window.toast(f"Replaced {n} occurrence{'s' if n != 1 else ''}; Save writes the file.", 6)

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _on_selected(self, *_):
        row = self.selection.get_selected_item()
        self._show_node(row.get_item() if row is not None else None)

    def _type_index(self) -> int:
        return int(self.type_dropdown.get_selected())

    def _show_node(self, node: JsonNode | None) -> None:
        self.current = node
        has = node is not None
        self.empty_hint.set_visible(not has)
        self.key_row.set_visible(has and node.parent is not None and isinstance(node.parent.value, dict))
        self.value_row.set_visible(has)
        self.apply_btn.set_visible(has)
        self.parent_btn.set_visible(has and node.parent is not None)
        self.delete_btn.set_visible(has and node.parent is not None)
        self.add_row.set_visible(has and node.is_container)
        if not has:
            self.path_label.set_text("")
            return
        v = node.value
        self.path_label.set_text(format_path(node.path))
        self.path_label.set_tooltip_text(format_path(node.path))
        if node.parent is not None and isinstance(node.parent.value, dict):
            self.key_entry.set_text(str(node.key))
        self.type_dropdown.set_selected(TYPE_NAMES.index(type_name(v)))
        self._show_type(type_name(v), fresh=True)
        if node.is_container:
            self.add_key.set_visible(isinstance(v, dict))
            self.add_key.set_text("")
            self.add_value.set_text("")
            self.add_type.set_selected(0)
            self._sync_add_row()

    def _show_type(self, name: str, fresh: bool = False) -> None:
        node = self.current
        if node is None:
            return
        v = node.value
        if name == "String":
            if fresh or not isinstance(v, str):
                text = v if isinstance(v, str) else ("" if v is None or isinstance(v, (dict, list)) else json.dumps(v))
                self.string_view.get_buffer().set_text(text)
            self.value_stack.set_visible_child_name("String")
        elif name == "Number":
            if isinstance(v, bool):
                text = "1" if v else "0"
            elif isinstance(v, (int, float)):
                text = json.dumps(v)
            elif isinstance(v, str):
                try:
                    text = json.dumps(parse_number(v))
                except ValueError:
                    text = ""
            else:
                text = ""
            self.number_entry.set_text(text)
            self.value_stack.set_visible_child_name("Number")
        elif name == "Boolean":
            if isinstance(v, str):
                active = v.strip().lower() in ("true", "1", "yes", "on")
            else:
                active = bool(v) if not isinstance(v, (dict, list)) else False
            self.bool_switch.set_active(active)
            self.value_stack.set_visible_child_name("Boolean")
        elif name == "Null":
            self.value_stack.set_visible_child_name("Null")
        else:
            if isinstance(v, (dict, list)) and type_name(v) == name:
                self.container_label.set_text(preview(v) + " (edit them in the tree; add one below)")
            else:
                self.container_label.set_text(f"becomes an empty {name.lower()} on Apply")
            self.value_stack.set_visible_child_name("Object")

    def _value_from_inspector(self, name: str, current):
        if name == "String":
            buf = self.string_view.get_buffer()
            return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        if name == "Number":
            return parse_number(self.number_entry.get_text())
        if name == "Boolean":
            return bool(self.bool_switch.get_active())
        if name == "Null":
            return None
        if name == "Object":
            return current if isinstance(current, dict) else {}
        return current if isinstance(current, list) else []

    def _apply(self) -> None:
        node = self.current
        if node is None or self.doc is None:
            return
        name = TYPE_NAMES[self._type_index()]
        try:
            new_value = self._value_from_inspector(name, node.value)
        except ValueError as err:
            self.window.toast(str(err), 6)
            return
        changed = False
        if self.key_row.get_visible():
            new_key = self.key_entry.get_text()
            if new_key != node.key:
                try:
                    rename_key(self.doc.data, node.path, new_key)
                except (KeyError, ValueError, TypeError) as err:
                    self.window.toast(str(err).strip("'"), 6)
                    return
                node.key = new_key
                changed = True
        old_value = node.value
        if new_value is not old_value and not (
            type(new_value) is type(old_value) and new_value == old_value and not isinstance(old_value, (dict, list))
        ):
            was_container = isinstance(old_value, (dict, list))
            if was_container:
                row = self._row_for(node)
                if row is not None and row.get_expanded():
                    row.set_expanded(False)
                node.store = None
            self.doc.data = set_at(self.doc.data, node.path, new_value)
            changed = True
        if changed:
            self._refresh_node(node)
            self.dialog.set_dirty(True)
            self._show_node(node)

    def _select_parent(self) -> None:
        if self.current is not None and self.current.parent is not None:
            self.select_node(self.current.parent)

    def _delete_current(self) -> None:
        node = self.current
        if node is None or node.parent is None or self.doc is None:
            return
        parent = node.parent
        idx = node.index_in_parent()
        try:
            delete_at(self.doc.data, node.path)
        except (KeyError, IndexError, ValueError, TypeError) as err:
            self.window.toast(f"Could not delete: {err}", 6)
            return
        store = parent.store
        if store is not None and idx < store.get_n_items() and store.get_item(idx) is node:
            store.remove(idx)
            if isinstance(parent.value, list):
                for i in range(idx, store.get_n_items()):
                    sibling = store.get_item(i)
                    sibling.key = i
                    self._refresh_node(sibling)
        self._bound.pop(node, None)
        self._refresh_node(parent)
        self.dialog.set_dirty(True)
        self.select_node(parent)
        self.window.toast(f"Deleted {format_path(node.path)}; Save writes the file.", 5)

    def _sync_add_row(self) -> None:
        name = TYPE_NAMES[int(self.add_type.get_selected())]
        self.add_value.set_visible(name in ("String", "Number", "Boolean"))
        self.add_value.set_placeholder_text(
            {"String": "text", "Number": "a number", "Boolean": "true or false"}.get(name, "")
        )

    def _add_child(self) -> None:
        parent = self.current
        if parent is None or self.doc is None or not parent.is_container:
            return
        name = TYPE_NAMES[int(self.add_type.get_selected())]
        text = self.add_value.get_text()
        try:
            if name == "String":
                value = text
            elif name == "Number":
                value = parse_number(text)
            elif name == "Boolean":
                value = text.strip().lower() in ("true", "1", "yes", "on")
            elif name == "Null":
                value = None
            elif name == "Object":
                value = {}
            else:
                value = []
            key = self.add_key.get_text() if isinstance(parent.value, dict) else None
            new_key = insert_at(self.doc.data, parent.path, key, value)
        except (KeyError, ValueError, TypeError) as err:
            self.window.toast(str(err).strip("'"), 6)
            return
        if parent.store is not None:
            parent.store.append(JsonNode(self, parent, new_key))
        self._refresh_node(parent)
        self.dialog.set_dirty(True)
        row = self._row_for(parent)
        if row is not None and not row.get_expanded():
            row.set_expanded(True)
        self._show_node(parent)
        self.add_key.grab_focus() if isinstance(parent.value, dict) else self.add_value.grab_focus()
        self.window.toast(f"Added {format_path(parent.path + (new_key,))}; Save writes the file.", 4)


class PackageEditorDialog(Adw.Dialog):

    def __init__(self, window, filename: str, folder: str, has_backup: bool):
        super().__init__(title=f"Edit files · {filename}", content_width=1180, content_height=800)
        self.window = window
        self.backend = window.backend
        self.filename = filename
        self.folder = folder
        self.current_file: FileNode | None = None
        self.doc: JsonDocument | None = None
        self.dirty = False
        self._switching_to: FileNode | None = None
        self.set_follows_content_size(False)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=filename, subtitle=folder)
        header.set_title_widget(self.title_widget)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.set_tooltip_text("Reload the file from disk, dropping the unsaved changes")
        self.revert_btn.connect("clicked", lambda *_: self.revert())
        header.pack_end(self.revert_btn)
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.set_tooltip_text("Write the file in VaM's own layout and re-index the package (Ctrl+S)")
        self.save_btn.connect("clicked", lambda *_: self.save())
        header.pack_end(self.save_btn)
        toolbar.add_top_bar(header)

        content = vbox(0)
        self.banner = Adw.Banner(
            title=f"No {filename}.orig beside this package: edits cannot be undone with Restore original.",
            revealed=not has_backup,
        )
        self.backup_note = f"the original is kept as {filename}.orig" if has_backup else ""
        content.append(self.banner)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(340)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)

        self.file_store = Gio.ListStore.new(FileNode)
        self.file_tree = Gtk.TreeListModel.new(self.file_store, False, True, self._file_children)
        self.file_selection = Gtk.SingleSelection.new(self.file_tree)
        self.file_selection.set_autoselect(False)
        self.file_selection.set_can_unselect(True)
        self.file_selection.connect("notify::selected-item", self._on_file_selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_file_row)
        factory.connect("bind", self._bind_file_row)
        factory.connect("unbind", lambda _f, item: item.get_child().set_list_row(None))
        self.file_view = Gtk.ListView.new(self.file_selection, factory)
        self.file_view.add_css_class("navigation-sidebar")
        self.file_view.connect("activate", self._on_file_activated)
        files_scroller = Gtk.ScrolledWindow(
            child=self.file_view, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, min_content_width=300
        )
        left = vbox(0)
        self.files_label = label("Reading the folder…", "vb-dim vb-small", wrap=True)
        for m in ("start", "end", "top", "bottom"):
            getattr(self.files_label, f"set_margin_{m}")(8)
        left.append(self.files_label)
        left.append(files_scroller)
        paned.set_start_child(left)

        self.pages = Gtk.Stack()
        self.pages.add_named(
            Adw.StatusPage(
                title="Pick a file",
                description="JSON files (meta.json, scenes, presets) are edited as a tree, so the syntax cannot break.",
                icon_name="document-edit-symbolic",
            ),
            "empty",
        )
        self.json_editor = JsonEditor(self)
        self.pages.add_named(self.json_editor, "json")
        self.text_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        text_box = vbox(0)
        self.text_note = label("", "vb-dim vb-small")
        for m in ("start", "end", "top", "bottom"):
            getattr(self.text_note, f"set_margin_{m}")(8)
        text_box.append(self.text_note)
        text_box.append(Gtk.ScrolledWindow(child=self.text_view, vexpand=True))
        self.pages.add_named(text_box, "text")
        self.binary_page = Adw.StatusPage(title="", description="", icon_name="package-x-generic-symbolic")
        reveal = Gtk.Button(label="Show in folder", halign=Gtk.Align.CENTER)
        reveal.add_css_class("pill")
        reveal.connect("clicked", lambda *_: self._reveal_current())
        self.binary_page.set_child(reveal)
        self.pages.add_named(self.binary_page, "binary")
        paned.set_end_child(self.pages)
        content.append(paned)
        toolbar.set_content(content)
        self.set_child(toolbar)

        shortcuts = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.BUBBLE)
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>s"), Gtk.CallbackAction.new(lambda *_: self.save() or True)
            )
        )
        shortcuts.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>f"),
                Gtk.CallbackAction.new(lambda *_: self.json_editor.search.grab_focus() or True),
            )
        )
        self.add_controller(shortcuts)
        self.connect("close-attempt", self._on_close_attempt)
        self.set_dirty(False)
        self.reload_files(select="meta.json")

    def reload_files(self, select: str | None = None) -> None:
        def done(files):
            nodes = build_file_tree(files)
            total = sum(f.size for f in files)
            self.files_label.set_text(
                f"{len(files)} files · {human_size(total)}" + (f" · {self.backup_note}" if self.backup_note else "")
            )
            self.file_store.splice(0, self.file_store.get_n_items(), nodes)
            if select:
                self.select_file(select)

        run_async(
            list_package_files,
            self.folder,
            on_done=done,
            on_error=lambda e: self.window.toast(f"Could not read {self.folder}: {e}", 8),
        )

    def _file_children(self, item):
        if isinstance(item, FileNode) and item.is_dir:
            store = Gio.ListStore.new(FileNode)
            store.splice(0, 0, item.children)
            return store
        return None

    def _setup_file_row(self, _factory, item) -> None:
        expander = Gtk.TreeExpander()
        row = hbox(6)
        icon = Gtk.Image()
        row.append(icon)
        name = label("")
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_hexpand(True)
        row.append(name)
        row.append(label("", "vb-dim vb-small"))
        expander.set_child(row)
        item.set_child(expander)

    def _bind_file_row(self, _factory, item) -> None:
        row = item.get_item()
        node = row.get_item()
        expander = item.get_child()
        expander.set_list_row(row)
        box = expander.get_child()
        icon, name, size = box.get_first_child(), box.get_first_child().get_next_sibling(), box.get_last_child()
        if node.is_dir:
            icon.set_from_icon_name("folder-symbolic")
        elif node.kind == KIND_JSON:
            icon.set_from_icon_name("text-x-generic-symbolic")
        elif node.kind == KIND_TEXT:
            icon.set_from_icon_name("text-x-generic-symbolic")
        else:
            icon.set_from_icon_name("package-x-generic-symbolic")
        name.set_text(node.name)
        name.set_tooltip_text(node.rel or node.name)
        size.set_text(human_size(node.size) if not node.is_dir or node.size else "")
        if node.is_dir or node.kind == KIND_JSON:
            name.remove_css_class("vb-dim")
        else:
            name.add_css_class("vb-dim")

    def _on_file_activated(self, _view, position: int) -> None:
        row = self.file_tree.get_row(position)
        if row is not None and row.get_item().is_dir:
            row.set_expanded(not row.get_expanded())

    def select_file(self, rel: str) -> None:
        for i in range(self.file_tree.get_n_items()):
            row = self.file_tree.get_row(i)
            if row is not None and row.get_item().rel == rel:
                self.file_selection.set_selected(i)
                return

    def _on_file_selected(self, *_):
        row = self.file_selection.get_selected_item()
        node = row.get_item() if row is not None else None
        if node is None or node.is_dir or node is self.current_file:
            return
        if self.dirty and self.current_file is not None:
            self._ask_unsaved(lambda: self._open_file(node), lambda: self._reselect_current())
            return
        self._open_file(node)

    def _reselect_current(self) -> None:
        if self.current_file is not None:
            self.select_file(self.current_file.rel)

    def _open_file(self, node: FileNode) -> None:
        self.current_file = node
        self.doc = None
        self.set_dirty(False)
        path = os.path.join(self.folder, *node.rel.split("/"))
        self.title_widget.set_subtitle(node.rel)
        if node.kind == KIND_JSON:
            self.pages.set_visible_child_name("json")
            self.json_editor.load(None)

            def loaded(doc):
                if self.current_file is not node:
                    return
                self.doc = doc
                self.json_editor.load(doc)
                self.set_dirty(False)

            def failed(err):
                if self.current_file is not node:
                    return
                self._show_binary(node, f"{human_size(node.size)} · cannot be edited: {err}")

            run_async(load_json_document, path, on_done=loaded, on_error=failed)
        elif node.kind == KIND_TEXT and node.size <= MAX_TEXT_PREVIEW:
            self.pages.set_visible_child_name("text")
            self.text_note.set_text(f"{node.rel} · {human_size(node.size)} · read-only (not a JSON file)")
            self.text_view.get_buffer().set_text("")

            def read(p):
                with open(p, "rb") as f:
                    return f.read().decode("utf-8", errors="replace")

            def show(text):
                if self.current_file is node:
                    self.text_view.get_buffer().set_text(text)

            run_async(read, path, on_done=show, on_error=lambda e: self.window.toast(f"Could not read {node.rel}: {e}"))
        else:
            self._show_binary(
                node,
                f"{human_size(node.size)} · "
                + (
                    "too large to show here" if node.kind == KIND_TEXT else "a binary file (asset, texture, morph data)"
                ),
            )

    def _show_binary(self, node: FileNode, description: str) -> None:
        self.pages.set_visible_child_name("binary")
        self.binary_page.set_title(node.name)
        self.binary_page.set_description(description)

    def _reveal_current(self) -> None:
        if self.current_file is not None:
            open_in_file_manager(os.path.join(self.folder, *self.current_file.rel.split("/")))

    def set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.save_btn.set_sensitive(dirty)
        self.revert_btn.set_sensitive(dirty)
        self.set_can_close(not dirty)
        name = self.current_file.rel if self.current_file else ""
        self.title_widget.set_subtitle(("• " if dirty else "") + name if name else self.folder)

    def save(self, then=None) -> None:
        doc = self.doc
        if doc is None or not self.dirty:
            if then:
                then()
            return
        node = self.current_file
        self.save_btn.set_sensitive(False)

        def write(d):
            n = save_json_document(d)
            self.backend.lib.reindex_package(self.filename)
            return n

        def done(n):
            if self.current_file is node and self.doc is doc:
                self.set_dirty(False)
            self.window.toast(f"Saved {node.rel if node else ''} ({human_size(n)}); the package was re-indexed.", 5)
            self.reload_files()
            if then:
                then()

        def failed(err):
            self.save_btn.set_sensitive(True)
            self.window.toast(f"Could not save: {err}", 10)

        run_async(write, doc, on_done=done, on_error=failed)

    def revert(self) -> None:
        if self.current_file is not None:
            self._open_file(self.current_file)

    def _ask_unsaved(self, on_discard, on_cancel=None) -> None:
        name = self.current_file.rel if self.current_file else "the file"
        dialog = Adw.AlertDialog(heading="Unsaved changes", body=f"{name} has changes that are not written yet.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_d, r):
            if r == "discard":
                self.set_dirty(False)
                on_discard()
            elif r == "save":
                self.save(then=on_discard)
            elif on_cancel:
                on_cancel()

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _on_close_attempt(self, *_):
        if not self.dirty:
            self.force_close()
            return
        self._ask_unsaved(self.force_close)

    def replace_everywhere(self, old: str, new: str, in_keys: bool, in_values: bool) -> None:
        if self.dirty:
            self.window.toast("Save or revert the open file first; the replacement writes every file it changes.", 8)
            return

        def done(res):
            n_files = len(res.files)
            msg = f"Replaced {res.total} occurrence{'s' if res.total != 1 else ''} in {n_files} file{'s' if n_files != 1 else ''}"
            if res.errors:
                msg += f"; {len(res.errors)} file(s) could not be read: " + ", ".join(list(res.errors)[:3])
            self.window.toast(msg, 8)
            if res.files:
                run_async(self.backend.lib.reindex_package, self.filename)
                if self.current_file is not None:
                    self._open_file(self.current_file)

        self.window.run_op(
            lambda: replace_in_package(self.folder, old, new, in_keys=in_keys, in_values=in_values), then=done
        )
