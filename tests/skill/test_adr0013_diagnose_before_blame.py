"""ADR-0013 sequencing step 1 (spec #92; issues #88 / #89 / #90).

Three contracts, all about what an *agent* sees when browserwright fails:

1. **Probe before blaming.** An "endpoint unavailable" error carries what the
   client actually observed — refused / foreign responder / daemon on another
   address — never the guess "restart the daemon".
2. **One recovery vocabulary.** No agent-visible remediation may say
   `restart`, `serve`, `--force`, or `session end`.
3. **`session new --reuse`** hands back the existing session of that name.

Plus the ADR-0012 rule 5 log primitives: the "already running" collapse and
the attributed lifecycle line.
"""
from __future__ import annotations

import json
import re
import socket
import threading
from pathlib import Path

import pytest

from browserwright.daemon._ipc import EndpointProbe


# ---- 1. probe before blaming ------------------------------------------------


def _serve_once(payload: bytes) -> tuple[str, int, threading.Thread]:
    """A one-shot TCP server on an ephemeral loopback port that answers
    ``payload`` to whatever it receives, then closes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5.0)
    host, port = srv.getsockname()

    def run():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(2.0)
                try:
                    conn.recv(4096)
                except OSError:
                    pass
                conn.sendall(payload)
        except OSError:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return host, port, t


def test_probe_classifies_a_503_proxy_as_foreign():
    from browserwright.daemon._ipc import probe_endpoint_sync

    host, port, t = _serve_once(
        b"HTTP/1.1 503 Service Unavailable\r\nServer: Surge/5.0\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n")
    pr = probe_endpoint_sync(host, port, timeout=2.0)
    t.join(3.0)
    assert pr.kind == "foreign"
    assert pr.status_line.startswith("HTTP/1.1 503")
    assert pr.server == "Surge/5.0"
    assert "something other than browserwright" in pr.describe()


def test_probe_recognises_our_pong():
    from browserwright.daemon._ipc import probe_endpoint_sync

    body = json.dumps({"pong": True, "pid": 4321, "version": "9.9.9"}).encode()
    host, port, t = _serve_once(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    pr = probe_endpoint_sync(host, port, timeout=2.0)
    t.join(3.0)
    assert pr.kind == "ours"
    assert pr.pid == 4321 and pr.version == "9.9.9"


def test_probe_reports_refused_when_nothing_listens():
    from browserwright.daemon._ipc import probe_endpoint_sync

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    pr = probe_endpoint_sync("127.0.0.1", port, timeout=1.0)
    assert pr.kind == "refused"
    assert "nothing is listening" in pr.describe()


def test_real_503_server_end_to_end_names_the_responder_without_banned_words(
        monkeypatch, tmp_path):
    """Spec #92 testing decision 1, unstubbed: a fake HTTP server answering
    503 on the resolved endpoint produces a fix naming a non-browserwright
    responder, with no banned word. Restores the real probe for this test."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    import browserwright.daemon_url as du
    from browserwright.daemon._ipc import probe_endpoint_sync

    monkeypatch.setattr(du, "probe", probe_endpoint_sync)
    host, port, t = _serve_once(
        b"HTTP/1.1 503 Service Unavailable\r\nServer: Surge/5.0\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n")
    # Pin the default candidate to a closed port so the diagnosis never
    # dials the developer's daemon (the conftest wall's vector B).
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    monkeypatch.setattr(du, "DEFAULT_DAEMON_URL", f"http://127.0.0.1:{dead_port}")

    fix = du.local_unreachable_fix(_endpoint(f"http://{host}:{port}", "default"))
    t.join(3.0)
    assert "something other than browserwright" in fix
    assert "503" in fix and "Surge/5.0" in fix
    for word in BANNED:
        assert word not in fix, word


def _endpoint(url: str, source: str):
    from browserwright.daemon_url import DaemonEndpoint
    return DaemonEndpoint(url=url, explicit=(source in ("cli", "env", "toml")),
                          source=source)


