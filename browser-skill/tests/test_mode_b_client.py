"""Mode B daemon client tests.

We don't depend on a running ``browser-daemon serve`` here — these tests
exercise discovery, alive probing, URL construction, and the auto factory's
fallback. End-to-end live verification is in the live-test suite.
"""
import json
import os
import socket
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _short_tmp() -> Path:
    """Same trick as the repl protocol tests: macOS unix-socket path limit
    is 104 chars, so pytest's tmp_path is too long. Mint a short scratch dir."""
    import tempfile, uuid
    p = Path(tempfile.gettempdir()) / f"bs-mb-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_discover_unix_via_status_json(tmp_path, monkeypatch):
    from browser_skill.mode_b_client import ModeBClient

    fake_status = {"alive": True, "transport": "unix",
                   "path": "/tmp/browser-daemon-default.sock"}

    class _FakeProc:
        returncode = 0
        stdout = json.dumps(fake_status)

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_FakeProc()):
        ep = ModeBClient().discover()
    assert ep["transport"] == "unix"
    assert ep["path"] == "/tmp/browser-daemon-default.sock"


def test_discover_unix_via_path_fallback(monkeypatch):
    """If `status --json` fails but the well-known socket file exists, we use
    it directly."""
    from browser_skill.mode_b_client import ModeBClient

    short = _short_tmp()
    sock_path = short / "browser-daemon-foo.sock"
    sock_path.write_text("")  # daemon would normally bind here

    class _FailProc:
        returncode = 1
        stdout = ""

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_FailProc()):
        client = ModeBClient(name="foo")
        ep = client.discover()
    assert ep["transport"] == "unix"
    assert ep["path"] == str(sock_path)
    shutil.rmtree(short, ignore_errors=True)


def test_is_alive_pings_socket(monkeypatch):
    """Stand up a tiny unix socket server, point the client at it, and verify
    is_alive() returns True."""
    from browser_skill.mode_b_client import ModeBClient

    short = _short_tmp()
    sock_path = short / "test.sock"

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def accept_loop():
        try:
            c, _ = srv.accept()
            c.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    monkeypatch.setenv("BD_NAME", "x")

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_FailProc()):
        # ``status --json`` fails → falls back to socket path inspection
        # → file exists → ping succeeds because we're listening.
        sock_target = short / "browser-daemon-x.sock"
        sock_target.write_text("")
        # Move the listening socket to that path (re-bind).
        srv.close()
        try:
            sock_target.unlink()
        except OSError:
            pass
        srv2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv2.bind(str(sock_target))
        srv2.listen(1)
        threading.Thread(target=lambda: (srv2.accept(), None), daemon=True).start()
        client = ModeBClient(name="x")
        assert client.is_alive() is True
        srv2.close()
    shutil.rmtree(short, ignore_errors=True)


def test_is_alive_false_when_no_endpoint(monkeypatch):
    from browser_skill.mode_b_client import ModeBClient

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/dir")
    monkeypatch.setenv("BD_NAME", "ghost")

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_FailProc()):
        assert ModeBClient(name="ghost").is_alive() is False


def test_ws_url_unix_format(monkeypatch):
    from browser_skill.mode_b_client import ModeBClient

    fake_status = {"alive": True, "transport": "unix",
                   "path": "/run/user/1000/browser-daemon-default.sock"}

    class _OK:
        returncode = 0
        stdout = json.dumps(fake_status)

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_OK()):
        url = ModeBClient().ws_url(client_label="skill-repl")
    assert url.startswith("ws+unix:///run/user/1000/")
    assert "?client=skill-repl" in url


def test_ws_url_tcp_format(monkeypatch):
    from browser_skill.mode_b_client import ModeBClient

    fake_status = {"alive": True, "transport": "tcp", "host": "127.0.0.1",
                   "port": 8541, "token": "abcd"}

    class _OK:
        returncode = 0
        stdout = json.dumps(fake_status)

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_OK()):
        url = ModeBClient().ws_url()
    assert url == "ws://127.0.0.1:8541?token=abcd&client=skill-repl"


def test_auto_falls_back_to_mode_a(monkeypatch):
    """When the Mode B socket isn't reachable, ``auto_client`` returns a
    Mode A ``DaemonClient``."""
    from browser_skill.daemon_client import DaemonClient as ModeAClient
    from browser_skill.mode_b_client import auto_client

    monkeypatch.setenv("BD_NAME", "absent")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent")
    monkeypatch.delenv("BS_DAEMON_MODE", raising=False)

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_FailProc()):
        client = auto_client()
    assert isinstance(client, ModeAClient)


def test_auto_force_b_raises_when_absent(monkeypatch):
    from browser_skill.errors import DaemonUnavailable
    from browser_skill.mode_b_client import auto_client

    monkeypatch.setenv("BD_NAME", "absent2")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent")
    monkeypatch.setenv("BS_DAEMON_MODE", "B")

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browser_skill.mode_b_client.subprocess.run", return_value=_FailProc()):
        with pytest.raises(DaemonUnavailable):
            auto_client()


def test_force_a_env_picks_mode_a(monkeypatch):
    from browser_skill.daemon_client import DaemonClient as ModeAClient
    from browser_skill.mode_b_client import auto_client

    monkeypatch.setenv("BS_DAEMON_MODE", "A")
    client = auto_client()
    assert isinstance(client, ModeAClient)


def test_client_uses_session_daemon_endpoint(tmp_bs_home, monkeypatch):
    """P1: the endpoint a call talks to comes from the session record, not a
    frozen module-level ``"default"``."""
    from browser_skill import mode_b_client
    from browser_skill import session_registry as reg

    sid = reg.allocate(backend="rdp", daemon_endpoint="browser-daemon-s7.sock",
                       owner="create")
    c = mode_b_client.client_for_session(reg.get(sid))
    assert c.name == "browser-daemon-s7.sock"  # not "default"


def test_default_name_is_not_frozen_at_import(monkeypatch):
    """BD_NAME is read live, so changing it after import takes effect."""
    from browser_skill import mode_b_client

    monkeypatch.setenv("BD_NAME", "live-changed")
    assert mode_b_client.ModeBClient().name == "live-changed"


def test_session_built_from_record_uses_endpoint(tmp_bs_home):
    """Threading a record into Session picks the record's daemon endpoint."""
    from browser_skill import session_registry as reg
    from browser_skill.session import Session

    sid = reg.allocate(backend="rdp", daemon_endpoint="browser-daemon-s9.sock",
                       owner="create")
    sess = Session(record=reg.get(sid))
    assert sess.daemon.name == "browser-daemon-s9.sock"
