"""REPRO -- extension SW loss orphans a resident executor's page.

Real-world incident (email-server workspace, sessions 636-641): a code agent
driving a long-lived session hit `TargetClosedError` on every second operation
after the extension service worker was reloaded / updated / killed. Doctor
reported `extension connected` and the ledger session was intact, so the only
"recovery" the agent found was `session new` -- which snowballed into 6
same-name sessions.

Root-cause chain (verified against the real harness):

1. The extension SW is the CDP transport for a session's tabs
   (chrome.debugger.attach). When the SW dies (reload/update/memory kill) every
   debugger attachment dies and the in-memory `attachedTabs` set is lost.
2. Worse: `chrome.runtime.reload()` (the daemon's supported `extension reload`
   verb) is a SW DEATH SWITCH -- the MV3 alarm recovery net is cleared by the
   reload and neither onInstalled nor onStartup fire, so the SW does not come
   back on its own (verified: 90s, no reconnect). That is the `extensions: 0
   -> reload -> still 0` state of session 637.
3. When the SW DOES come back (Chrome-side revive, user touch, browser
   restart), it reconnects to the daemon relay (warm, same installId) but has
   NOTHING to re-announce: the relay's ghost table for the session's tabs is
   gone and never rebuilt. The daemon's upstream state never changes (the
   relay is transport-only), so the facade ws to the resident executor stays
   open and the executor's Playwright page objects are zombies:
   `TargetClosedError` / `Execution context was destroyed` on every use --
   with no hint in the error.
4. The resident executor (a daemon child, kept alive by design) holds the
   single-attacher slot for the session's tab through its facade client. While
   it lives, NO other client can re-attach the tab: the daemon refuses with
   "target ... already attached by another client", so even a fresh-process
   cold-start / title-keyed recoverSession is deadlocked. The only escapes
   are `session reset <sid>` (reaps the executor, releasing the slot) or
   `session new` (fresh tab, no conflict). The agent only knew the latter.

Tests:

- test_extension_reload_is_a_sw_death_switch (session 637): the daemon's own
  `extension reload` verb kills the connection and the SW does not return.
- test_extension_sw_loss_orphans_resident_executor (session 638): the
  extension is back (doctor-green) but the session's tab is orphaned -- the
  resident executor's page fails with a raw, hint-free error, while a
  brand-new tab (the `session new` path) works in the same process.
- test_resident_executor_blocks_recovery_until_reset: while the orphaned
  resident executor lives, a fresh process cannot recover the session
  ("already attached by another client"); after `session reset` (executor
  reaped) the same fresh process recovers transparently.
"""
from __future__ import annotations

import json
import time
import urllib.request

from ._real_browser import extension_id_from_path
from .helpers import run_skill
from .test_l2_recovery import _payload, _seed_session

# In-SW simulation of the post-reload "fresh SW" state WITHOUT killing the SW:
# drop every debugger attachment, empty the in-memory tab sets, and force the
# ws to reconnect -- the fresh SW then re-announces nothing, exactly like a
# rebooted SW after chrome.runtime.reload().
_SIMULATE_FRESH_SW_JS = (
    "(async () => {"
    "  const tabs = [...attachedTabs];"
    "  attachedTabs.clear();"
    "  markedTabs.clear();"
    "  await Promise.allSettled(tabs.map(t => chrome.debugger.detach({tabId: t})));"
    "  forceReconnect('simulating fresh SW after reload');"
    "  return {detached: tabs};"
    "})()"
)


