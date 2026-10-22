from __future__ import annotations

from collections import Counter

from gi.repository import Adw, Gtk

from .dispatch import Debouncer, run_async
from .models import human_size
from .widgets import chip, hbox, icon_button, label

STATUS_STYLE = {"queued": "muted", "active": "success", "completed": "success", "failed": "error", "cancelled": "muted"}
MAX_ROWS = 300


class DownloadRow(Gtk.Box):
    def __init__(self, view, d: dict):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("vb-download-row")
        self.view = view
        self.id = d["id"]
        self.shape = self.shape_of(d)
        top = hbox(8)
        self.title = label(d.get("display_name") or d["package_ref"])
        self.title.set_hexpand(True)
        top.append(self.title)
        self.chip = chip(d["status"].upper(), STATUS_STYLE.get(d["status"], "muted"))
        top.append(self.chip)
        if d["status"] in ("queued", "active"):
            top.append(icon_button("process-stop-symbolic", "Cancel", lambda: view.backend.downloads.cancel(self.id)))
        elif d["status"] == "failed":
            top.append(icon_button("view-refresh-symbolic", "Retry", lambda: view.backend.downloads.retry(self.id)))
            top.append(
                icon_button("edit-delete-symbolic", "Remove", lambda: view.backend.downloads.remove_failed(self.id))
            )
        self.append(top)
        self.sub = label(
            d["package_ref"] + (f" · for {d['parent_ref']}" if d.get("parent_ref") else ""), "vb-mono vb-dim"
        )
        self.append(self.sub)
        self.bar = Gtk.ProgressBar(show_text=False)
        self.append(self.bar)
        self.info = label("", "vb-dim vb-small")
        self.append(self.info)
        self.update(d)

    @staticmethod
    def shape_of(d: dict) -> tuple:
        return d["status"], d.get("priority"), d.get("display_name"), d["package_ref"], d.get("parent_ref")

    def update(self, d: dict):
        self.data = d
        self.bar.set_fraction(min(1.0, (d.get("progress") or 0) / 100))
        self.bar.set_visible(d["status"] in ("queued", "active", "completed"))
        size = d.get("file_size")
        parts = []
        if d["status"] == "active":
            parts.append(f"{human_size(d.get('bytes_loaded'))}" + (f" of {human_size(size)}" if size else ""))
            if d.get("speed"):
                parts.append(f"{human_size(d['speed'])}/s")
                if size and d["speed"] > 0:
                    remaining = max(0, size - (d.get("bytes_loaded") or 0)) / d["speed"]
                    parts.append(f"{int(remaining)}s left")
        elif d["status"] == "failed" and d.get("error"):
            parts.append(d["error"])
        elif size:
            parts.append(human_size(size))
        self.info.set_text(" · ".join(parts))
        self.info.set_visible(bool(parts))

    def progress(self, p: dict):
        self.bar.set_fraction(min(1.0, (p.get("progress") or 0) / 100))
        size = p.get("file_size")
        text = human_size(p.get("bytes_loaded")) + (f" of {human_size(size)}" if size else "")
        if p.get("speed"):
            text += f" · {human_size(p['speed'])}/s"
            if size and p["speed"] > 0:
                text += f" · {int(max(0, size - (p.get('bytes_loaded') or 0)) / p['speed'])}s left"
        self.info.set_text(text)
        self.info.set_visible(True)