def test_refused_with_a_state_file_naming_another_host_reports_both_probes(
        monkeypatch, tmp_path):
    """Connection refused on the default, but the daemon answers at the
    address it published: the message says both and gives the export."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": "http://100.72.20.32:19990", "pid": 31305}))
    import browserwright.daemon_url as du

    def fake_probe(host, port, timeout=1.5):
        if host == "100.72.20.32":
            return EndpointProbe(kind="ours", host=host, port=port, pid=31305,
                                 version="0.17.4")
        return EndpointProbe(kind="refused", host=host, port=port,
                             detail="Connection refused")

    monkeypatch.setattr(du, "probe", fake_probe)
    fix = du.local_unreachable_fix(_endpoint("http://127.0.0.1:19990", "default"))
    assert "nothing is listening at 127.0.0.1:19990" in fix
    assert "a browserwright daemon answers at 100.72.20.32:19990" in fix
    assert "export BW_DAEMON_URL=http://100.72.20.32:19990" in fix
    assert "restart" not in fix and "serve" not in fix


def test_transient_failure_says_retry_and_version_skew_says_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    import browserwright.daemon_url as du
    from browserwright.version import package_version

    monkeypatch.setattr(du, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="ours", host=h, port=p, pid=1, version=package_version()))
    fix = du.local_unreachable_fix(_endpoint("http://127.0.0.1:19990", "default"))
    assert "transient" in fix and "Retry" in fix

    monkeypatch.setattr(du, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="ours", host=h, port=p, pid=1, version="0.0.1"))
    fix = du.local_unreachable_fix(_endpoint("http://127.0.0.1:19990", "default"))
    assert "stale" in fix and "version check" in fix
    assert "restart" not in fix


def test_session_unreachable_carries_the_diagnosis(monkeypatch, tmp_path):
    """Through the real raise site, not just the helper."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.delenv("BD_CONFIG", raising=False)
    import browserwright.daemon_url as du

    monkeypatch.setattr(du, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="foreign", host=h, port=p, status_line="HTTP/1.1 503 Service Unavailable",
        server="Surge/5.0"))
    from browserwright.session import Session

    sess = Session.__new__(Session)
    err = sess._unreachable("ws://127.0.0.1:19990/control?client=skill-s1",
                            OSError(61, "Connection refused"))
    assert "Surge/5.0" in (err.fix or "")
    assert "restart" not in (err.fix or "")


def test_doctor_endpoint_check_uses_the_diagnosis(monkeypatch):
    from browserwright import health
    import browserwright.daemon_url as du

    monkeypatch.setattr(du, "daemon_endpoint",
                        lambda **_k: _endpoint("http://127.0.0.1:19990", "default"))
    monkeypatch.setattr(health, "_probe_tcp",
                        lambda host, port, timeout=1.5: "Connection refused")
    monkeypatch.setattr(du, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="foreign", host=h, port=p, status_line="HTTP/1.1 503 Service Unavailable",
        server="Surge/5.0"))
    (check,) = health._endpoint_reachability_checks()
    assert check["status"] == "fail"
    assert "Surge/5.0" in check["fix"]
    assert "restart" not in check["fix"]


# ---- 2. one recovery vocabulary ---------------------------------------------

BANNED = ("browserwright-daemon restart", "browserwright-daemon serve",
          "restart --force", "--force", "session end")

#: Human-only surfaces exempt from the rule (ADR-0013 rule 3): the daemon's
#: own startup complaints, the LaunchAgent/restart/install flows, `stop`.
_HUMAN_ONLY = re.compile(
    r"(def _cmd_(stop|restart|install|uninstall|serve)\b|launchagent|"
    r"restart_guard|_stale\.py|listener\.py|daemon_url\.unreachable_message)")


def _agent_visible_sources() -> list[Path]:
    import browserwright
    root = Path(browserwright.__file__).parent
    return [root / "errors.py", root / "health.py", root / "cdp.py",
            root / "session_create.py", root / "daemon_url.py",
            root / "session.py", root / "mode_b_client.py"]


def test_agent_visible_remediation_text_has_no_banned_words():
    """Greps the source of every module that builds an agent-facing error or
    doctor fix. A hit means a hint is again telling agents to restart/serve
    the daemon or end a session — the two moves that made 2026-09-01 worse."""
    offenders = []
    for path in _agent_visible_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # Only string-literal lines can reach an agent; comments and
            # docstring prose (no quote on the line) are explanation.
            if stripped.startswith("#") or '"' not in line:
                continue
            for word in BANNED:
                if word in line and not _HUMAN_ONLY.search(line):
                    offenders.append(f"{path.name}:{lineno}: {stripped[:100]}")
    # `unreachable_message` is the explicit-endpoint (remote) text and may
    # mention `--facade-host` for the operator; nothing else gets a pass.
    offenders = [o for o in offenders if "--facade-host" not in o]
    assert not offenders, "\n".join(offenders)


