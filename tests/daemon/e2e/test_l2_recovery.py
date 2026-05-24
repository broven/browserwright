"""L2 -- transparent session-reconnect recovery through the extension backend.

These exercise the north-star behavior: a code agent keeps calling with the
same browserwright session id, and the skill transparently re-attaches to the
session's tab WITHOUT any explicit attach — across separate `bs run` processes
(in-process `current_target_id` is gone) and even when the ledger.runtime fast
    path is stale (forcing the group-id `recoverSession` fallback).

The group-fallback test proves `open_background` persisted the numeric Chrome
tab group id and `recoverSession` uses that id instead of the mutable group
title.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .helpers import SkillResult, run_skill


def _bs_home() -> Path:
    # Mirror the BS_HOME that helpers.run_skill pins for the extension backend.
    return Path(__file__).resolve().parent / "_bs_home" / "extension"


def _seed_session(sid: str, name: str) -> Path:
    """Pre-seed a STABLE ledger session (helpers only auto-creates ephemeral
    ones it then deletes; we want one that survives across two run_skill calls
    so the runtime cache persists)."""
    sessions = _bs_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    ledger = sessions / "ledger.json"
    now = time.time()
    record = {
        "id": sid, "backend": "extension",
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


# `open_background` is a daemon/extension feature with no agent-surface
# Playwright equivalent (removed from EXPORTS in Phase C PR3); the recovery
# tests still seed a background tab via the internal primitive.
_OPEN_SCRIPT = (
    "import json\n"
    "from urllib.parse import quote\n"
    "from browserwright.primitives.page import open_background, wait_for_load\n"
    "html='<!doctype html><title>{title}</title><main>hi</main>'\n"
    "tab=open_background('data:text/html;charset=utf-8,'+quote(html))\n"
    "wait_for_load()\n"
    "print(json.dumps({{'targetId':tab['targetId'],'groupId':tab['groupId']}}))\n"
)

# A fresh process that does NOT open or attach anything — it just operates the
# session's "current" tab. This is a DAEMON-capability test: it verifies the
# daemon transparently re-binds the session's recovered target (ledger fast path
# / group-id fallback). It deliberately observes through the internal `js`
# primitive (the agent CDP path that drives the recovered target directly) — NOT
# the Phase C Playwright `page`. The Playwright handle correlates a `Page` to
# the agent target by URL, which is ambiguous for the `data:`-content recovery
# fixture here; the daemon recovery contract under test is the agent-path bind,
# so we assert it on that path (same pattern as the multisession / parity /
# userscript daemon-capability e2e tests). `js` was removed from the EXPORTS
# agent surface in Phase C PR3 but survives as an internal primitive.
_OPERATE_SCRIPT = (
    "import json\n"
    "from browserwright.primitives.interact import js\n"
    "print(json.dumps({'title': js('document.title')}))\n"
)


def test_recovery_fast_path_across_processes(ext_ready, e2e_daemon):
    """Process A opens a tab in session 'cf-bots'; process B (new process,
    empty in-process current_target_id) operates it with no attach — must
    transparently recover from the persisted ledger.runtime fast path."""
    rd = e2e_daemon.runtime_dir
    sid = "rec-fast"
    ledger = _seed_session(sid, "cf-bots")
    try:
        a = run_skill(script=_OPEN_SCRIPT.format(title="RecoverFast"),
                      backend="extension", runtime_dir=rd,
                      extra_env={"BD_SESSION": sid})
        assert a.returncode == 0, (a.stdout, a.stderr)
        _payload(a)  # tab opened

        b = run_skill(script=_OPERATE_SCRIPT, backend="extension",
                      runtime_dir=rd, extra_env={"BD_SESSION": sid})
        assert b.returncode == 0, (
            f"transparent recovery failed; stdout={b.stdout!r} stderr={b.stderr!r}")
        # The extension prepends a "👀 " attach-marker to the live DOM title;
        # containment (not ==) correctly tolerates it.
        assert "RecoverFast" in _payload(b)["title"]
    finally:
        ledger.unlink(missing_ok=True)


def test_recovery_via_group_id_when_runtime_stale(ext_ready, e2e_daemon):
    """When the ledger.runtime fast path points at a dead target (simulating a
    stale tab binding), process B must fall back to recoverSession by the
    persisted numeric group id."""
    rd = e2e_daemon.runtime_dir
    sid = "rec-group"
    ledger = _seed_session(sid, "cf-bots2")
    try:
        a = run_skill(script=_OPEN_SCRIPT.format(title="RecoverGroup"),
                      backend="extension", runtime_dir=rd,
                      extra_env={"BD_SESSION": sid})
        assert a.returncode == 0, (a.stdout, a.stderr)
        opened = _payload(a)

        # Clobber the fast-path target so attach fails, but keep the persisted
        # group id so recovery goes through the numeric group lookup.
        data = json.loads(ledger.read_text())
        data["sessions"][sid]["runtime"] = {
            "current_target_id": "ext-tab-999999",
            "group_id": opened["groupId"], "owned_tab_ids": [], "updated_at": 0,
        }
        ledger.write_text(json.dumps(data), encoding="utf-8")

        b = run_skill(script=_OPERATE_SCRIPT, backend="extension",
                      runtime_dir=rd, extra_env={"BD_SESSION": sid})
        assert b.returncode == 0, (
            f"group-id recovery failed; stdout={b.stdout!r} stderr={b.stderr!r}")
        assert "RecoverGroup" in _payload(b)["title"]
    finally:
        ledger.unlink(missing_ok=True)
