"""Mode B daemon client tests.

We don't depend on a running ``browserwright-daemon serve`` here — these tests
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
    from browserwright.mode_b_client import ModeBClient

    fake_status = {"alive": True, "transport": "unix",
                   "path": "/tmp/browserwright-daemon-default.sock"}

    class _FakeProc:
        returncode = 0
        stdout = json.dumps(fake_status)

    with patch("browserwright.mode_b_client.subprocess.run", return_value=_FakeProc()):
        ep = ModeBClient().discover()
    assert ep["transport"] == "unix"
    assert ep["path"] == "/tmp/browserwright-daemon-default.sock"


def test_discover_unix_via_path_fallback(monkeypatch):
    """If `status --json` fails but the well-known socket file exists, we use
    it directly."""
    from browserwright.mode_b_client import ModeBClient

    short = _short_tmp()
    # Single-global-daemon: the socket is a FIXED name under XDG_RUNTIME_DIR.
    sock_path = short / "browserwright-daemon.sock"
    sock_path.write_text("")  # daemon would normally bind here

    class _FailProc:
        returncode = 1
        stdout = ""

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    with patch("browserwright.mode_b_client.subprocess.run", return_value=_FailProc()):
        client = ModeBClient()
        ep = client.discover()
    assert ep["transport"] == "unix"
    assert ep["path"] == str(sock_path)
    shutil.rmtree(short, ignore_errors=True)


def test_is_alive_pings_socket(monkeypatch):
    """Stand up a tiny unix socket server, point the client at it, and verify
    is_alive() returns True."""
    from browserwright.mode_b_client import ModeBClient

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

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browserwright.mode_b_client.subprocess.run", return_value=_FailProc()):
        # ``status --json`` fails → falls back to the FIXED socket path
        # → file exists → ping succeeds because we're listening.
        sock_target = short / "browserwright-daemon.sock"
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
        client = ModeBClient()
        assert client.is_alive() is True
        srv2.close()
    shutil.rmtree(short, ignore_errors=True)


def test_is_alive_false_when_no_endpoint(monkeypatch):
    from browserwright.mode_b_client import ModeBClient

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/dir")

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browserwright.mode_b_client.subprocess.run", return_value=_FailProc()):
        assert ModeBClient().is_alive() is False


def test_ws_url_unix_format(monkeypatch):
    from browserwright.mode_b_client import ModeBClient

    fake_status = {"alive": True, "transport": "unix",
                   "path": "/run/user/1000/browserwright-daemon-default.sock"}

    class _OK:
        returncode = 0
        stdout = json.dumps(fake_status)

    with patch("browserwright.mode_b_client.subprocess.run", return_value=_OK()):
        url = ModeBClient().ws_url(client_label="skill-repl")
    assert url.startswith("ws+unix:///run/user/1000/")
    assert "?client=skill-repl" in url


def test_ws_url_tcp_format(monkeypatch):
    from browserwright.mode_b_client import ModeBClient

    fake_status = {"alive": True, "transport": "tcp", "host": "127.0.0.1",
                   "port": 8541, "token": "abcd"}

    class _OK:
        returncode = 0
        stdout = json.dumps(fake_status)

    with patch("browserwright.mode_b_client.subprocess.run", return_value=_OK()):
        url = ModeBClient().ws_url()
    assert url == "ws://127.0.0.1:8541?token=abcd&client=skill-repl"


def test_client_is_lazy_when_socket_absent(monkeypatch):
    """Mode A was removed (and so was ``auto_client``): constructing a
    ``ModeBClient`` with no reachable daemon socket still stays offline — it
    builds fine — and ``DaemonUnavailable`` surfaces only when the ws is
    actually resolved."""
    from browserwright.errors import DaemonUnavailable
    from browserwright.mode_b_client import ModeBClient

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent")

    class _FailProc:
        returncode = 1
        stdout = ""

    with patch("browserwright.mode_b_client.subprocess.run", return_value=_FailProc()):
        client = ModeBClient()
        assert isinstance(client, ModeBClient)
        assert client.is_alive() is False
        # Lazy: the error is deferred to first use, not construction.
        with pytest.raises(DaemonUnavailable):
            client.resolve_ws_url()


def test_client_for_session_uses_session_label(tmp_bs_home, monkeypatch):
    """Single-global-daemon: there is no per-session endpoint anymore. A
    client built for a session talks to the ONE fixed socket and carries the
    session identity only as its client label (skill-s<id>) for daemon-side
    routing/observability."""
    from browserwright import mode_b_client
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="rdp", owner="create")
    c = mode_b_client.client_for_session(reg.get(sid))
    assert isinstance(c, mode_b_client.ModeBClient)
    assert c._client_label == f"skill-s{sid}"


def test_session_built_from_record_targets_fixed_socket(tmp_bs_home, monkeypatch):
    """Threading a record into Session yields a client for the single global
    daemon's fixed socket, labelled with the session id."""
    from browserwright import session_registry as reg
    from browserwright.session import Session

    short = _short_tmp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    # Lay down the fixed socket file so discover()'s path fallback resolves it.
    (short / "browserwright-daemon.sock").write_text("")
    sid = reg.allocate(backend="rdp", owner="create")

    class _FailProc:
        returncode = 1
        stdout = ""

    sess = Session(record=reg.get(sid))
    assert sess.daemon._client_label == f"skill-s{sid}"
    # The endpoint, once discovered, is the fixed socket under XDG_RUNTIME_DIR.
    with patch("browserwright.mode_b_client.subprocess.run", return_value=_FailProc()):
        assert sess.daemon.discover()["path"].endswith("browserwright-daemon.sock")
    shutil.rmtree(short, ignore_errors=True)
