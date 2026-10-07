"""Issue #131 -- `recover` must not call a session healthy while a client that
is NOT the session's executor holds the session's tab.

The daemon keeps one attacher per target. The session's resident executor
holding its own tab is by design (see test_repro_extension_reload_orphan).
Here a *stray* connection -- any other process on the session -- holds it, so
every fresh attach to the session's tab is refused with "already attached by
another client". Before the fix `recover` short-circuited on the state
machine's `healthy` and never looked at the attacher table, and `session
reset` only reaps the registered executor, so that attachment survived both.

The stray stays alive for the whole test: its ownership must be released by
`recover`, not by the holder going away.
"""
from __future__ import annotations

import json
import subprocess
import sys

from .conftest import TEST_EXT_PORT, published_endpoint, scrubbed_env
from .helpers import bs_home, run_skill
from .test_l2_recovery import _payload, _seed_session
from .test_repro_extension_reload_orphan import (
    _ATTACH_PROBE_SCRIPT, _RESET_SCRIPT,
)

_RECOVER_SCRIPT = (
    "import json, os, subprocess, sys\n"
    "r = subprocess.run(\n"
    "    [sys.executable, '-m', 'browserwright', 'recover', '--session',\n"
    "     os.environ['BD_SESSION']],\n"
    "    capture_output=True, text=True, env=os.environ, timeout=150)\n"
    "print(json.dumps({'rc': r.returncode, 'out': r.stdout, 'err': r.stderr}))\n"
)

# A process that is not the session's executor: it attaches the session's
# tab over its own control connection and keeps that connection open.
_STRAY_HOLDER = (
    "import json, sys, time\n"
    "from browserwright.session import Session\n"
    "s = Session(record=json.loads(sys.argv[1]))\n"
    "s.cdp.attach(sys.argv[2])\n"
    "print('HELD', flush=True)\n"
    "time.sleep(600)\n"
)


def _run(script: str, runtime_dir: str, sid: str):
    return run_skill(script, backend="extension", runtime_dir=runtime_dir,
                     extra_env={"BD_SESSION": sid}, timeout=180.0)


def _start_stray(runtime_dir: str, record: dict, target_id: str):
    env = scrubbed_env()
    env.update({
        "BW_DAEMON_URL": published_endpoint(runtime_dir),
        "BS_HOME": str(bs_home("extension")),
        "BD_EXTENSION_PORT": str(TEST_EXT_PORT),
        "XDG_RUNTIME_DIR": runtime_dir, "TMPDIR": runtime_dir,
        "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost",
    })
    proc = subprocess.Popen(
        [sys.executable, "-c", _STRAY_HOLDER, json.dumps(record), target_id],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    if "HELD" not in line:
        proc.kill()
        raise AssertionError(f"stray holder failed: {proc.stderr.read()[:2000]}")
    return proc


def test_recover_releases_a_stray_attacher_of_the_sessions_tab(
    ext_ready, e2e_daemon, e2e_chrome,
):
    rt = e2e_daemon.runtime_dir
    sid = "stray-attacher"
    _seed_session(sid, "StrayAttacher")
    record = {"id": sid, "backend": "extension", "workspace": None,
              "owner": "attach", "name": "StrayAttacher"}

    opened = _run("page.goto('about:blank', wait_until='load')\nprint('ok')\n",
                  rt, sid)
    assert opened.returncode == 0, (opened.stdout, opened.stderr)
    # Reap the resident executor so its (legitimate) attachment is gone and the
    # stray can take the tab.
    reset = _run(_RESET_SCRIPT, rt, sid)
    assert reset.stdout.startswith("0"), (reset.stdout, reset.stderr)
    free = _payload(_run(_ATTACH_PROBE_SCRIPT, rt, sid))
    assert free.get("attach_ok"), free
    target_id = free["tid"]

    stray = _start_stray(rt, record, target_id)
    try:
        blocked = _payload(_run(_ATTACH_PROBE_SCRIPT, rt, sid))
        assert "already attached by another client" in blocked.get(
            "attach_err", ""), blocked

        rec = _payload(_run(_RECOVER_SCRIPT, rt, sid))
        assert rec["rc"] == 0, rec
        assert "healthy" in rec["out"], rec
        # The healthy verdict is backed by a reported release.
        assert target_id in rec["err"] and "released" in rec["err"], rec

        # The ladder may hand the tab back to the session's executor (its
        # by-design attacher); reap it, as `session reset` always could. Then a
        # fresh outside attach must succeed while the stray is still alive --
        # before the fix the stray's ownership survived both verbs.
        reset = _run(_RESET_SCRIPT, rt, sid)
        assert reset.stdout.startswith("0"), (reset.stdout, reset.stderr)
        fresh = _start_stray(rt, record, target_id)
        fresh.kill()
        fresh.wait()
        assert stray.poll() is None, "the stray must still be alive"
    finally:
        stray.kill()
        stray.wait()


def test_recover_keeps_the_resident_executors_attachment(
    ext_ready, e2e_daemon, e2e_chrome,
):
    """The session's own live executor holding its tab is by design: a
    `recover` on that healthy session releases nothing."""
    rt = e2e_daemon.runtime_dir
    sid = "resident-attacher"
    _seed_session(sid, "ResidentAttacher")
    opened = _run("page.goto('about:blank', wait_until='load')\nprint('ok')\n",
                  rt, sid)
    assert opened.returncode == 0, (opened.stdout, opened.stderr)

    rec = _payload(_run(_RECOVER_SCRIPT, rt, sid))
    assert rec["rc"] == 0 and "healthy" in rec["out"], rec
    assert "released" not in rec["err"], rec

    # The executor still owns the tab, so an outside attach is still refused.
    probe = _payload(_run(_ATTACH_PROBE_SCRIPT, rt, sid))
    assert "already attached by another client" in probe.get("attach_err", ""), probe
    again = _run("print(page.title() is not None)\n", rt, sid)
    assert again.returncode == 0 and "True" in again.stdout, (
        again.stdout, again.stderr)
