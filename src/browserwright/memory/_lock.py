"""Shared cross-process file lock for memory files and the session ledger."""
from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path
from typing import Optional


class FileLock:
    """Cross-process advisory ``flock`` on ``path``, plus an in-process
    thread lock so concurrent threads in one process serialise too.

    The lock file is created (0600) if missing; parent directories are
    created as needed. ``flock`` failures (e.g. filesystems without lock
    support) degrade to the thread lock alone.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: Optional[int] = None
        self._thread_lock = threading.Lock()

    def __enter__(self):
        self._thread_lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            # No flock support on this filesystem — rely on the thread lock.
            pass
        return self

    def __exit__(self, *exc):
        try:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(self._fd)
        finally:
            self._fd = None
            self._thread_lock.release()
