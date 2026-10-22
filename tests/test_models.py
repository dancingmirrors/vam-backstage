import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk

    _GTK_OK = True
except Exception:  # noqa: BLE001 - a missing or broken PyGObject/typelib means we can't test here
    _GTK_OK = False

if not _GTK_OK:
    pytest.skip("GTK 4 not available", allow_module_level=True)

from vambackstage.ui.models import DictItem, rebuild_store, sync_store  # noqa: E402


def rows(pairs) -> list[dict]:
    return [{"k": k, "v": v} for k, v in pairs]


def key_of(row: dict) -> str:
    return row["k"]


def keys(store) -> list[str]:
    return [store.get_item(i).key for i in range(store.get_n_items())]


def filled(n: int = 100):
    store = Gio.ListStore.new(DictItem)
    return store, rebuild_store(store, rows((f"r{i}", i) for i in range(n)), key_of)


def test_only_the_rows_that_changed_are_touched():
    store, current = filled()
    selection = Gtk.SingleSelection.new(store)
    selection.set_autoselect(False)
    selection.set_selected(5)
    changes = []
    store.connect("items-changed", lambda _s, *change: changes.append(change))
    fresh = rows((f"r{i}", -1 if i == 5 else i) for i in range(100) if i != 7)
    fresh[3:3] = rows([("a", 0)])
    fresh.append({"k": "z", "v": 0})
    out, changed = sync_store(store, fresh, key_of, current)
    assert keys(store) == [f"r{i}" for i in range(100) if i != 7] + ["a", "z"] == list(out)
    assert changed == [current["r5"]] and out["r5"].data["v"] == -1, "a changed row keeps its item"
    assert all(out[k] is current[k] for k in out if k not in ("a", "z"))
    assert changes == [(7, 1, 0), (5, 1, 1), (99, 0, 2)]
    assert selection.get_selected_item() is current["r5"], "and stays selected"


def test_a_block_of_changes_is_one_splice_and_order_is_kept():
    store, current = filled()
    changes = []
    store.connect("items-changed", lambda _s, *change: changes.append(change))
    fresh = rows((f"r{i}", -i if 10 <= i < 60 else i) for i in reversed(range(100)))
    out, changed = sync_store(store, fresh, key_of, current)
    assert keys(store) == [f"r{i}" for i in range(100)], "rows that stay keep their place"
    assert changes == [(10, 50, 50)] and len(changed) == 50


def test_falls_back_to_a_rebuild():
    store, current = filled(200)
    scattered = rows((f"r{i}", -i if i % 2 else i) for i in range(200))
    out, changed = sync_store(store, scattered, key_of, current)
    assert changed == [] and out["r51"] is not current["r51"] and out["r51"].data["v"] == -51

    store, current = filled(10)
    twice = rows((f"r{i}", i) for i in range(10)) + rows([("r3", 99)])
    out, changed = sync_store(store, twice, key_of, current)
    assert store.get_n_items() == 11 and changed == [], "duplicate keys are shown as before"
