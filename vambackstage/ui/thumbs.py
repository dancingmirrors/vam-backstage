from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gdk, GLib

from .dispatch import on_main

log = logging.getLogger(__name__)

_MAX_TEXTURES = 800


class TextureLoader:
    def __init__(self, backend, workers: int = 4):
        self.backend = backend
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumb")
        self._cache: OrderedDict[str, Gdk.Texture | None] = OrderedDict()
        self._pending: dict[str, list[Callable]] = {}
        self._stale: set[str] = set()

    def get(self, key: str, callback: Callable[[Gdk.Texture | None], None]) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
            callback(self._cache[key])
            return
        waiters = self._pending.get(key)
        if waiters is not None:
            waiters.append(callback)
            return
        self._pending[key] = [callback]
        self._pool.submit(self._load, key)

    def _load(self, key: str) -> None:
        texture = None
        try:
            data = self.backend.thumbs.get(key)
            if data:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error as err:
            log.debug("thumbnail decode failed for %s: %s", key, err)
        except Exception:
            log.exception("thumbnail load failed for %s", key)
        on_main(self._deliver, key, texture)

    def _deliver(self, key: str, texture: Gdk.Texture | None) -> None:
        if key in self._stale:
            self._stale.discard(key)
            self._pool.submit(self._load, key)
            return
        self._cache[key] = texture
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_TEXTURES:
            self._cache.popitem(last=False)
        for cb in self._pending.pop(key, ()):
            try:
                cb(texture)
            except Exception:
                log.exception("thumbnail callback failed")

    def invalidate(self, keys) -> None:
        for k in keys:
            self._cache.pop(k, None)
            if k in self._pending:
                self._stale.add(k)
        self.backend.thumbs.invalidate(keys)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
