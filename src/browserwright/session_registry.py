"""File-locked session ledger: short id → session record (P1 isolation key)."""
from __future__ import annotations

import contextlib
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
    # A session record can hold a CDP endpoint with an embedded bearer token,
    # so the directory is owner-only. Re-applying on every call is cheap and
    # repairs a directory created by an older version under a looser umask.
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    return d


def _ledger_path() -> Path:
    return _dir() / "ledger.json"


@contextmanager
def _locked() -> Iterator[dict]:
    """Exclusive flock around a read-modify-write of the ledger.

    The write is temp-file-and-rename. Readers (`get`, `list_all`, `stale`)
    deliberately bypass the lock for speed, so a plain in-place write left a
    window where a crash — or just a slow write — exposed a truncated file to
    them. `os.replace` is atomic, so a reader sees either the old ledger or the
    new one, never half of one.

    **Load-bearing: the write happens after the `yield`.** An exception raised
    by the caller's body propagates before anything touches the file, which is
    how every validation guard in this module leaves the ledger untouched on
    rejection. Do not move the write into a `finally`.
    """
    with FileLock(_dir() / ".lock"):
        p = _ledger_path()
        data = json.loads(p.read_text()) if p.exists() else {"next_id": 1, "sessions": {}}
        yield data
        tmp = p.with_name(p.name + ".tmp")
        # The mode argument to `os.open` only applies when the file is created,
        # so a stale tmp left by a crashed write could keep looser permissions;
        # the explicit chmod repairs that case.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, p)


def allocate(*, backend: str, owner: str,
             workspace: Optional[object] = None, name: Optional[str] = None,
             unique_name: bool = False) -> str:
    now = time.time()
    with _locked() as data:
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
        data["sessions"][sid] = record
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
