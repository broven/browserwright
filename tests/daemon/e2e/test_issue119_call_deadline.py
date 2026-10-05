"""Issue #119 / ADR-0014 (extension backend, real Chrome): the caller sets the
call deadline, and running out of it is its own error.

`browserwright -e --timeout <seconds>` (default 90) is the call deadline. When
it expires the call is fail-stopped and the CLI exits 7 with `DeadlineExceeded`
and a `[fix]` line naming `--timeout`; the next call on the same session works.

The slow page is served locally and commits only after 12s: a real site would
make timing depend on the internet, and `data:` URLs cannot be navigated over
chrome.debugger at all.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from .conftest import (
    TEST_EXT_FACADE_PORT,
    TEST_EXT_PORT,
    endpoint_url,
    scrubbed_env,
)
from .test_l2_heredoc_playwright_page import (
    _cleanup_session,
    _grep,
    _seed_session,
)
from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)

_REPO = Path(__file__).resolve().parents[3]
#: One JSON line per expired deadline — the repeatable record of "exit 7 at
#: about N seconds" this suite exists to prove (gitignored, like all artifacts).
_EVIDENCE = Path(__file__).resolve().parent / "_artifacts" / "issue119_call_deadline.jsonl"
_BS_HOME = Path(__file__).resolve().parent / "_bs_home" / "extension"

#: How long the slow route holds its response headers. A navigation commits
#: only when they arrive, so this is "a page that commits after 12s".
_SLOW_COMMIT_S = 12.0

#: The CLI's own overhead on top of the deadline: process start, session
#: lookup, `ensureExecutor`, and the daemon-confirmed reap that fail-stop
#: waits for before returning.
_OVERHEAD_S = 8.0


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        name = self.path.strip("/").split("?")[0] or "root"
        if name == "slow":
            time.sleep(_SLOW_COMMIT_S)
        body = (
            f"<!doctype html><title>bw119 {name}</title><main>{name}</main>"
        ).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the browser gave up on the slow page; that is the point

    def log_message(self, *_a):
        pass


@pytest.fixture
def local_site():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _cli_env(runtime_dir: str) -> dict[str, str]:
    env = scrubbed_env()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BS_HOME"] = str(_BS_HOME)
    env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    env["BW_DAEMON_URL"] = endpoint_url(TEST_EXT_FACADE_PORT)
    env["BD_CONFIG"] = ""
    env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _cli(args: list[str], runtime_dir: str, *, timeout: float = 120.0):
    """Run `browserwright <args>`; return (CompletedProcess, elapsed seconds)."""
    binary = Path(sys.executable).with_name("browserwright")
    started = time.monotonic()
    proc = subprocess.run(
        [str(binary), *args], capture_output=True, text=True,
        env=_cli_env(runtime_dir), timeout=timeout,
    )
    return proc, time.monotonic() - started


def _error_envelope(stderr: str) -> dict:
    for line in reversed(stderr.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise AssertionError(f"no JSON error envelope on stderr:\n{stderr}")


def _record(case: str, **fields) -> None:
    _EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    with _EVIDENCE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"case": case, "at": time.time(), **fields}) + "\n")


def _assert_deadline_exceeded(proc, elapsed: float, deadline_s: float) -> None:
    _record(os.environ.get("PYTEST_CURRENT_TEST", "?").split(" ")[0],
            deadline_s=deadline_s, elapsed_s=round(elapsed, 2),
            returncode=proc.returncode, stderr_tail=proc.stderr[-400:])
    detail = f"rc={proc.returncode} elapsed={elapsed:.1f}s\nstdout={proc.stdout!r}\nstderr={proc.stderr}"
    assert proc.returncode == 7, detail
    envelope = _error_envelope(proc.stderr)
    assert envelope["type"] == "DeadlineExceeded", detail
    assert envelope["scope"] == "call", detail
    assert envelope["timeout"] == pytest.approx(deadline_s), detail
    fix_lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("[fix]")]
    assert fix_lines and "--timeout" in fix_lines[-1], detail
    # About the deadline: not before it, and not the 9-12s an inner budget or
    # the slow page itself would take.
    assert deadline_s - 0.5 <= elapsed <= deadline_s + _OVERHEAD_S, detail


def _warm(sid: str, runtime_dir: str, url: str) -> str:
    """Bind the session's executor to a real page; absorb the known cold-start
    announce race the other e2e tests retry through."""
    code = f"page.goto({url!r})\nstate['sentinel'] = 'alive'\nprint('URL=' + page.url)\n"
    proc, _ = _cli(["-s", sid, "-e", code], runtime_dir)
    if proc.returncode != 0:
        _cli(["-s", sid, "-e", "reset()\n"], runtime_dir, timeout=60)
        proc, _ = _cli(["-s", sid, "-e", code], runtime_dir)
    assert proc.returncode == 0, f"warm-up failed: {proc.stdout!r} {proc.stderr!r}"
    return _grep(proc.stdout, "URL")


def test_timeout_fail_stops_executor_code_and_next_call_works(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,
):
    """AC2 on the executor path: `--timeout 5` + a 30s sleep → exit 7 at ~5s,
    and the next call on the same session cold-starts and works."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        warm_url = _warm(sid, runtime_dir, local_site + "/fast")

        proc, elapsed = _cli(
            ["-s", sid, "--timeout", "5", "-e",
             "import time\nstate['touched'] = True\ntime.sleep(30)\n"],
            runtime_dir)
        _assert_deadline_exceeded(proc, elapsed, 5.0)

        after, _ = _cli(
            ["-s", sid, "-e",
             "print('URL=' + page.url)\nprint('STATE=' + repr(state.get('sentinel')))\n"],
            runtime_dir)
        assert after.returncode == 0, f"next call failed: {after.stdout!r} {after.stderr!r}"
        # Same tab, fresh executor: the page survived, `state` did not.
        assert _grep(after.stdout, "URL") == warm_url
        assert _grep(after.stdout, "STATE") == "None"
    finally:
        _cleanup_session("extension", sid)


