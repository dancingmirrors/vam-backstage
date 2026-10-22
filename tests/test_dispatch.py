import pytest

try:
    from gi.repository import GLib  # noqa: F401

    _GI_OK = True
except Exception:  # noqa: BLE001 - a missing or broken PyGObject means we can't test here
    _GI_OK = False

if not _GI_OK:
    pytest.skip("PyGObject not available", allow_module_level=True)

from vambackstage.ui import dispatch  # noqa: E402
from vambackstage.ui.dispatch import Debouncer  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0
        self.timers: dict[int, tuple[int, object]] = {}
        self.next_id = 1

    def get_monotonic_time(self) -> int:
        return self.now

    def timeout_add(self, ms, fn) -> int:
        self.next_id += 1
        self.timers[self.next_id] = (self.now + int(ms) * 1000, fn)
        return self.next_id

    def source_remove(self, source) -> None:
        del self.timers[source]

    def advance(self, ms: int) -> None:
        end = self.now + ms * 1000
        while due := sorted((t, i) for i, (t, _fn) in self.timers.items() if t <= end):
            self.now, source = due[0]
            self.timers.pop(source)[1]()
        self.now = end


def test_refreshes_are_spaced_out_only_while_busy(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dispatch, "GLib", clock)
    fired, busy = [], [False]
    d = Debouncer(100, lambda: fired.append(clock.now // 1000), 1000, lambda: busy[0])
    for _ in range(5):
        d.trigger()
        clock.advance(50)
    assert fired == [], "each trigger pushes the refresh back"
    clock.advance(100)
    assert fired == [300]
    busy[0] = True
    for _ in range(30):
        d.trigger()
        clock.advance(100)
    assert fired == [300, 1300, 2300, 3300], "at most one a second, and none is lost"
    busy[0] = False
    d.trigger()
    clock.advance(100)
    assert fired[-1] == 3450
    busy[0] = True
    d.trigger()
    clock.advance(100)
    d.trigger()
    clock.advance(100)
    assert fired[-1] == 3450, "the second trigger waits out the pause"
    d.hurry()
    clock.advance(100)
    assert fired[-1] == 3750, "unless something has to show at once"
    d.hurry()
    clock.advance(1000)
    assert fired[-1] == 3750, "and nothing was pending"
