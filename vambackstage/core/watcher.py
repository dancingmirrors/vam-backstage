from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

_WATCHED_SUFFIXES = (".var", ".var.disabled", ".disabled")


class LibraryWatcher:
    def __init__(self, lib, on_changed: Callable[[], None], interval: float = 4.0):
        self.lib = lib
        self.on_changed = on_changed
        self.interval = interval
        self.paused = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="library-watcher", daemon=True)
        self._thread.start()

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        th = self._thread
        self._thread = None
        if join and th is not None and th is not threading.current_thread():
            th.join(timeout=max(1.0, self.interval + 1))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def roots(self) -> list[str]:
        out = []
        main = self.lib.dirs.main_dir_path()
        if main:
            out.append(main)
        for d in self.lib.dirs.aux_library_dirs():
            if d.get("path"):
                out.append(d["path"])
        return out

    def snapshot(self) -> dict[str, tuple[int, int]]:
        snap: dict[str, tuple[int, int]] = {}
        for root in self.roots():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for d in dirnames:
                    if d.lower().endswith(".var"):
                        try:
                            st = os.stat(os.path.join(dirpath, d))
                        except OSError:
                            continue
                        snap[os.path.join(dirpath, d)] = (0, st.st_mtime_ns)
                for name in filenames:
                    if name.lower().endswith(_WATCHED_SUFFIXES):
                        full = os.path.join(dirpath, name)
                        try:
                            st = os.stat(full)
                        except OSError:
                            continue
                        snap[full] = (st.st_size, st.st_mtime_ns)
        return snap

    def _loop(self) -> None:
        try:
            baseline = self.snapshot()
        except Exception:
            log.exception("watcher: initial snapshot failed")
            return
        last = baseline
        while not self._stop.wait(self.interval):
            if self.paused:
                continue
            try:
                snap = self.snapshot()
            except Exception:
                log.exception("watcher: snapshot failed")
                continue
            if snap != baseline and snap == last:
                baseline = last = snap
                try:
                    self.on_changed()
                except Exception:
                    log.exception("watcher: rescan failed")
                try:
                    baseline = last = self.snapshot()
                except Exception:
                    pass
            else:
                last = snap
