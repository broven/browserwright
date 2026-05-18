"""Global memory — ``$BS_HOME/global.md`` (default ``~/.browser-skill/global.md``)."""
from __future__ import annotations

import datetime as _dt
import fcntl
import os
import threading
from pathlib import Path
from typing import Any, Optional

from ..errors import NeedsUserConfirm
from . import _md


_DEFAULT_BODY = """# Global skill memory

Notes here apply across every site. See frontmatter for machine-readable
preferences (e.g. ``daemon.preferred_backend``).

## Aliases (user-defined)

## Last choices
"""


def home_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browser-skill"))).resolve()


def global_path() -> Path:
    return home_dir() / "global.md"


class _FileLock:
    """Cross-process advisory lock on the global memory file."""

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
            # Non-POSIX (Windows) — skip; rely on the thread lock.
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


class GlobalMemory:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or global_path()

    # ---- low-level R/W ------------------------------------------------

    def _read(self) -> tuple[dict, str]:
        if not self.path.exists():
            return {"schema_version": 1}, _DEFAULT_BODY
        return _md.parse_doc(self.path.read_text(encoding="utf-8"))

    def read(self) -> dict:
        fm, body = self._read()
        return {"frontmatter": fm, "body": body}

    def _write(self, fm: dict, body: str) -> None:
        fm = dict(fm) if fm else {"schema_version": 1}
        fm.setdefault("schema_version", 1)
        _md.write_atomic(self.path, _md.render_doc(fm, body))

    # ---- high-level helpers ------------------------------------------

    def append(self, line: str, section: str = "Notes") -> None:
        with _FileLock(self.path):
            fm, body = self._read()
            new_body = _md.append_to_section(body, section, f"- {line}".rstrip())
            self._write(fm, new_body)

    def find(self, pattern: str) -> list[tuple[int, str]]:
        _fm, body = self._read()
        return _md.find_matching_lines(body, pattern)

    def forget(self, pattern: str, *, confirm: bool = True) -> list[str]:
        """Remove every bullet whose text contains ``pattern`` from the
        global memory body. ``confirm=True`` is a dry-run; ``confirm=False``
        actually deletes. See ``SiteMemory.forget`` for rationale."""
        matches = self.find(pattern)
        if not matches:
            return []
        if confirm:
            return [ln for _i, ln in matches]
        with _FileLock(self.path):
            fm, body = self._read()
            new_body = _md.remove_lines(body, {i for i, _ln in matches})
            self._write(fm, new_body)
        return [ln for _i, ln in matches]

    def set_preference(self, key: str, value: Any, *, confirm: bool = True) -> dict:
        """Write a structured preference (e.g. ``daemon.preferred_backend``).

        spec §C.3 type D: **strong confirm required**. When ``confirm=True`` we
        raise ``NeedsUserConfirm`` carrying the proposal so the agent can
        surface a dialog. Pass ``confirm=False`` after user assent to actually
        write.
        """
        if confirm:
            raise NeedsUserConfirm(
                what=f"set {key} = {value!r}",
                proposal={"key": key, "value": value},
            )
        with _FileLock(self.path):
            fm, body = self._read()
            old = _set_dotted(fm, key, value)
            # Stamp set_by_user_at on the relevant block (top-level container).
            container = key.split(".")[0]
            now_iso = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
            if container in fm and isinstance(fm[container], dict):
                fm[container]["set_by_user_at"] = now_iso
                # spec §C.3: keep history rather than silent overwrite.
                if old is not None and old != value:
                    note = (
                        fm[container].get("notes")
                        or ""
                    )
                    suffix = f"prev {key}={old!r} until {now_iso}"
                    fm[container]["notes"] = (note + "; " + suffix) if note else suffix
            self._write(fm, body)
        return {"key": key, "value": value, "previous": old}


# ---- dotted-path helpers --------------------------------------------


def _set_dotted(obj: dict, dotted: str, value: Any) -> Any:
    """Set ``obj[a][b] = value`` for dotted key ``a.b``; return previous value."""
    parts = dotted.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    leaf = parts[-1]
    prev = cur.get(leaf)
    cur[leaf] = value
    return prev


def _get_dotted(obj: dict, dotted: str, default=None):
    cur: Any = obj
    for p in dotted.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


# ---- module-level convenience ---------------------------------------


_singleton: Optional[GlobalMemory] = None
_singleton_lock = threading.Lock()


def global_memory() -> GlobalMemory:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GlobalMemory()
    return _singleton


def read_daemon_preferred_backend() -> Optional[str]:
    """Backend resolution helper used at Skill startup (spec §C.3)."""
    mem = global_memory().read()
    return _get_dotted(mem["frontmatter"], "daemon.preferred_backend")


def write_daemon_preferred_backend(backend: str, *, confirm: bool = True) -> dict:
    return global_memory().set_preference(
        "daemon.preferred_backend", backend, confirm=confirm
    )
