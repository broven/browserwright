"""L2 -- transparent session-reconnect recovery through the extension backend.

These exercise the north-star behavior: a code agent keeps calling with the
same browserwright session id, and the skill transparently re-attaches to the
session's tab WITHOUT any explicit attach — across separate `bs run` processes
(in-process `current_target_id` is gone) and even when the ledger.runtime fast
path is stale (forcing the group-title `recoverSession` fallback).

The group-fallback test transitively proves name->tab-group: `recoverSession`
finds the tab purely by querying the Chrome tab group whose title == the
session name, which only succeeds if `open_background` actually titled the
group with the session name.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .conftest import TEST_NAME
from .helpers import SkillResult, run_skill


def _bs_home() -> Path:
    # Mirror the BS_HOME that helpers.run_skill pins for the extension backend.
    return Path(__file__).resolve().parent / "_bs_home" / TEST_NAME


def _seed_session(sid: str, name: str) -> Path:
    """Pre-seed a STABLE ledger session (helpers only auto-creates ephemeral
    ones it then deletes; we want one that survives across two run_skill calls
    so the runtime cache persists)."""
    sessions = _bs_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    ledger = sessions / "ledger.json"
    now = time.time()
    record = {
        "id": sid, "backend": "extension", "daemon_endpoint": TEST_NAME,
        "workspace": None, "owner": "attach", "name": name,
        "created_at": now, "last_seen": now,
    }
    ledger.write_text(json.dumps({"next_id": 1, "sessions": {sid: record}}),
                      encoding="utf-8")
    return ledger


def _payload(result: SkillResult) -> dict:
    line = next(
        (ln for ln in reversed(result.stdout.strip().splitlines())
         if ln.startswith("{")),
        None,
    )
    assert line is not None, (
        f"no JSON payload; stdout={result.stdout!r} stderr={result.stderr!r}")
    return json.loads(line)


_OPEN_SCRIPT = (
    "import json\n"
    "from urllib.parse import quote\n"
    "html='<!doctype html><title>{title}</title><main>hi</main>'\n"
    "tab=open_background('data:text/html;charset=utf-8,'+quote(html))\n"
    "wait_for_load()\n"
    "print(json.dumps({{'targetId':tab['targetId'],'groupId':tab['groupId']}}))\n"
)

# A fresh process that does NOT open or attach anything — it just operates the
# session's "current" tab. Works only if recovery transparently re-binds it.
_OPERATE_SCRIPT = (
    "import json\n"
    "print(json.dumps({'title': js('document.title')}))\n"
)


def test_recovery_fast_path_across_processes(ext_ready):
    """Process A opens a tab in session 'cf-bots'; process B (new process,
    empty in-process current_target_id) operates it with no attach — must
    transparently recover from the persisted ledger.runtime fast path."""
    sid = "rec-fast"
    ledger = _seed_session(sid, "cf-bots")
    try:
        a = run_skill(script=_OPEN_SCRIPT.format(title="RecoverFast"),
                      backend="extension", extra_env={"BD_SESSION": sid})
        assert a.returncode == 0, (a.stdout, a.stderr)
        _payload(a)  # tab opened

        b = run_skill(script=_OPERATE_SCRIPT, backend="extension",
                      extra_env={"BD_SESSION": sid})
        assert b.returncode == 0, (
            f"transparent recovery failed; stdout={b.stdout!r} stderr={b.stderr!r}")
        # The extension prepends a "👀 " attach-marker to the live DOM title;
        # containment (not ==) correctly tolerates it.
        assert "RecoverFast" in _payload(b)["title"]
    finally:
        ledger.unlink(missing_ok=True)


def test_recovery_via_group_when_runtime_stale(ext_ready):
    """When the ledger.runtime fast path points at a dead target (simulating a
    browser restart that changed tab ids), process B must fall back to
    BrowserwrightDaemon.recoverSession, which finds the tab by the group whose title
    == the session name. Transitively proves name->tab-group titling."""
    sid = "rec-group"
    ledger = _seed_session(sid, "cf-bots2")
    try:
        a = run_skill(script=_OPEN_SCRIPT.format(title="RecoverGroup"),
                      backend="extension", extra_env={"BD_SESSION": sid})
        assert a.returncode == 0, (a.stdout, a.stderr)
        _payload(a)

        # Clobber the fast-path cache with a bogus/stale target so attach fails
        # and recovery must go through the group-title query.
        data = json.loads(ledger.read_text())
        data["sessions"][sid]["runtime"] = {
            "current_target_id": "ext-tab-999999",
            "group_id": -1, "owned_tab_ids": [], "updated_at": 0,
        }
        ledger.write_text(json.dumps(data), encoding="utf-8")

        b = run_skill(script=_OPERATE_SCRIPT, backend="extension",
                      extra_env={"BD_SESSION": sid})
        assert b.returncode == 0, (
            f"group-fallback recovery failed; stdout={b.stdout!r} stderr={b.stderr!r}")
        assert "RecoverGroup" in _payload(b)["title"]
    finally:
        ledger.unlink(missing_ok=True)