def test_timeout_fail_stops_in_process_code(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
):
    """AC2 verbatim: `-e --timeout 5 'import time; time.sleep(30)'`.

    That code never touches the browser surface, so it runs in the CLI process
    itself rather than the executor; the call deadline must hold there too."""
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        proc, elapsed = _cli(
            ["-s", sid, "-e", "print('before', flush=True)\nimport time; time.sleep(30)",
             "--timeout", "5"],
            runtime_dir)
        _assert_deadline_exceeded(proc, elapsed, 5.0)
        assert "before" in proc.stdout  # output printed before the cut survives

        after, _ = _cli(["-s", sid, "-e", "print('NEXT=ok')"], runtime_dir)
        assert after.returncode == 0, f"next call failed: {after.stderr!r}"
        assert _grep(after.stdout, "NEXT") == "ok"
    finally:
        _cleanup_session("extension", sid)


def test_call_deadline_caps_a_longer_goto_timeout(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,
):
    """AC4: under `--timeout 3`, `page.goto(slow, timeout=60_000)` to a page
    that commits after 12s fails at ~3s with DeadlineExceeded — not at the
    page's 12s, and not at any inner per-command budget."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")

        proc, elapsed = _cli(
            ["-s", sid, "--timeout", "3", "-e",
             f"page.goto({local_site + '/slow'!r}, timeout=60_000)\n"],
            runtime_dir)
        _assert_deadline_exceeded(proc, elapsed, 3.0)

        # The session is still usable once the slow navigation is abandoned.
        after, _ = _cli(
            ["-s", sid, "-e",
             f"page.goto({local_site + '/after'!r})\nprint('TITLE=' + page.title())\n"],
            runtime_dir, timeout=180)
        assert after.returncode == 0, f"next call failed: {after.stdout!r} {after.stderr!r}"
        assert _grep(after.stdout, "TITLE") == "bw119 after"
    finally:
        _cleanup_session("extension", sid)


def _inflight_timeout_ms(sid: str, args: list[str], runtime_dir: str,
                         monkeypatch) -> int:
    """Start a call and read the deadline the executor itself is enforcing.

    The executor publishes what it is running, with its `timeout_ms`, to a
    sidecar file the moment the call starts — so this reads the value that
    actually reached it, not what the CLI meant to send."""
    from browserwright.daemon import _ipc

    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)
    monkeypatch.setenv("TMPDIR", runtime_dir)
    binary = Path(sys.executable).with_name("browserwright")
    proc = subprocess.Popen(
        [str(binary), "-s", sid, *args,
         "-e", "import time\npage.url\ntime.sleep(4)\n"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_cli_env(runtime_dir))
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            entry = _ipc.read_executor_inflight(sid)
            if entry is not None:
                return int(entry["timeout_ms"])
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(
            f"never saw the call in flight: rc={proc.poll()} "
            f"stderr={proc.stderr.read() if proc.poll() is not None else ''}")
    finally:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_timeout_reaches_the_executor_and_defaults_to_90s(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,
    monkeypatch,
):
    """AC3 (CLI) + AC5: `--timeout` is the deadline the executor enforces, and
    omitting it leaves the 90s default."""
    pytest.importorskip("playwright.sync_api")
    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")
        assert _inflight_timeout_ms(sid, [], runtime_dir, monkeypatch) == 90_000
        assert _inflight_timeout_ms(
            sid, ["--timeout", "42.5"], runtime_dir, monkeypatch) == 42_500
    finally:
        _cleanup_session("extension", sid)


def test_exec_relay_timeout_field_is_the_call_deadline(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,
):
    """AC3 (`/exec`): a raw client's `timeout_ms` is enforced by the executor,
    and expiry answers with the caller-facing DeadlineExceeded."""
    pytest.importorskip("playwright.sync_api")
    from websockets.sync.client import connect

    runtime_dir, _ = _ext_autofacade_ready
    sid = _seed_session(runtime_dir, "extension")
    try:
        _warm(sid, runtime_dir, local_site + "/fast")
        url = f"ws://127.0.0.1:{TEST_EXT_FACADE_PORT}/exec?session={sid}"
        started = time.monotonic()
        with connect(url, open_timeout=30, max_size=None, proxy=None) as ws:
            ws.send(json.dumps({
                "code": "import time\npage.url\ntime.sleep(30)\n",
                "timeout_ms": 2000,
            }))
            response = json.loads(ws.recv(timeout=30))
        elapsed = time.monotonic() - started
        _record("exec-relay", deadline_s=2.0, elapsed_s=round(elapsed, 2),
                exit_code=response.get("exit_code"), error=response.get("error"))
        assert response["exit_code"] == 7, response
        assert response["error"]["type"] == "DeadlineExceeded", response
        assert response["error"]["scope"] == "call", response
        assert "--timeout" in response["error"]["fix"], response
        # Internal: tells the client to reap this executor. Kept, not surfaced.
        assert response["terminal_reason"] == "deadline_exceeded", response
        assert 1.5 <= elapsed <= 2 + _OVERHEAD_S, elapsed
    finally:
        # The raw client skipped the reap a real client performs; let the next
        # test's ensureExecutor find a clean slot.
        _cli(["session", "reset", sid], runtime_dir, timeout=60)
        _cleanup_session("extension", sid)


def _node_with_type_stripping() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    out = subprocess.run([node, "--version"], capture_output=True, text=True)
    try:
        major, minor = (int(p) for p in out.stdout.strip().lstrip("v").split(".")[:2])
    except ValueError:
        return None
    return node if (major, minor) >= (23, 6) else None


def test_pi_fetch_rung_forwards_its_timeout(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    local_site,
):
    """AC3 (pi): the `bw_web_fetch` timeout parameter reaches the executor.

    Runs the pi extension's own CLI call — `runBrowserwright` with the argv
    `bw_web_fetch` builds — against the 12s-commit page with a 3s call
    deadline. It must fail with DeadlineExceeded and the `--timeout` fix, well
    before the page would have committed."""
    pytest.importorskip("playwright.sync_api")
    node = _node_with_type_stripping()
    if node is None:
        pytest.skip("needs node >= 23.6 (unflagged TS type stripping)")
    runtime_dir, _ = _ext_autofacade_ready
    script = (
        "import { runBrowserwright } from './browserwright.ts';\n"
        # `node -e` puts the first script argument at argv[1].
        "const [, url, deadline] = process.argv;\n"
        "const callTimeoutS = deadline ? Number(deadline) : undefined;\n"
        "const started = Date.now();\n"
        "let outcome;\n"
        "try {\n"
        "  const { stdout } = await runBrowserwright(['markdown', url, '--max-chars=50000', '--name=pi-webfetch'], { callTimeoutS });\n"
        "  outcome = { ok: true, content: stdout };\n"
        "} catch (e) { outcome = { ok: false, reason: e.message }; }\n"
        "console.log(JSON.stringify({ ...outcome, ms: Date.now() - started }));\n"
    )

    def fetch(url: str, *call_timeout: str) -> dict:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script, url, *call_timeout],
            cwd=_REPO / "pi-extension", capture_output=True, text=True,
            env=_cli_env(runtime_dir), timeout=120)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    # No timeout from the caller: the call runs under the default deadline.
    # Retried once to absorb the fresh-Chrome cold-start race the other e2e
    # tests retry through.
    warm = fetch(local_site + "/fast")
    if not warm["ok"]:
        warm = fetch(local_site + "/fast")
    assert warm["ok"] is True, warm
    assert "fast" in warm["content"], warm

    outcome = fetch(local_site + "/slow", "3")
    _record("pi-fetch-rung", deadline_s=3.0, elapsed_s=outcome["ms"] / 1000,
            reason=outcome.get("reason"))
    assert outcome["ok"] is False, outcome
    assert outcome["reason"].startswith("DeadlineExceeded"), outcome
    assert "--timeout" in outcome["reason"], outcome
    # DeadlineExceeded (exit 7) already proves the 3s deadline cut the call:
    # under the default 90s the page would have committed at 12s and succeeded.
    assert outcome["ms"] >= 3000, outcome
