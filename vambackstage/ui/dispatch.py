from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

from ..core.events import EventBus

log = logging.getLogger(__name__)


def on_main(fn: Callable, *args, **kwargs) -> None:

    def _call():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("main-loop callback failed: %s", getattr(fn, "__name__", fn))
        return False

    GLib.idle_add(_call)


def run_async(
    fn: Callable, *args, on_done: Callable | None = None, on_error: Callable | None = None, name: str | None = None
) -> threading.Thread:

    def runner():
        try:
            result = fn(*args)
        except Exception as err:
            log.exception("background task failed: %s", getattr(fn, "__name__", fn))
            if on_error:
                on_main(on_error, err)
            return
        if on_done:
            on_main(on_done, result)

    th = threading.Thread(target=runner, daemon=True, name=name or getattr(fn, "__name__", "task"))
    th.start()
    return th


class EventBridge:

    def __init__(self, events: EventBus):
        self._handlers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        events.subscribe_all(self._on_event)

    def _on_event(self, channel: str, payload: Any) -> None:
        GLib.idle_add(self._dispatch, channel, payload)

    def _dispatch(self, channel: str, payload: Any) -> bool:
        for h in list(self._handlers.get(channel, ())):
            try:
                h(payload)
            except Exception:
                log.exception("event handler failed for %s", channel)
        return False

    def connect(self, channel: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        self._handlers[channel].append(handler)

        def disconnect():
            try:
                self._handlers[channel].remove(handler)
            except ValueError:
                pass

        return disconnect


class Debouncer:

    def __init__(self, delay_ms: int, fn: Callable[[], None]):
        self.delay_ms = delay_ms
        self.fn = fn
        self._source: int | None = None

    def trigger(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
        self._source = GLib.timeout_add(self.delay_ms, self._fire)

    def _fire(self) -> bool:
        self._source = None
        try:
            self.fn()
        except Exception:
            log.exception("debounced callback failed")
        return False

    def cancel(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
