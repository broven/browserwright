"""L2 -- Phase C PR1: the heredoc-injected Playwright `page` / `context`.

Proves the Phase C foundation end to end through the REAL `browserwright`
heredoc CLI (NOT a direct Playwright client):

  1. cross-heredoc tab REUSE: heredoc #1 `page.goto(url1)` then a SEPARATE
     heredoc #2 `page.goto(url2)` land on the SAME tab (same targetId, the tab
     count did not grow) — the tab-explosion fix. Then `context.new_page()`
     explicitly creates a second tab.
  2. lazy: a memory-only heredoc that never touches `page`/`context` opens NO
     browser connection (the facade sees no client).

Run on BOTH backends: cdp (cheapest) + the extension CfT harness.

The daemon AUTO-ENABLES the facade now (Phase C) — these fixtures spawn it
WITHOUT `--facade-port`, then read the advertised ws from
`browserwright-daemon status --json` to prove discovery works.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from .conftest import (
    TEST_EXT_FACADE_PORT,
    TEST_EXT_PORT,
    TEST_AUTOFACADE_PORT,
    TEST_CDP_PORT,
    _isolated_runtime_dir,
)
from .helpers import run_skill
from .test_l2_multisession import (
    _chrome_close_tabs,
    _extension_id_from_path,
    _extension_worker_target_id,
)


def _port_free(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _describe_holders(port: int) -> str:
    """Who listens on `port` (via lsof), for failure messages.

    One line per holder. The common failure mode is the machine-global daemon
    or a sibling worktree's run holding the port (issue #44 B) — naming the
    holder turns a misleading "facade never advertised" into an actionable
    "pid X (cmdline) is listening on the port"."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or "(nobody)"
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return "(lsof unavailable)"


