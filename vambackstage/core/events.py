from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

Handler = Callable[[str, Any], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._any: list[Handler] = []

    def subscribe(self, channel: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._handlers[channel].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._handlers[channel].remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def subscribe_all(self, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._any.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._any.remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def emit(self, channel: str, payload: Any = None) -> None:
        with self._lock:
            handlers = list(self._handlers.get(channel, ())) + list(self._any)
        for h in handlers:
            try:
                h(channel, payload)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("event handler failed for %s", channel)
