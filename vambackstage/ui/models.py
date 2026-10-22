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


def sync_store(
    store: Gio.ListStore, rows: list[dict], key_of, current: dict[str, DictItem]
) -> tuple[dict[str, DictItem], list[DictItem]]:
    fresh = {key_of(r): r for r in rows}
    if not current or len(fresh) != len(rows) or len(current) != store.get_n_items():
        return rebuild_store(store, rows, key_of), []
    items = list(current.values())
    changed = [it for it in items if it.key in fresh and fresh[it.key] != it.data]
    touched = {it.key for it in changed}.union(k for k in current if k not in fresh)
    runs, i = [], len(items)
    while i:
        end = i
        while i and items[i - 1].key in touched:
            i -= 1
        if i < end:
            runs.append((i, end))
        else:
            i -= 1
    if len(runs) > max(64, len(items) // 16):
        return rebuild_store(store, rows, key_of), []
    for start, end in runs:
        kept = [it for it in items[start:end] if it.key in fresh]
        for item in kept:
            item.data = fresh[item.key]
        store.splice(start, end - start, kept)
    out = {it.key: it for it in items if it.key in fresh}
    added = [DictItem(r, k) for k, r in fresh.items() if k not in current]
    if added:
        store.splice(len(out), 0, added)
        out.update((it.key, it) for it in added)
    return out, changed


def human_size(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
