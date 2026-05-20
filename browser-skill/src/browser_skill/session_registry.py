"""File-locked session ledger: short id → session record (P1 isolation key)."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional


def _home() -> Path:
    return Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browser-skill")))


def _dir() -> Path:
    d = _home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger_path() -> Path:
    return _dir() / "ledger.json"


@contextmanager
def _locked() -> Iterator[dict]:
    """Exclusive flock around a read-modify-write of the ledger."""
    lock = _dir() / ".lock"
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            p = _ledger_path()
            data = json.loads(p.read_text()) if p.exists() else {"next_id": 1, "sessions": {}}
            yield data
            p.write_text(json.dumps(data))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def allocate(*, backend: str, daemon_endpoint: str, owner: str,
             workspace: Optional[object] = None, name: Optional[str] = None) -> str:
    now = time.time()
    with _locked() as data:
        sid = str(data["next_id"])
        data["next_id"] += 1
        data["sessions"][sid] = {
            "id": sid, "backend": backend, "daemon_endpoint": daemon_endpoint,
            "workspace": workspace, "owner": owner, "name": name,
            "created_at": now, "last_seen": now,
        }
        return sid


def get(session_id: str) -> Optional[dict]:
    p = _ledger_path()
    if not p.exists():
        return None
    return json.loads(p.read_text())["sessions"].get(session_id)


def _with_entry(session_id: str, fn: Callable[[dict], object]) -> Optional[dict]:
    """Apply ``fn`` to a session entry in-place under the lock; return the entry."""
    with _locked() as data:
        entry = data["sessions"].get(session_id)
        if entry is None:
            return None
        fn(entry)
        return entry


def touch(session_id: str) -> Optional[dict]:
    """Bump ``last_seen`` to now."""
    now = time.time()
    return _with_entry(session_id, lambda e: e.update(last_seen=now))


def update(session_id: str, **fields) -> Optional[dict]:
    """Patch arbitrary fields on a session record."""
    return _with_entry(session_id, lambda e: e.update(**fields))


def remove(session_id: str) -> Optional[dict]:
    """Drop a session from the ledger; return the removed record (or None)."""
    with _locked() as data:
        return data["sessions"].pop(session_id, None)


def list_all() -> list[dict]:
    """All session records, ordered by id."""
    p = _ledger_path()
    if not p.exists():
        return []
    sessions = json.loads(p.read_text())["sessions"]
    return [sessions[k] for k in sorted(sessions, key=int)]


def prune(*, idle_seconds: float) -> list[dict]:
    """Remove sessions idle longer than ``idle_seconds``; return removed records."""
    now = time.time()
    with _locked() as data:
        stale = [sid for sid, e in data["sessions"].items()
                 if now - e.get("last_seen", 0.0) >= idle_seconds]
        return [data["sessions"].pop(sid) for sid in stale]
