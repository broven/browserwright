"""File-locked session ledger: short id → session record (P1 isolation key)."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from .memory._lock import FileLock


def _home() -> Path:
    return Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browserwright")))


def _dir() -> Path:
    d = _home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger_path() -> Path:
    return _dir() / "ledger.json"


@contextmanager
def _locked() -> Iterator[dict]:
    """Exclusive flock around a read-modify-write of the ledger."""
    with FileLock(_dir() / ".lock"):
        p = _ledger_path()
        data = json.loads(p.read_text()) if p.exists() else {"next_id": 1, "sessions": {}}
        yield data
        p.write_text(json.dumps(data))


def allocate(*, backend: str, owner: str,
             workspace: Optional[object] = None, name: Optional[str] = None,
             unique_name: bool = False,
             env_daemon_scope: Optional[str] = None) -> str:
    now = time.time()
    with _locked() as data:
        if env_daemon_scope is not None:
            if backend != "env":
                raise ValueError("env_daemon_scope is valid only for env sessions")
            for e in data["sessions"].values():
                if e.get("backend") != backend:
                    continue
                existing_scope = e.get("daemon_scope")
                # Records written before daemon_scope existed cannot safely be
                # assigned to a different daemon. Treat them as conflicts until
                # explicitly ended rather than opening an unknown shared scope.
                if existing_scope not in (None, env_daemon_scope):
                    continue
                conflict = e.get("id")
                raise ValueError(
                    f"daemon {env_daemon_scope!r} already has env session "
                    f"{conflict!r}. An env daemon has one shared upstream, so "
                    "reuse or end that session first. To drive N profiles, run "
                    "N isolated daemons as documented in "
                    "docs/session-workspaces.md § 'Scaling env to N profiles'."
                )
        if unique_name:
            # Globally-unique name guard. Raising here (after the `yield` in
            # _locked) aborts before `p.write_text`, so a rejected allocation
            # leaves the ledger untouched.
            for e in data["sessions"].values():
                if e.get("name") == name:
                    conflict = e.get("id")
                    raise ValueError(
                        f"session name {name!r} is already taken by session "
                        f"{conflict!r}. Names must be globally unique. Either "
                        f"pick a different --name, reuse the existing session "
                        f"with `browserwright -s {conflict} -e ...`, or "
                        f"end it first: browserwright session end "
                        f"--session={conflict}"
                    )
        sid = str(data["next_id"])
        data["next_id"] += 1
        record = {
            "id": sid, "backend": backend,
            "workspace": workspace, "owner": owner, "name": name,
            "created_at": now, "last_seen": now,
        }
        if env_daemon_scope is not None:
            record["daemon_scope"] = env_daemon_scope
        data["sessions"][sid] = record
        return sid


def get(session_id: str) -> Optional[dict]:
    p = _ledger_path()
    if not p.exists():
        return None
    return json.loads(p.read_text())["sessions"].get(session_id)


def claim_legacy_env_scope(session_id: str, daemon_scope: str) -> bool:
    """Atomically bind one unscoped legacy env record to a daemon.

    Records created before ``daemon_scope`` existed have no stronger ownership
    evidence.  The first matching env daemon to route the record may migrate
    it; an already-scoped, missing, or non-env record remains fail-closed.
    """
    if not isinstance(daemon_scope, str) or not daemon_scope:
        return False
    with _locked() as data:
        entry = data["sessions"].get(session_id)
        if not isinstance(entry, dict) or entry.get("backend") != "env":
            return False
        existing_scope = entry.get("daemon_scope")
        if existing_scope is None:
            entry["daemon_scope"] = daemon_scope
            return True
        return existing_scope == daemon_scope


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
    """Patch fields on a session record.

    ``backend`` is fixed at creation and immutable for the session's whole life
    (single-daemon refactor, decision 2): a change to a DIFFERENT backend is
    rejected. Raising before ``e.update`` (and before ``_locked``'s post-yield
    ``write_text``) leaves the ledger untouched. A no-op same-value patch is
    allowed so callers that re-write the whole record don't trip the guard."""
    def _patch(e: dict) -> None:
        if "backend" in fields and fields["backend"] != e.get("backend"):
            raise ValueError(
                f"session {session_id!r} backend is immutable: refusing to "
                f"change {e.get('backend')!r} → {fields['backend']!r}")
        e.update(**fields)
    return _with_entry(session_id, _patch)


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


def stale(*, idle_seconds: float) -> list[dict]:
    """Sessions idle longer than ``idle_seconds`` without removing them."""
    now = time.time()
    p = _ledger_path()
    if not p.exists():
        return []
    sessions = json.loads(p.read_text())["sessions"]
    records = [
        e for e in sessions.values()
        if now - e.get("last_seen", 0.0) >= idle_seconds
    ]
    return sorted(records, key=lambda e: int(e.get("id", 0)))


def prune(*, idle_seconds: float) -> list[dict]:
    """Remove sessions idle longer than ``idle_seconds``; return removed records."""
    now = time.time()
    with _locked() as data:
        stale = [sid for sid, e in data["sessions"].items()
                 if now - e.get("last_seen", 0.0) >= idle_seconds]
        return [data["sessions"].pop(sid) for sid in stale]