class DownloadsView(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.backend = window.backend
        self.rows: dict[int, DownloadRow] = {}
        bar = hbox(8)
        for m in ("start", "end"):
            getattr(bar, f"set_margin_{m}")(12)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        self.summary = label("", "vb-dim")
        bar.append(self.summary)
        bar.append(Gtk.Box(hexpand=True))
        self.pause_btn = Gtk.Button(label="Pause all")
        self.pause_btn.connect("clicked", lambda *_: self._toggle_pause())
        bar.append(self.pause_btn)
        b = Gtk.Button(label="Cancel all")
        b.add_css_class("destructive-action")
        b.connect(
            "clicked",
            lambda *_: window.confirm(
                "Cancel all downloads?",
                "Partially downloaded files are deleted.",
                "Cancel all",
                self.backend.downloads.cancel_all,
            ),
        )
        bar.append(b)
        b = Gtk.Button(label="Clear completed")
        b.connect("clicked", lambda *_: self.backend.downloads.clear_completed())
        bar.append(b)
        b = Gtk.Button(label="Clear failed")
        b.connect("clicked", lambda *_: self.backend.downloads.clear_failed())
        bar.append(b)
        self.append(bar)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        for m in ("start", "end", "bottom"):
            getattr(self.list, f"set_margin_{m}")(12)
        self.empty = Adw.StatusPage(
            title="No downloads",
            description="Installs from the Hub show up here with their progress.",
            icon_name="folder-download-symbolic",
        )
        self.empty.set_vexpand(True)
        self.stack = Gtk.Stack()
        self.stack.add_named(
            Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.list, vexpand=True), "list"
        )
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)
        self.more = label("", "vb-dim vb-small")
        self.more.set_margin_top(8)
        self.more.set_margin_bottom(8)
        self.more.set_margin_start(10)
        self.more_row = Gtk.ListBoxRow(child=self.more, activatable=False, selectable=False, visible=False)
        self.list.append(self.more_row)
        self.order: list[int] = []
        self._refresh_debounce = Debouncer(100, self.refresh, 1000, lambda: self.backend.downloads.busy)
        window.bridge.connect("downloads:updated", lambda _p: self._refresh_debounce.trigger())
        window.bridge.connect("download:progress", self._on_progress)

    def on_shown(self):
        self.refresh()

    def refresh(self):
        if self.window.stack.get_visible_child() is not self:
            return
        run_async(self.backend.downloads.list, on_done=self._loaded)

    def _loaded(self, items: list[dict]):
        order = {"active": 0, "queued": 1, "failed": 2, "completed": 3, "cancelled": 4}
        items = sorted(
            items,
            key=lambda d: (order.get(d["status"], 9), 0 if d["priority"] == "direct" else 1, d["created_at"], d["id"]),
        )
        self._place_rows(items[:MAX_ROWS])
        rest = Counter(d["status"] for d in items[MAX_ROWS:])
        self.more.set_text(
            f"{len(items) - MAX_ROWS} more not listed: " + ", ".join(f"{n} {s}" for s, n in rest.items())
        )
        self.more_row.set_visible(len(items) > MAX_ROWS)
        self.stack.set_visible_child_name("list" if items else "empty")
        active = sum(1 for d in items if d["status"] in ("active", "queued"))
        done = sum(1 for d in items if d["status"] == "completed")
        failed = sum(1 for d in items if d["status"] == "failed")
        self.summary.set_text(f"{active} in progress · {done} completed · {failed} failed")
        paused = self.backend.downloads.paused
        self.pause_btn.set_label("Resume all" if paused else "Pause all")

    def _place_rows(self, shown: list[dict]) -> None:
        want = {d["id"]: d for d in shown}
        for i, row in list(self.rows.items()):
            d = want.get(i)
            if d is None or row.shape != DownloadRow.shape_of(d):
                self.list.remove(row.get_parent())
                del self.rows[i]
            elif d != row.data:
                row.update(d)
        if [i for i in self.order if i in self.rows] != [i for i in want if i in self.rows]:
            for row in self.rows.values():
                self.list.remove(row.get_parent())
            self.rows = {}
        for k, d in enumerate(shown):
            if d["id"] not in self.rows:
                self.rows[d["id"]] = DownloadRow(self, d)
                self.list.insert(self.rows[d["id"]], k)
        self.order = list(want)

    def _on_progress(self, p: dict):
        row = self.rows.get(p.get("id"))
        if row:
            row.progress(p)

    def _toggle_pause(self):
        if self.backend.downloads.paused:
            self.backend.downloads.resume_all()
        else:
            self.backend.downloads.pause_all()