def _status_facade_ws(env: dict, deadline_s: float = 10.0) -> str | None:
    """Read the daemon's advertised facade ws via `status --json`."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        res = subprocess.run(
            ["browserwright-daemon", "status", "--json"],
            capture_output=True, text=True, env=env,
        )
        if res.returncode == 0 and res.stdout.strip():
            try:
                status = json.loads(res.stdout)
            except json.JSONDecodeError:
                status = {}
            facade = status.get("facade")
            if status.get("alive") and isinstance(facade, dict) and facade.get("ws"):
                return facade["ws"]
        time.sleep(0.2)
    return None


# ---------------------------------------------------------------------------
#   cdp backend (auto-facade, no --facade-port)
# ---------------------------------------------------------------------------


@pytest.fixture
def cdp_autofacade_daemon(e2e_chrome_cdp, e2e_artifacts_dir):
    """Spawn the cdp daemon WITHOUT --facade-port and prove the facade
    auto-enabled (advertised via `status --json`). Yields (runtime_dir,
    facade_ws).

    Auto-enable is proven by the ABSENCE of the CLI flag, not by the default
    port: the facade is steered to this worktree's derived port via
    BD_FACADE_PORT so it can never collide with the machine-global daemon's
    default facade port (19990) or a sibling worktree's run (issue #44 B).
    The None->default-port mapping itself is unit-tested
    (test_phase_c_foundation_unit.py)."""
    import shutil

    log_path = e2e_artifacts_dir / "daemon-cdp-autofacade.log"
    log_fh = open(log_path, "wb")  # noqa: SIM115

    runtime_dir = _isolated_runtime_dir()
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BD_CDP_PORT"] = str(TEST_CDP_PORT)
    env["BD_FACADE_PORT"] = str(TEST_AUTOFACADE_PORT)
    env["BS_HOME"] = str(Path(__file__).resolve().parent / "_bs_home" / "cdp")
    env["BD_CONFIG"] = ""

    subprocess.run(["browserwright-daemon", "stop"], capture_output=True, env=env)

    proc = subprocess.Popen(
        [sys.executable, "-m", "browserwright.daemon.cli", "serve",
         "--backend", "cdp", "-v"],
        stdout=log_fh, stderr=subprocess.STDOUT, env=env,
    )

    facade_ws = _status_facade_ws(env)
    if facade_ws is None:
        log_fh.flush()
        proc.terminate()
        pytest.fail(
            f"cdp daemon never advertised a facade ws on port "
            f"{TEST_AUTOFACADE_PORT}; holders of that port: "
            f"{_describe_holders(TEST_AUTOFACADE_PORT)}; see {log_path}"
        )

    yield runtime_dir, facade_ws

    subprocess.run(["browserwright-daemon", "stop"], capture_output=True, env=env)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    log_fh.close()
    shutil.rmtree(runtime_dir, ignore_errors=True)


def _seed_session(runtime_dir: str, backend: str,
                  owner: str = "attach") -> str:
    """Create a persistent ledger session under the backend's BS_HOME and
    return its id. Unlike helpers.run_skill's transient record, this one
    survives across heredoc invocations so the SECOND heredoc resolves the same
    bound tab from the ledger.

    ``owner`` defaults to ``"attach"`` (the common case: the test attaches to a
    daemon-launched Chrome). Pass ``owner="create"`` when the test needs
    ``session end`` to actually drive the daemon's endSession verb — only a
    create-owned session contacts the daemon on teardown (an attach session
    deliberately leaves the browser untouched, ``session_create.end``)."""
    bs_home = Path(__file__).resolve().parent / "_bs_home" / backend
    sessions_dir = bs_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = sessions_dir / "ledger.json"
    sid = f"e2e-phasec-{uuid.uuid4().hex}"
    now = time.time()
    record = {
        "id": sid, "backend": backend, "workspace": None, "owner": owner,
        "name": "e2e-phasec", "created_at": now, "last_seen": now,
    }
    # Merge into any existing ledger so we don't clobber other sessions.
    try:
        existing = json.loads(ledger_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {"next_id": 1, "sessions": {}}
    existing.setdefault("sessions", {})[sid] = record
    ledger_path.write_text(json.dumps(existing), encoding="utf-8")
    return sid


def _cleanup_session(backend: str, sid: str) -> None:
    ledger_path = (Path(__file__).resolve().parent / "_bs_home" / backend
                   / "sessions" / "ledger.json")
    try:
        data = json.loads(ledger_path.read_text())
        data.get("sessions", {}).pop(sid, None)
        ledger_path.write_text(json.dumps(data), encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


# The bound targetId is read from the LEDGER between heredocs (the handle
# persists it). We avoid `context.new_cdp_session(page)` for the in-script
# targetId because a per-page CDP session collides with the page's primary
# session over the extension facade and crashes the driver — the production
# handle maps via a browser-level Target.getTargets for the same reason.
# cdp backend drives `data:` navigations fine (only the extension backend
# aborts them over chrome.debugger — see the facade spec).
_REUSE_SCRIPT_1 = (
    "page.goto('data:text/html,<title>one</title>', wait_until='load')\n"
    "print('TITLE1=' + page.title())\n"
    "print('NPAGES1=' + str(len(context.pages)))\n"
)

_REUSE_SCRIPT_2 = (
    "page.goto('data:text/html,<title>two</title>', wait_until='load')\n"
    "print('TITLE2=' + page.title())\n"
    "print('NPAGES2=' + str(len(context.pages)))\n"
    "p2 = context.new_page()\n"
    "print('NPAGES3=' + str(len(context.pages)))\n"
)


def _grep(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(key + "="):
            return line[len(key) + 1:]
    raise AssertionError(f"{key}= not found in output:\n{out}")


def _bound_target(backend: str, sid: str) -> str | None:
    """The session's persisted `current_target_id` from the ledger (what the
    handle binds + persists each heredoc)."""
    ledger_path = (Path(__file__).resolve().parent / "_bs_home" / backend
                   / "sessions" / "ledger.json")
    try:
        data = json.loads(ledger_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    rec = data.get("sessions", {}).get(sid, {})
    return (rec.get("runtime") or {}).get("current_target_id")


def _chrome_group_tab_ids(chrome, extension_id: str, group_id: int) -> list[int]:
    from browserwright.cdp import CDPSession

    cdp = CDPSession(chrome.ws_url)
    try:
        worker = _extension_worker_target_id(cdp, extension_id)
        session_id = cdp.attach(worker)
        expression = (
            "(async () => {"
            f"const gid = {int(group_id)};"
            "const tabs = await chrome.tabs.query({groupId: gid});"
            "return tabs.map(t => t.id);"
            "})()"
        )
        result = cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
    finally:
        cdp.close()
    if "exceptionDetails" in result:
        raise AssertionError(f"tab group query failed: {result!r}")
    return list(result.get("result", {}).get("value", []))


def _wait_extension_worker_connected(chrome, extension_id: str) -> None:
    from browserwright.cdp import CDPSession

    deadline = time.monotonic() + 10.0
    last_result = None
    while time.monotonic() < deadline:
        cdp = CDPSession(chrome.ws_url)
        try:
            worker = _extension_worker_target_id(cdp, extension_id)
            session_id = cdp.attach(worker)
            result = cdp.send(
                "Runtime.evaluate",
                session=session_id,
                expression="!!ws && ws.readyState === WebSocket.OPEN",
                returnByValue=True,
            )
            last_result = result
            value = result.get("result", {}).get("value")
            if value is True:
                return
        except Exception as e:  # noqa: BLE001
            last_result = repr(e)
        finally:
            cdp.close()
        time.sleep(0.2)
    raise AssertionError(
        f"extension worker did not report relay connection; last={last_result!r}")


def _session_group_title(sid: str) -> str | None:
    """ADR-0009: the session's tab-group title `<name>-BW<sid>`, derived from
    the ledger exactly like the daemon's ``session_group_title``."""
    ledger_path = (Path(__file__).resolve().parent / "_bs_home" / "extension"
                   / "sessions" / "ledger.json")
    try:
        data = json.loads(ledger_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    name = (data.get("sessions", {}).get(sid, {}) or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return f"{name.strip()}-BW{sid}"


def _session_group_id(chrome, extension_id: str, sid: str) -> int | None:
    """Resolve the session's LIVE Chrome tab group by its title (ADR-0009) —
    not from the ledger, which no longer stores a numeric group id. Returns
    the matching group's id, or None when no group carries the title (user
    renamed it / it never existed)."""
    title = _session_group_title(sid)
    if title is None:
        return None
    return _chrome_group_id_by_title(chrome, extension_id, title)


def _chrome_group_id_by_title(chrome, extension_id: str, title: str) -> int | None:
    """Exact-title group lookup through the extension worker. Deliberately
    `chrome.tabGroups.query({})` + exact compare, mirroring background.js —
    `query({title})` matches a PATTERN, so a `*`/`?` in the name would change
    what matches (ADR-0009)."""
    from browserwright.cdp import CDPSession

    cdp = CDPSession(chrome.ws_url)
    try:
        worker = _extension_worker_target_id(cdp, extension_id)
        session_id = cdp.attach(worker)
        expression = (
            "(async () => {"
            f"const expected = {json.dumps(title)};"
            "const groups = await chrome.tabGroups.query({});"
            "const hit = groups.find(g => g.title === expected);"
            "return hit ? hit.id : -1;"
            "})()"
        )
        result = cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
    finally:
        cdp.close()
    if "exceptionDetails" in result:
        raise AssertionError(f"tab group title query failed: {result!r}")
    value = result.get("result", {}).get("value")
    return value if isinstance(value, int) and value >= 0 else None


def _assert_one_session_group(chrome, extension_id: str, sid: str) -> list[int]:
    gid = _session_group_id(chrome, extension_id, sid)
    assert gid is not None, (
        f"session {sid} has no live group titled "
        f"{_session_group_title(sid)!r} (ADR-0009)")
    tab_ids = _chrome_group_tab_ids(chrome, extension_id, gid)
    assert tab_ids, f"session group {gid} has no tabs"
    return tab_ids


def _run_execute(script: str, *, sid: str, runtime_dir: str,
                 timeout: float = 60) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BS_HOME"] = str(Path(__file__).resolve().parent
                         / "_bs_home" / "extension")
    env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    env["BD_CONFIG"] = ""
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    skill_bin = Path(sys.executable).with_name("browserwright")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if not skill_bin.exists():
        skill_bin = Path("browserwright")
    return subprocess.run(
        [str(skill_bin), "-s", sid, "-e", script],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def _one_group_case(
    *,
    sid: str,
    script: str,
    runtime_dir: str,
    chrome,
    extension_id: str,
    timeout: float = 60,
) -> None:
    try:
        _wait_extension_worker_connected(chrome, extension_id)
        result = _run_execute(
            script, sid=sid, runtime_dir=runtime_dir, timeout=timeout)
        assert result.returncode == 0, (
            f"heredoc failed; stdout={result.stdout!r} stderr={result.stderr!r}")
        tab_ids = _assert_one_session_group(chrome, extension_id, sid)
    finally:
        gid = _session_group_id(chrome, extension_id, sid)
        tab_ids = (
            _chrome_group_tab_ids(chrome, extension_id, gid)
            if gid is not None else []
        )
        _chrome_close_tabs(chrome, extension_id, tab_ids)
        _cleanup_session("extension", sid)


def test_one_group_playwright_first_extension(ext_autofacade_ready, e2e_chrome,
                                              patched_ext_dir):
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    _one_group_case(
        sid=sid,
        runtime_dir=runtime_dir,
        chrome=e2e_chrome,
        extension_id=extension_id,
        script=(
            "page.goto('about:blank', wait_until='load')\n"
            "print('ok')\n"
        ),
    )


def test_one_group_agent_first_extension(ext_autofacade_ready, e2e_chrome,
                                         patched_ext_dir):
    runtime_dir, _facade_ws = ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    _one_group_case(
        sid=sid,
        runtime_dir=runtime_dir,
        chrome=e2e_chrome,
        extension_id=extension_id,
        script=(
            "from browserwright.session import current_session\n"
            "from browserwright.session_runtime import open_session_tab\n"
            "tab = open_session_tab(current_session(), 'about:blank')\n"
            "print(tab['targetId'])\n"
        ),
    )


def test_one_group_mixed_extension(ext_autofacade_ready, e2e_chrome,
                                   patched_ext_dir):
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    _one_group_case(
        sid=sid,
        runtime_dir=runtime_dir,
        chrome=e2e_chrome,
        extension_id=extension_id,
        script=(
            "from browserwright.session import current_session\n"
            "from browserwright.session_runtime import open_session_tab\n"
            "open_session_tab(current_session(), 'about:blank')\n"
            "page.goto('about:blank', wait_until='load')\n"
            "print('ok')\n"
        ),
    )


def test_one_group_after_recover_extension(ext_autofacade_ready, e2e_chrome,
                                           patched_ext_dir):
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = ext_autofacade_ready
    extension_id = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    try:
        _wait_extension_worker_connected(e2e_chrome, extension_id)
        first = _run_execute(
            "page.goto('about:blank', wait_until='load')\nprint('first')\n",
            sid=sid,
            runtime_dir=runtime_dir,
            timeout=60,
        )
        assert first.returncode == 0, (
            f"initial heredoc failed; stdout={first.stdout!r} stderr={first.stderr!r}")

        subprocess.run(
            ["browserwright-daemon", "kill-executor", "--session", sid],
            capture_output=True,
            env={
                **os.environ,
                "XDG_RUNTIME_DIR": runtime_dir,
                "TMPDIR": runtime_dir,
                "BS_HOME": str(Path(__file__).resolve().parent
                               / "_bs_home" / "extension"),
                "BD_EXTENSION_PORT": str(TEST_EXT_PORT),
                "BD_CONFIG": "",
            },
            timeout=10,
        )

        second = _run_execute(
            "page.goto('about:blank', wait_until='load')\nprint('second')\n",
            sid=sid,
            runtime_dir=runtime_dir,
            timeout=60,
        )
        assert second.returncode == 0, (
            f"recovered heredoc failed; stdout={second.stdout!r} stderr={second.stderr!r}")
        tab_ids = _assert_one_session_group(e2e_chrome, extension_id, sid)
    finally:
        gid = _session_group_id(e2e_chrome, extension_id, sid)
        tab_ids = (
            _chrome_group_tab_ids(e2e_chrome, extension_id, gid)
            if gid is not None else []
        )
        _chrome_close_tabs(e2e_chrome, extension_id, tab_ids)
        _cleanup_session("extension", sid)


def test_cross_heredoc_tab_reuse_cdp(cdp_autofacade_daemon):
    """ACCEPTANCE (cdp): two SEPARATE heredocs reuse the same tab; new_page()
    explicitly opens a second one."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = cdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "cdp")
    extra = {"BD_SESSION": sid}
    try:
        r1 = run_skill(_REUSE_SCRIPT_1,
                       backend="cdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r1.returncode == 0, f"heredoc#1 failed: {r1.stderr}"
        tid1 = _bound_target("cdp", sid)
        assert tid1, "heredoc#1 did not bind/persist a target"

        r2 = run_skill(_REUSE_SCRIPT_2,
                       backend="cdp", runtime_dir=runtime_dir, extra_env=extra)
        assert r2.returncode == 0, f"heredoc#2 failed: {r2.stderr}"
        tid2 = _bound_target("cdp", sid)
        assert _grep(r2.stdout, "TITLE2") == "two"
        npages2 = int(_grep(r2.stdout, "NPAGES2"))
        npages3 = int(_grep(r2.stdout, "NPAGES3"))

        # Same tab across heredocs (the reuse acceptance): the bound targetId
        # persisted by heredoc#1 is what heredoc#2 re-bound.
        assert tid1 == tid2, f"tab NOT reused: {tid1} != {tid2}"
        # new_page() explicitly grew the tab count by one.
        assert npages3 == npages2 + 1, (
            f"new_page() did not open a second tab: {npages2} -> {npages3}")
    finally:
        _cleanup_session("cdp", sid)


def test_memory_only_heredoc_does_not_connect_cdp(cdp_autofacade_daemon):
    """ACCEPTANCE (lazy): a heredoc that never touches page/context opens no
    browser connection. We assert the script runs green WITHOUT requiring a
    page bind, and (belt + suspenders) that no extra tab was created."""
    runtime_dir, _facade_ws = cdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "cdp")
    # Count tabs WITHOUT binding a `page`. Unlike touching `context`/`page`
    # (which connects the facade and auto-binds/opens the session's tab via
    # resolve_current_target), the internal `session_tabs` helper is a pure
    # read that does NOT open a tab. That is what makes it a valid bracket for
    # the lazy memory-only heredoc.
    count_probe = (
        "from browserwright.session import current_session\n"
        "from browserwright.session_runtime import session_tabs\n"
        "print('NTABS=' + str(len(session_tabs(current_session()))))"
    )
    try:
        before = run_skill(count_probe,
                           backend="cdp", runtime_dir=runtime_dir,
                           extra_env={"BD_SESSION": sid})
        # A pure-Python heredoc: no page/context/snapshot access at all.
        r = run_skill("print('answer=' + str(6 * 7))",
                      backend="cdp", runtime_dir=runtime_dir,
                      extra_env={"BD_SESSION": sid})
        assert r.returncode == 0, f"memory heredoc failed: {r.stderr}"
        assert "answer=42" in r.stdout
        after = run_skill(count_probe,
                          backend="cdp", runtime_dir=runtime_dir,
                          extra_env={"BD_SESSION": sid})
        assert _grep(before.stdout, "NTABS") == _grep(after.stdout, "NTABS"), (
            "memory-only heredoc opened a tab (not lazy)")
    finally:
        _cleanup_session("cdp", sid)


# ---------------------------------------------------------------------------
#   PR2: snapshot() ref → locator round-trip (cdp)
# ---------------------------------------------------------------------------


# A page with known interactive elements; click the snapshot's ref for the
# button, then assert the click handler ran (sets <title>). Proves the
# snapshot -> page.locator("aria-ref=eN") round-trip end to end through the
# real heredoc CLI. cdp drives data: navigations fine (the extension backend
# does not — see the facade spec).
_SNAPSHOT_SCRIPT_CDP = (
    "import re\n"
    "page.goto('data:text/html,"
    "<button onclick=%22document.title=%27clicked%27%22>Press me</button>"
    "<input placeholder=Search>', wait_until='load')\n"
    "snap = snapshot()\n"
    "print('SNAP_START')\n"
    "print(snap)\n"
    "print('SNAP_END')\n"
    "btn = [l for l in snap.splitlines() if 'Press me' in l][0]\n"
    "ref = re.search(r'\\[ref=(\\w+)\\]', btn).group(1)\n"
    "print('BTNREF=' + ref)\n"
    "page.locator('aria-ref=' + ref).click()\n"
    "print('TITLE=' + page.title())\n"
    "box = [l for l in snap.splitlines() if 'Search' in l][0]\n"
    "bref = re.search(r'\\[ref=(\\w+)\\]', box).group(1)\n"
    "page.locator('aria-ref=' + bref).fill('hello')\n"
    "print('INPUT=' + page.locator('aria-ref=' + bref).input_value())\n"
)


def test_snapshot_ref_roundtrip_cdp(cdp_autofacade_daemon):
    """PR2 ACCEPTANCE (cdp): snapshot() yields [ref=eN] lines for the page's
    interactive elements, and page.locator("aria-ref=eN") resolves each ref to
    a clickable / fillable locator."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = cdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "cdp")
    try:
        r = run_skill(_SNAPSHOT_SCRIPT_CDP, backend="cdp",
                      runtime_dir=runtime_dir, extra_env={"BD_SESSION": sid})
        assert r.returncode == 0, f"snapshot heredoc failed: {r.stderr}"
        snap = r.stdout.split("SNAP_START\n", 1)[1].split("\nSNAP_END", 1)[0]
        # The snapshot carries refs for the interactive elements.
        assert "[ref=" in snap, f"no refs in snapshot:\n{snap}"
        assert "Press me" in snap and "button" in snap
        assert "Search" in snap and "textbox" in snap
        # The button ref clicked (its onclick set the title)...
        assert _grep(r.stdout, "TITLE") == "clicked", (
            f"aria-ref click did not run the handler:\n{r.stdout}")
        # ...and the textbox ref filled.
        assert _grep(r.stdout, "INPUT") == "hello", (
            f"aria-ref fill did not take:\n{r.stdout}")
    finally:
        _cleanup_session("cdp", sid)


# ---------------------------------------------------------------------------
#   extension backend (auto-facade, CfT harness)
# ---------------------------------------------------------------------------


@pytest.fixture
def ext_autofacade_ready(e2e_daemon, ext_ready):
    """Reuse the session-scoped extension daemon. Yields (runtime_dir, facade_ws).

    The CfT extension is patched to dial ONE relay port (the per-worktree
    TEST_EXT_PORT), so only one extension daemon can own it per pytest session —
    a second daemon on that port would collide (this fixture used to spawn one
    and failed `_port_free` whenever the session-scoped `e2e_daemon` was alive
    in a full-suite run). `e2e_daemon` already serves TEST_EXT_PORT WITH a
    facade on `conftest.TEST_EXT_FACADE_PORT`, and `ext_ready` blocks until the
    extension SW has connected — exactly what these consumers need (a usable
    facade ws).
    """
    yield (e2e_daemon.runtime_dir,
           f"ws://127.0.0.1:{TEST_EXT_FACADE_PORT}/cdp")


# Extension backend `page.goto("data:...")` is aborted by Chrome over
# chrome.debugger (see the facade spec); use set_content for inline HTML. The
# bound page is already on about:blank (new_page init), so we don't re-goto it
# — re-navigating about:blank over chrome.debugger can stall.
_EXT_SCRIPT_1 = (
    "page.set_content('<title>one</title>', wait_until='load')\n"
    "print('TITLE1=' + page.title())\n"
)

_EXT_SCRIPT_2 = (
    "page.set_content('<title>two</title>', wait_until='load')\n"
    "print('TITLE2=' + page.title())\n"
    "n_before = len(context.pages)\n"
    "p2 = context.new_page()\n"
    "print('GREW=' + str(len(context.pages) == n_before + 1))\n"
)


# Extension backend: data: navigations abort over chrome.debugger, so seed the
# page with set_content. Then snapshot() and round-trip a ref to a click.
_SNAPSHOT_SCRIPT_EXT = (
    "import re\n"
    "page.set_content("
    "'<button onclick=\"document.title=\\'clicked\\'\">Press me</button>"
    "<input placeholder=Search>', wait_until='load')\n"
    "snap = snapshot()\n"
    "print('SNAP_START')\n"
    "print(snap)\n"
    "print('SNAP_END')\n"
    "btn = [l for l in snap.splitlines() if 'Press me' in l][0]\n"
    "ref = re.search(r'\\[ref=(\\w+)\\]', btn).group(1)\n"
    "print('BTNREF=' + ref)\n"
    "page.locator('aria-ref=' + ref).click()\n"
    "print('TITLE=' + page.title())\n"
)


def test_snapshot_ref_roundtrip_extension(ext_autofacade_ready):
    """PR2 ACCEPTANCE (extension/CfT): snapshot() refs round-trip to a clickable
    locator over the extension facade."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        r = run_skill(_SNAPSHOT_SCRIPT_EXT, backend="extension",
                      runtime_dir=runtime_dir, extra_env={"BD_SESSION": sid},
                      timeout=60)
        assert r.returncode == 0, f"ext snapshot heredoc failed: {r.stderr}"
        snap = r.stdout.split("SNAP_START\n", 1)[1].split("\nSNAP_END", 1)[0]
        assert "[ref=" in snap, f"no refs in ext snapshot:\n{snap}"
        assert "Press me" in snap and "button" in snap
        assert _grep(r.stdout, "TITLE") == "clicked", (
            f"ext aria-ref click did not run the handler:\n{r.stdout}")
    finally:
        _cleanup_session("extension", sid)


def test_cross_heredoc_tab_reuse_extension(ext_autofacade_ready):
    """ACCEPTANCE (extension): two SEPARATE heredocs reuse the same tab over the
    CfT harness; new_page() explicitly opens a second one."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    extra = {"BD_SESSION": sid}
    try:
        r1 = run_skill(_EXT_SCRIPT_1, backend="extension",
                       runtime_dir=runtime_dir, extra_env=extra, timeout=60)
        assert r1.returncode == 0, f"ext heredoc#1 failed: {r1.stderr}"
        assert _grep(r1.stdout, "TITLE1") == "one"
        tid1 = _bound_target("extension", sid)
        assert tid1, "ext heredoc#1 did not bind/persist a target"

        r2 = run_skill(_EXT_SCRIPT_2, backend="extension",
                       runtime_dir=runtime_dir, extra_env=extra, timeout=60)
        assert r2.returncode == 0, f"ext heredoc#2 failed: {r2.stderr}"
        tid2 = _bound_target("extension", sid)

        # Same tab across heredocs: heredoc#2 re-bound the targetId heredoc#1
        # persisted (no new tab), and set_content drove that same tab.
        assert tid1 == tid2, f"ext tab NOT reused: {tid1} != {tid2}"
        assert _grep(r2.stdout, "TITLE2") == "two"
        assert _grep(r2.stdout, "GREW") == "True", "new_page() did not grow tabs"
    finally:
        _cleanup_session("extension", sid)


def test_facade_survives_a_wiped_runtime_dir_cdp(cdp_autofacade_daemon):
    """REGRESSION (2026-08-19 outage): the facade must stay usable after every
    regular file in the runtime dir is deleted out from under the live daemon.

    What happened: the facade endpoint was published to a
    `browserwright-daemon.facade` discovery file under /tmp. macOS reaps
    regular files there after three days — sockets survive, so the daemon kept
    serving and the facade kept LISTENing while `status --json` reported
    `facade: null` and EVERY `browserwright -s <id> -e ...` call died with
    "facade discovery file absent". `doctor` was all green throughout.

    The endpoint now lives only in the daemon's memory and is answered live
    over `/__ping__`, so wiping the directory can't hide it. This test wipes
    it exactly the way the reaper does (regular files only, socket untouched)
    and then drives a real heredoc through the facade.
    """
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _facade_ws = cdp_autofacade_daemon
    sid = _seed_session(runtime_dir, "cdp")
    extra = {"BD_SESSION": sid}

    # Reproduce the reaper: unlink regular files, leave the unix socket alone.
    wiped = []
    for entry in Path(runtime_dir).iterdir():
        if entry.is_file() and not entry.is_symlink():
            entry.unlink()
            wiped.append(entry.name)
    assert wiped, f"nothing to wipe in {runtime_dir} — test would prove nothing"

    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir

    try:
        # 1. The daemon still advertises the facade: memory, not the filesystem.
        assert _status_facade_ws(env) is not None, (
            f"facade vanished from `status --json` after wiping {wiped}; "
            "the endpoint is being read from a file again")

        # 2. And it is actually usable: a real heredoc drives a real page.
        res = run_skill(_REUSE_SCRIPT_1,
                        backend="cdp", runtime_dir=runtime_dir, extra_env=extra)
        assert res.returncode == 0, (
            f"heredoc failed after wiping {wiped}: {res.stderr}")
        assert _bound_target("cdp", sid), "heredoc bound no target"
    finally:
        _cleanup_session("cdp", sid)
