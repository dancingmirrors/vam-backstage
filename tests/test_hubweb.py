import pytest

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk  # noqa: F401

    _GTK_OK = True
except Exception:  # noqa: BLE001 - a missing or broken PyGObject/typelib means we can't test here
    _GTK_OK = False

if not _GTK_OK:
    pytest.skip("GTK 4 / libadwaita not available", allow_module_level=True)

from vambackstage.ui import hubweb  # noqa: E402


def test_stall_timeout_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("VAM_BACKSTAGE_HUB_TIMEOUT", raising=False)
    assert hubweb._stall_timeout() == hubweb.DEFAULT_STALL_TIMEOUT_S


@pytest.mark.parametrize("value", ["0", "-5", "", "garbage", "2.5"])
def test_stall_timeout_falls_back_on_bad_or_nonpositive_values(monkeypatch, value):
    monkeypatch.setenv("VAM_BACKSTAGE_HUB_TIMEOUT", value)
    assert hubweb._stall_timeout() == hubweb.DEFAULT_STALL_TIMEOUT_S


def test_stall_timeout_honours_positive_override(monkeypatch):
    monkeypatch.setenv("VAM_BACKSTAGE_HUB_TIMEOUT", "3")
    assert hubweb._stall_timeout() == 3


@pytest.mark.parametrize("value", ["600", "601", "999999", "9999999999"])
def test_stall_timeout_clamps_large_values(monkeypatch, value):
    monkeypatch.setenv("VAM_BACKSTAGE_HUB_TIMEOUT", value)
    assert hubweb._stall_timeout() == 600
