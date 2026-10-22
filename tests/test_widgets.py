import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gtk  # noqa: F401

    _GTK_OK = Gtk.init_check() and Gdk.Display.get_default() is not None
except Exception:  # noqa: BLE001 - a missing or broken PyGObject/typelib means we can't test here
    _GTK_OK = False

if not _GTK_OK:
    pytest.skip("GTK 4 / libadwaita or a display not available", allow_module_level=True)

from vambackstage.ui.widgets import FilterList  # noqa: E402


def test_a_folded_away_choice_leaves_once_another_is_picked():
    picked = []
    fl = FilterList("Author", picked.append, show_more_after=3)
    options = [(None, "All authors", 9)] + [(f"a{i}", f"a{i}", 1) for i in range(8)]
    fl.set_options(options, None)
    fl._toggle_more()
    fl._buttons["a6"].set_active(True)
    fl._toggle_more()
    assert fl._buttons["a6"].get_visible(), "the choice stays in view while the rest is folded away"
    fl._buttons[None].set_active(True)
    fl.set_options(options, None)
    assert picked == ["a6", None] and not fl._buttons["a6"].get_visible()
    fl.select("a7")
    assert fl._buttons["a7"].get_visible() and fl._buttons["a7"].get_active()
    fl.select(None)
    assert not fl._buttons["a7"].get_visible()
