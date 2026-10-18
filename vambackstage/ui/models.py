from __future__ import annotations

from gi.repository import Gio, GObject


class DictItem(GObject.Object):

    __gtype_name__ = "VbDictItem"

    def __init__(self, data: dict, key: str):
        super().__init__()
        self.data = data
        self.key = key


def rebuild_store(store: Gio.ListStore, rows: list[dict], key_of) -> dict[str, DictItem]:
    items = [DictItem(r, key_of(r)) for r in rows]
    store.splice(0, store.get_n_items(), items)
    return {it.key: it for it in items}


def human_size(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
