"""IPC plumbing — socket / port file / ping / cleanup.

This is the layer that has to mirror browser-harness `_ipc.py` exactly,
because the patterns there were field-tested. We test the contract, not
the implementation.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from browser_daemon import _ipc
from browser_daemon.errors import UserError


# ---- name validation -------------------------------------------------------


@pytest.mark.parametrize("name", ["default", "v0_2", "test-1", "A-B-C", "abc123"])
def test_valid_name_accepted(name):
    assert _ipc.check_name(name) == name


@pytest.mark.parametrize("name", [
    "", "with space", "../escape", "a/b", "x.y", "a" * 65, "セキュア",
])
def test_invalid_name_rejected(name):
    with pytest.raises(UserError):
        _ipc.check_name(name)


# ---- path layout -----------------------------------------------------------


def test_sock_path_under_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    p = _ipc.sock_path("default")
    assert str(p).startswith(str(tmp_path))
    assert "browser-daemon-default.sock" in str(p)


def test_log_path_under_tmpdir(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    p = _ipc.log_path("v02")
    assert str(p).startswith(str(tmp_path))
    assert p.name == "browser-daemon-v02.log"


# ---- port file (Windows path tested on all platforms) ---------------------


def test_write_and_read_port_file_atomic(monkeypatch, tmp_path):
    """Even on POSIX, the Windows code path's atomic write is testable —
    we just write+read against the same name."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _ipc.write_port_file("portfile-test", 9999, "abc123def")
    port, token = _ipc.read_port_file("portfile-test")
    assert port == 9999
    assert token == "abc123def"


def test_read_port_file_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    port, token = _ipc.read_port_file("never-written")
    assert port is None and token is None


def test_read_port_file_corrupt_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    pf = _ipc.port_path("corrupt")
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("{this is not json")
    port, token = _ipc.read_port_file("corrupt")
    assert port is None and token is None


# ---- POSIX socket bind perms ----------------------------------------------


@pytest.fixture
def short_runtime(monkeypatch):
    """AF_UNIX sun_path on macOS has a 104-byte budget. pytest's tmp_path
    lives under /private/var/folders/... which routinely blows that. Tests
    that bind a real socket need a short path."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="bd-t-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    monkeypatch.setenv("TMPDIR", str(d))
    yield d
    # Best-effort cleanup
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_make_unix_socket_sets_owner_only_perms(short_runtime):
    """Spec §6.2: socket file must NOT be world/group accessible. We accept
    either 0600 or 0700 (asyncio chmod's the file after bind on some
    platforms — spec asks for 0600 but 0700 is also owner-only and safe).
    """
    sock = _ipc.make_unix_socket("perm-test")
    try:
        mode = os.stat(_ipc.sock_path("perm-test")).st_mode & 0o777
        # Must be owner-only.
        assert mode & 0o077 == 0, f"socket world/group accessible: {oct(mode)}"
        assert mode & 0o600 == 0o600, f"owner read/write missing: {oct(mode)}"
    finally:
        sock.close()
        _ipc.cleanup_endpoint("perm-test")


# ---- cleanup --------------------------------------------------------------


def test_cleanup_removes_socket_and_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_p = _ipc.sock_path("cleanup-test")
    pid_p = _ipc.pid_path("cleanup-test")
    sock_p.parent.mkdir(parents=True, exist_ok=True)
    sock_p.write_text("dummy")
    pid_p.write_text("12345")
    _ipc.cleanup_endpoint("cleanup-test")
    assert not sock_p.exists()
    assert not pid_p.exists()


def test_cleanup_silent_on_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # No-op — must not raise.
    _ipc.cleanup_endpoint("never-existed")


# ---- ping handshake -------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_no_endpoint_returns_none(monkeypatch, tmp_path):
    """spec §6.7: stale-detection ping must not falsely report alive when
    nothing's listening."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    pid = await _ipc.ping_async("nobody-home", timeout=0.5)
    assert pid is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
async def test_ping_wrong_listener_returns_none(short_runtime):
    """A foreign listener on the socket path must not be mistaken for our
    daemon. We bind a raw socket that doesn't speak HTTP and ping it."""
    s = _ipc.make_unix_socket("foreign")
    try:
        # raw accept loop, send nothing back
        loop = asyncio.get_running_loop()

        async def accept_silent():
            srv_sock = s
            srv_sock.setblocking(False)
            try:
                while True:
                    try:
                        c, _ = await loop.sock_accept(srv_sock)
                        c.close()
                    except asyncio.CancelledError:
                        return
            except OSError:
                return

        task = asyncio.create_task(accept_silent())
        pid = await _ipc.ping_async("foreign", timeout=0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        s.close()
        _ipc.cleanup_endpoint("foreign")
    assert pid is None