def _sw_eval_script(ws_url: str, ext_id: str, expression: str) -> str:
    """Heredoc fragment: CDP-evaluate `expression` inside the extension SW.

    Values are INTERPOLATED because the executor runs under the daemon's env,
    so extra_env never reaches it. Returns the Python source for the
    `Runtime.evaluate` call (result assigned to `out['sim']`).
    """
    import json as _json
    fragment = (
        "from browserwright.cdp import CDPSession\n"
        "cdp = CDPSession(" + _json.dumps(ws_url) + ")\n"
        "cdp.send('Target.setDiscoverTargets', discover=True)\n"
        "sw = [t['targetId'] for t in cdp.send('Target.getTargets')['targetInfos']\n"
        "      if t['type'] == 'service_worker'\n"
        "      and t['url'].startswith('chrome-extension://" + ext_id + "/')][0]\n"
        "sid = cdp.attach(sw)\n"
        "out['sim'] = cdp.send('Runtime.evaluate', session=sid,\n"
        "    expression=" + _json.dumps(expression) + ", returnByValue=True,\n"
        "    awaitPromise=True).get('result', {}).get('value')\n"
    )
    # Indent so the fragment can sit directly inside a `try:` block.
    return "\n".join("    " + ln if ln else ln for ln in fragment.splitlines()) + "\n"


# One heredoc = one process = one resident executor. The script:
#   1. operates the session's page (works),
#   2. simulates the post-reload fresh-SW state via CDP into the SW,
#   3. waits until the relay again sees an extension (doctor-green state),
#   4. operates the SAME page object again (zombie),
#   5. opens a brand-NEW tab via context.new_page() (fresh target, works) --
#      the "session new always recovers" asymmetry the agent observed.
def _orphan_script(ws_url: str, ext_id: str) -> str:
    return (
        "import json, os, time, urllib.request\n"
        "def exts():\n"
        "    port = os.environ.get('BD_EXTENSION_PORT', '')\n"
        "    try:\n"
        "        with urllib.request.urlopen(\n"
        "            f'http://127.0.0.1:{port}/__status__', timeout=0.5) as r:\n"
        "            return int(json.loads(r.read().decode()).get('extensions', 0))\n"
        "    except Exception:\n"
        "        return -1\n"
        "out = {}\n"
        "try:\n"
        "    page.goto('about:blank', wait_until='load')\n"
        "    out['phase1_ok'] = True\n"
        "except Exception as e:\n"
        "    out['phase1_error'] = type(e).__name__ + ': ' + str(e)\n"
        "try:\n" + _sw_eval_script(ws_url, ext_id, _SIMULATE_FRESH_SW_JS) +
        "except Exception as e:\n"
        "    out['sim_error'] = type(e).__name__ + ': ' + str(e)\n"
        "deadline = time.monotonic() + 30\n"
        "while time.monotonic() < deadline and exts() < 1:\n"
        "    time.sleep(0.5)\n"
        "out['exts_after'] = exts()\n"
        "try:\n"
        "    out['phase2_eval'] = page.evaluate('document.title')\n"
        "except Exception as e:\n"
        "    out['phase2_error'] = type(e).__name__ + ': ' + str(e)\n"
        "try:\n"
        "    p2 = context.new_page()\n"
        "    p2.goto('about:blank', wait_until='load')\n"
        "    out['phase3_ok'] = True\n"
        "except Exception as e:\n"
        "    out['phase3_error'] = type(e).__name__ + ': ' + str(e)\n"
        "print(json.dumps(out))\n"
    )


_RELOAD_SCRIPT = (
    "import os, subprocess, sys\n"
    "r = subprocess.run(\n"
    "    [sys.executable, '-m', 'browserwright.daemon.cli', 'extension', 'reload'],\n"
    "    capture_output=True, text=True, env=os.environ, timeout=40)\n"
    "print(r.returncode, '|', (r.stderr or '').strip())\n"
)

# Fresh-process probe: try the agent-path attach (ensure_session_target step 2
# replay) and report the daemon's exact refusal.
_ATTACH_PROBE_SCRIPT = (
    "import json\n"
    "from browserwright.session import current_session\n"
    "from browserwright.session_runtime import _resolve_record\n"
    "sess = current_session()\n"
    "rec = _resolve_record(sess) or {}\n"
    "tid = ((rec.get('runtime') or {}).get('current_target_id'))\n"
    "out = {'tid': tid}\n"
    "if tid:\n"
    "    try:\n"
    "        sess.cdp.attach(tid)\n"
    "        out['attach_ok'] = True\n"
    "    except Exception as e:\n"
    "        out['attach_err'] = type(e).__name__ + ': ' + str(e)\n"
    "print(json.dumps(out))\n"
)

