"""REPL temporary context — in-process dict, no disk."""
from __future__ import annotations

import threading


class ReplMemory:
    def __init__(self):
        self._d: dict[str, object] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value):
        with self._lock:
            self._d[key] = value

    def get(self, key: str, default=None):
        return self._d.get(key, default)

    def all(self) -> dict:
        with self._lock:
            return dict(self._d)


_singleton = ReplMemory()


def repl_memory() -> ReplMemory:
    return _singleton