def test_skill_runtime_recovery_section_leads_and_bans_restart():
    import browserwright
    doc = (Path(browserwright.__file__).parent / "skill_runtime.md").read_text()
    assert "## When A Call Fails" in doc
    section = doc.split("## When A Call Fails", 1)[1].split("\n## ", 1)[0]
    assert "browserwright doctor" in section
    assert "session reset" in section
    assert "Never do these as an agent" in section
    # the old "restart the daemon" instruction is gone from the version section
    version = doc.split("## Version Discipline", 1)[1].split("\n## ", 1)[0]
    assert "`browserwright-daemon restart`" not in version


def test_default_error_fixes_have_no_banned_words():
    from browserwright import errors
    for name in dir(errors):
        cls = getattr(errors, name)
        fix = getattr(cls, "default_fix", None)
        if isinstance(fix, str):
            for word in BANNED:
                assert word not in fix, f"{name}.default_fix contains {word!r}"


def test_no_session_error_shows_reuse():
    from browserwright.errors import NoSession
    e = NoSession()
    assert "--reuse" in str(e)
    assert "--reuse" in e.fix


# ---- 3. session new --reuse -------------------------------------------------


@pytest.fixture
def ledger_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BS_HOME", str(tmp_path / "home"))
    from browserwright import session_create
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)
    return tmp_path


def test_session_new_reuse_returns_the_existing_id(ledger_home):
    from browserwright import session_create

    a = session_create.new(backend="extension", name="hn", reuse=True)
    assert session_create.last_new_reused is None
    b = session_create.new(backend="extension", name="hn", reuse=True)
    assert b == a
    assert session_create.last_new_reused == a


def test_session_new_without_reuse_allocates_every_time(ledger_home):
    from browserwright import session_create

    a = session_create.new(backend="extension", name="hn")
    b = session_create.new(backend="extension", name="hn")
    assert a != b
    assert session_create.last_new_reused is None


def test_session_new_reuse_matches_backend_and_name_only(ledger_home):
    from browserwright import session_create

    ext = session_create.new(backend="extension", name="hn")
    other = session_create.new(backend="extension", name="other", reuse=True)
    assert other != ext
    cdp = session_create.new(backend="cdp", name="hn", create=True, reuse=True)
    assert cdp != ext


def test_cli_session_new_reuse_says_so(ledger_home, capsys):
    from browserwright import cli

    assert cli._cmd_session(["new", "--backend=extension", "--name=hn"]) == 0
    first = capsys.readouterr().out.strip()
    assert cli._cmd_session(
        ["new", "--backend=extension", "--name=hn", "--reuse"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == first
    assert "reusing session" in out.err


# ---- ADR-0012 rule 5: log primitives ----------------------------------------


def test_already_running_is_collapsed_to_one_summary_per_minute(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from browserwright.daemon import _ipc

    t0 = 1_000_000.0
    first = _ipc.note_already_running(4207, now=t0)
    assert first == "browserwright-daemon already running (pid 4207)"
    for i in range(1, 20):
        assert _ipc.note_already_running(4207, now=t0 + i) is None
    summary = _ipc.note_already_running(4207, now=t0 + 61)
    assert summary is not None
    assert "19 further start attempt(s)" in summary
    assert "KeepAlive" in summary
    assert _ipc.note_already_running(4207, now=t0 + 62) is None


def test_lifecycle_line_is_timestamped_and_attributed(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    from browserwright.daemon import _ipc

    _ipc.log_lifecycle("restart", pid_before=1, initiator=_ipc.describe_initiator("restart"))
    text = _ipc.log_path().read_text()
    line = text.strip().splitlines()[-1]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} LIFECYCLE restart ", line)
    assert "pid_before=1" in line
    assert "initiator=cli:restart cwd=" in line
    assert "parent=" in line


def test_initiator_defaults_to_launchd_for_a_pid1_parent(monkeypatch):
    from browserwright.daemon import _ipc
    import os

    monkeypatch.delenv(_ipc.INITIATOR_ENV, raising=False)
    monkeypatch.setattr(os, "getppid", lambda: 1)
    assert _ipc.initiator_from_env() == "launchd"
    monkeypatch.setenv(_ipc.INITIATOR_ENV, "cli:auto-start cwd=/x parent='zsh'")
    assert _ipc.initiator_from_env().startswith("cli:auto-start")


def test_stderr_line_is_timestamped(capsys):
    from browserwright.daemon import _ipc
    _ipc.stderr_line("hello")
    err = capsys.readouterr().err
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} hello\n$", err)