_RESET_SCRIPT = (
    "import os, subprocess, sys\n"
    "r = subprocess.run(\n"
    "    [sys.executable, '-m', 'browserwright', 'session', 'reset',\n"
    "     os.environ['BD_SESSION']],\n"
    "    capture_output=True, text=True, env=os.environ, timeout=30)\n"
    "print(r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip())\n"
)

_FRESH_OPERATE_SCRIPT = (
    "import json\n"
    "page.goto('about:blank', wait_until='load')\n"
    "print(json.dumps({'title': page.title()}))\n"
)


def _wait_extension(ext_port: int, deadline_s: float = 30.0) -> int:
    deadline = time.monotonic() + deadline_s
    last = -1
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{ext_port}/__status__", timeout=0.5,
            ) as resp:
                last = int(json.loads(resp.read().decode()).get("extensions", 0))
            if last >= 1:
                return last
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    return last


def test_extension_reload_is_a_sw_death_switch(ext_ready, e2e_daemon):
    """Session 637 + D acceptance: the daemon's `extension reload` verb kills
    the extension connection and the SW does NOT come back on its own. With
    reload verification (D) the CLI now says so honestly: exit 1 + manual
    recovery guidance, instead of the old silent "reload requested".
    """
    sid = "reload-death"
    _seed_session(sid, "ReloadDeath")
    result = run_skill(
        _RELOAD_SCRIPT, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    # The reload script prints "rc stdout stderr"; D makes the CLI fail (rc=1)
    # when the SW does not come back within the verify window.
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.split()[0] == "1", (
        f"reload CLI should report failure when the SW does not return: "
        f"{result.stdout}")
    assert "chrome://extensions" in result.stdout, result.stdout
    # And the SW really does not come back on its own.
    assert _wait_extension(e2e_daemon.ext_port, deadline_s=30.0) == 0, (
        "extension unexpectedly reconnected after chrome.runtime.reload(); "
        "the death-switch repro no longer holds")


def test_extension_sw_loss_orphans_resident_executor(
    ext_ready, e2e_daemon, e2e_chrome, patched_ext_dir,
):
    """Session 638 + B/C acceptance: after SW loss the resident executor's
    page dies with a TargetClosed-family error. The UNCAUGHT error surfaces
    with a recovery hint (C) and the executor recycles itself (B); the NEXT
    command cold-starts and re-attaches the session automatically -- no
    `session reset`, no `session new`.
    """
    sid = "orphan-repro"
    _seed_session(sid, "OrphanRepro")
    # Heredoc A: phase1 works, then orphan the tab (fresh-SW simulation), then
    # wait for the relay to see the extension again (doctor-green state).
    script_a = (
        "import json, os, time, urllib.request\n"
        "def exts():\n"
        "    port = os.environ.get('BD_EXTENSION_PORT', '')\n"
        "    try:\n"
        "        with urllib.request.urlopen(\n"
        "            f'http://127.0.0.1:{port}/__status__', timeout=0.5) as r:\n"
        "            return int(json.loads(r.read().decode()).get('extensions', 0))\n"
        "    except Exception:\n"
        "        return -1\n"
        "out = {}\n"
        "try:\n"
        "    page.goto('about:blank', wait_until='load')\n"
        "    out['phase1_ok'] = True\n"
        "except Exception as e:\n"
        "    out['phase1_error'] = type(e).__name__ + ': ' + str(e)\n"
        "try:\n" + _sw_eval_script(
            e2e_chrome.ws_url, extension_id_from_path(patched_ext_dir),
            _SIMULATE_FRESH_SW_JS) +
        "except Exception as e:\n"
        "    out['sim_error'] = type(e).__name__ + ': ' + str(e)\n"
        "deadline = time.monotonic() + 30\n"
        "while time.monotonic() < deadline and exts() < 1:\n"
        "    time.sleep(0.5)\n"
        "out['exts_after'] = exts()\n"
        "print(json.dumps(out))\n"
    )
    result_a = run_skill(
        script_a, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=90.0,
    )
    assert result_a.returncode == 0, (result_a.stdout, result_a.stderr)
    payload = _payload(result_a)
    assert "phase1_ok" in payload and "sim_error" not in payload, payload
    assert payload.get("exts_after", -1) >= 1, payload
    # Heredoc B: the SAME resident executor (daemon keeps it warm), page op
    # raised UNCAUGHT (the agent's real pattern -- no try/except around
    # `page.click`), so the executor's self-heal path fires.
    result_b = run_skill(
        "page.evaluate('document.title')\n",
        backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert result_b.returncode == 3, (result_b.stdout, result_b.stderr)
    # C: the surfaced error carries a recovery hint -- and must NOT teach
    # `session new` (the session itself is fine). The hint is rendered as a
    # `[fix]` line after the traceback (inline.py).
    assert "[fix]" in result_b.stderr, result_b.stderr
    fix = result_b.stderr.split("[fix]", 1)[1].strip()
    assert "session reset" in fix and "attach-active" in fix, fix
    assert "session new" not in fix, fix
    # B: the executor recycled itself (terminal target_closed) -- the NEXT
    # command cold-starts and re-attaches the session automatically. The cold
    # bind can race the announce replay once (PageBindTimeout is retryable),
    # so retry like the error message tells the agent to.
    second = None
    for attempt in range(3):
        second = run_skill(
            _FRESH_OPERATE_SCRIPT, backend="extension",
            runtime_dir=e2e_daemon.runtime_dir,
            extra_env={"BD_SESSION": sid}, timeout=90.0,
        )
        if second.returncode == 0:
            break
        time.sleep(1.0)
    assert second is not None and second.returncode == 0, (
        f"session did not self-heal after executor recycle; "
        f"stdout={second.stdout!r} stderr={second.stderr!r}")
    assert "title" in _payload(second), second.stdout


def test_extension_reconnect_auto_recovers_sessions(
    ext_ready, e2e_daemon, e2e_chrome, patched_ext_dir,
):
    """A (daemon auto-recovery): after SW loss wipes the relay's ghost table,
    the daemon re-attaches the session's tab group on extension reconnect --
    with NO client action (no attach, no reset, no new session). The agent
    path sees the session's tab again.
    """
    sid = "auto-recover"
    _seed_session(sid, "AutoRecover")
    opener = "page.goto('about:blank', wait_until='load')\nprint('opened')\n"
    first = run_skill(
        opener, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert first.returncode == 0, (first.stdout, first.stderr)

    orphan_script = (
        "import json\n"
        "out = {}\n"
        "try:\n" + _sw_eval_script(
            e2e_chrome.ws_url, extension_id_from_path(patched_ext_dir),
            _SIMULATE_FRESH_SW_JS) +
        "except Exception as e:\n"
        "    out['sim_error'] = type(e).__name__ + ': ' + str(e)\n"
        "print(json.dumps(out))\n"
    )
    orphan = run_skill(
        orphan_script, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert orphan.returncode == 0 and "sim_error" not in _payload(orphan), (
        orphan.stdout, orphan.stderr)
    assert _wait_extension(e2e_daemon.ext_port) >= 1

    # The tab is orphaned right after the sim (no ghost) -- the probe is
    # agent-path only (no executor, no attach), so it cannot itself recover.
    probe = (
        "import json\n"
        "from browserwright.session import current_session\n"
        "r = current_session().cdp.send('Target.getTargets')\n"
        "print(json.dumps(r))\n"
    )
    before = run_skill(
        probe, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert before.returncode == 0, (before.stdout, before.stderr)
    before_payload = json.loads(before.stdout)
    assert before_payload.get("targetInfos") == [], (
        f"expected the tab to be orphaned right after the sim: {before.stdout}")

    # A: within the recovery window (hello + delay + sweep), the daemon
    # re-attaches the session's group BY TITLE -- the tab becomes visible
    # again with no client action. Poll (the probe itself never attaches).
    deadline = time.monotonic() + 40.0
    seen: list | None = None
    while time.monotonic() < deadline:
        poll = run_skill(
            probe, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
            extra_env={"BD_SESSION": sid}, timeout=60.0,
        )
        assert poll.returncode == 0, (poll.stdout, poll.stderr)
        seen = json.loads(poll.stdout).get("targetInfos") or []
        if seen:
            break
        time.sleep(2.0)
    assert seen, (
        "daemon auto-recovery did not re-attach the session's tab within 40s")
    assert any(t.get("url") == "about:blank" for t in seen), seen


def test_resident_executor_blocks_recovery_until_reset(
    ext_ready, e2e_daemon, e2e_chrome, patched_ext_dir,
):
    """While the orphaned resident executor lives, a fresh process CANNOT
    recover the session ("already attached by another client" -- the
    single-attacher slot is held by the executor's facade client). After
    `session reset` (executor reaped, slot released) the same fresh process
    recovers transparently. This is the deadlock that left the agent with
    `session new` as its only recovery.
    """
    sid = "orphan-deadlock"
    _seed_session(sid, "OrphanDeadlock")
    opener = "page.goto('about:blank', wait_until='load')\nprint('opened')\n"
    first = run_skill(
        opener, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert first.returncode == 0, (first.stdout, first.stderr)

    # Orphan the tab (fresh-SW simulation) in its own skill process.
    orphan_script = (
        "import json\n"
        "out = {}\n"
        "try:\n" + _sw_eval_script(
            e2e_chrome.ws_url, extension_id_from_path(patched_ext_dir),
            _SIMULATE_FRESH_SW_JS) +
        "except Exception as e:\n"
        "    out['sim_error'] = type(e).__name__ + ': ' + str(e)\n"
        "print(json.dumps(out))\n"
    )
    orphan = run_skill(
        orphan_script, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert orphan.returncode == 0 and "sim_error" not in _payload(orphan), (
        orphan.stdout, orphan.stderr)
    assert _wait_extension(e2e_daemon.ext_port) >= 1

    # Fresh process, same session: the agent-path attach must be REFUSED
    # while the resident executor (still alive, facade client still holding
    # the attacher slot) owns the tab.
    blocked = run_skill(
        _ATTACH_PROBE_SCRIPT, backend="extension",
        runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    blocked_payload = _payload(blocked)
    assert blocked.returncode == 0 and "attach_err" in blocked_payload, (
        f"expected the recovery attach to be refused, got: {blocked_payload}\n"
        f"stdout={blocked.stdout!r} stderr={blocked.stderr!r}")
    assert "already attached by another client" in blocked_payload["attach_err"], (
        blocked_payload)

    # The designed escape: reset reaps the executor → slot released → the
    # next operation cold-starts and recovers transparently.
    reset = run_skill(
        _RESET_SCRIPT, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
        extra_env={"BD_SESSION": sid}, timeout=60.0,
    )
    assert reset.returncode == 0 and reset.stdout.startswith("0"), (
        reset.stdout, reset.stderr)

    recovered = None
    for attempt in range(3):
        recovered = run_skill(
            _FRESH_OPERATE_SCRIPT, backend="extension",
            runtime_dir=e2e_daemon.runtime_dir,
            extra_env={"BD_SESSION": sid}, timeout=60.0,
        )
        if recovered.returncode == 0:
            break
        time.sleep(1.0)
    assert recovered is not None and recovered.returncode == 0, (
        f"session did not recover after reset; "
        f"stdout={recovered.stdout!r} stderr={recovered.stderr!r}")
    assert "title" in _payload(recovered), recovered.stdout
