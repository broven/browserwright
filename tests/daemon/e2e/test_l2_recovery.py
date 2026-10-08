"""L2 -- transparent session-reconnect recovery through the extension backend.

These exercise the north-star behavior: a code agent keeps calling with the
same browserwright session id, and the skill transparently re-attaches to the
session's tab WITHOUT any explicit attach — across separate `bs run` processes
(in-process `current_target_id` is gone) and even when the ledger.runtime fast
path is stale (forcing the title-keyed `recoverSession` fallback).

The fallback test proves the daemon finds the session's Chrome tab group by
its TITLE (`<name>-BW<sid>`, ADR-0009) when the fast-path target is dead —
not by a numeric group id, which Chrome recycles on restart.
"""
from __future__ import annotations

import json

from .helpers import (
    SkillResult,
    bs_home,
    drop_ledger_session,
    locked_ledger,
    run_skill,
    seed_ledger_session,
)

# The BS_HOME helpers.run_skill pins for the extension backend.
_HOME = bs_home("extension")


def _seed_session(sid: str, name: str) -> None:
    """Pre-seed a STABLE ledger session (helpers only auto-creates ephemeral
    ones it then deletes; we want one that survives across two run_skill calls
    so the runtime cache persists)."""
    seed_ledger_session(_HOME, sid, backend="extension", name=name)


def _payload(result: SkillResult) -> dict:
    line = next(
        (ln for ln in reversed(result.stdout.strip().splitlines())
         if ln.startswith("{")),
        None,
    )
    assert line is not None, (
        f"no JSON payload; stdout={result.stdout!r} stderr={result.stderr!r}")
    return json.loads(line)


# Background-tab open is a daemon feature with no agent-surface Playwright
# equivalent; the recovery tests seed a background tab via the internal
# session_runtime helper.
_OPEN_SCRIPT = (
    "import json\n"
    "from urllib.parse import quote\n"
    "from browserwright.session import current_session\n"
    "from browserwright.session_runtime import open_session_tab, wait_for_ready\n"
    "sess = current_session()\n"
    "html='<!doctype html><title>{title}</title><main>hi</main>'\n"
    "tab=open_session_tab(sess, 'data:text/html;charset=utf-8,'+quote(html))\n"
    "wait_for_ready(sess)\n"
    "print(json.dumps({{'targetId':tab['targetId'],'groupId':tab['groupId']}}))\n"
)

# A fresh process that does NOT open or attach anything — it just operates the
# session's "current" tab. This is a DAEMON-capability test: it verifies the
# daemon transparently re-binds the session's recovered target (ledger fast path
# / group-id fallback). It deliberately observes through the internal
# `eval_js` helper (the agent CDP path that drives the recovered target
# directly, via `ensure_session_target`) — NOT the Playwright `page`. The
# Playwright handle correlates a `Page` to the agent target by URL, which is
# ambiguous for the `data:`-content recovery fixture here; the daemon recovery
# contract under test is the agent-path bind, so we assert it on that path
# (same pattern as the multisession / parity / userscript daemon-capability
# e2e tests).
_OPERATE_SCRIPT = (
    "import json\n"
    "from browserwright.session import current_session\n"
    "from browserwright.session_runtime import eval_js\n"
    "print(json.dumps({'title': eval_js(current_session(), 'document.title')}))\n"
)


def test_recovery_fast_path_across_processes(ext_ready, e2e_daemon):
    """Process A opens a tab in session 'cf-bots'; process B (new process,
    empty in-process current_target_id) operates it with no attach — must
    transparently recover from the persisted ledger.runtime fast path."""
    rd = e2e_daemon.runtime_dir
    sid = "rec-fast"
    _seed_session(sid, "cf-bots")
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
        # Containment (not ==) keeps this independent of title formatting.
        assert "RecoverFast" in _payload(b)["title"]
    finally:
        drop_ledger_session(_HOME, sid)


def test_recovery_via_group_title_when_runtime_stale(ext_ready, e2e_daemon):
    """When the ledger.runtime fast path points at a dead target (simulating a
    stale tab binding), process B must fall back to recoverSession, which
    finds the session's group BY TITLE (`<name>-BW<sid>`, ADR-0009)."""
    rd = e2e_daemon.runtime_dir
    sid = "rec-group"
    _seed_session(sid, "cf-bots2")
    try:
        a = run_skill(script=_OPEN_SCRIPT.format(title="RecoverGroup"),
                      backend="extension", runtime_dir=rd,
                      extra_env={"BD_SESSION": sid})
        assert a.returncode == 0, (a.stdout, a.stderr)
        _payload(a)  # tab opened

        # Clobber the fast-path target so attach fails. ADR-0009's runtime
        # cache holds only `current_target_id` / `updated_at` — no group id is
        # persisted, so nothing here simulates one: the fallback must recover
        # purely from the ledger name (`<name>-BW<sid>`) + live group title.
        with locked_ledger(_HOME) as data:
            data["sessions"][sid]["runtime"] = {
                "current_target_id": "ext-tab-999999", "updated_at": 0,
            }

        b = run_skill(script=_OPERATE_SCRIPT, backend="extension",
                      runtime_dir=rd, extra_env={"BD_SESSION": sid})
        assert b.returncode == 0, (
            f"title recovery failed; stdout={b.stdout!r} stderr={b.stderr!r}")
        assert "RecoverGroup" in _payload(b)["title"]
    finally:
        drop_ledger_session(_HOME, sid)
