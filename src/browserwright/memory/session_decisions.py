"""Session-decision memory (P7): situation → how to start a session.

A file-locked JSON map at ``$BS_HOME/session_decisions.json``. The agent flow
is **hit → auto-start; miss → ask the user, then record** (see
``session_create.choose``). Decisions capture the backend + mode (and, for
fingerprint attach, the target port/recipe) so a recurring situation doesn't
re-prompt.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def _home() -> Path:
    return Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browserwright")))


def _path() -> Path:
    _home().mkdir(parents=True, exist_ok=True)
    return _home() / "session_decisions.json"


@contextmanager
def _locked() -> Iterator[dict]:
    p = _path()
    lock = _home() / ".session_decisions.lock"
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = json.loads(p.read_text()) if p.exists() else {}
            yield data
            p.write_text(json.dumps(data))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def lookup(situation: str) -> Optional[dict]:
    """Return the recorded decision for ``situation``, or None."""
    p = _path()
    if not p.exists():
        return None
    return json.loads(p.read_text()).get(situation)


def record(situation: str, decision: dict) -> None:
    """Persist (overwriting) the decision for ``situation``."""
    with _locked() as data:
        data[situation] = decision
