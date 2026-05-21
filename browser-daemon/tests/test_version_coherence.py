"""S6 (A2-a): the running daemon must advertise its build/package version so a
client can detect a stale daemon (running an older version than the installed
code) and auto-restart it.

The version is carried on the `/__ping__` pong body — the same probe every
liveness check already issues — and surfaced through `status --json`. These
tests pin only the *seam*: the pong body carries a version, the parser reads it
back, and `status --json` includes it. No live browser/daemon.
"""
import json

from browser_daemon import _ipc, __version__


# ---- pong body carries the version ----------------------------------------

def test_pong_body_includes_version():
    body = _ipc.make_pong_body(4242)
    payload = json.loads(body.decode("utf-8"))
    assert payload["pong"] is True
    assert payload["pid"] == 4242
    # The running daemon advertises its package version on every ping.
    assert payload["version"] == __version__


def test_parse_pong_extracts_version():
    """The client-side pong parser must surface the advertised version, not
    just the pid, so a coherence check has something to compare."""
    body = _ipc.make_pong_body(1234)
    pid, version = _ipc.parse_pong(body)
    assert pid == 1234
    assert version == __version__


def test_parse_pong_missing_version_is_none():
    """A daemon old enough to predate version-advertising returns a pong with
    no version field. The parser must report version=None (treated as stale by
    the client) rather than crashing."""
    legacy = json.dumps({"pong": True, "pid": 99}).encode()
    pid, version = _ipc.parse_pong(legacy)
    assert pid == 99
    assert version is None


def test_parse_pong_rejects_garbage():
    pid, version = _ipc.parse_pong(b"not json at all")
    assert pid is None
    assert version is None


# ---- ping_sync stays backward compatible ----------------------------------

def test_ping_sync_still_returns_pid_only(monkeypatch):
    """Existing callers of ping_sync expect a bare pid. Adding version must not
    change that contract."""
    monkeypatch.setattr(
        _ipc, "ping_status_sync", lambda name, timeout=1.0: (321, "9.9.9"))
    assert _ipc.ping_sync("default", timeout=0.1) == 321


# ---- status --json surfaces the running daemon's version ------------------

class _StatusArgs:
    json = True


def test_status_json_includes_running_version(monkeypatch, capsys):
    from browser_daemon import cli
    from browser_daemon.config import Config

    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda name, timeout=1.0: (777, "1.2.3"))
    monkeypatch.setattr(
        _ipc, "endpoint_describe",
        lambda name: {"transport": "unix", "path": "/tmp/x.sock"})

    rc = cli._cmd_status(_StatusArgs(), Config(name="default"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is True
    assert out["pid"] == 777
    assert out["version"] == "1.2.3"


def test_status_json_version_none_when_dead(monkeypatch, capsys):
    from browser_daemon import cli
    from browser_daemon.config import Config

    monkeypatch.setattr(
        _ipc, "ping_status_sync", lambda name, timeout=1.0: (None, None))
    monkeypatch.setattr(
        _ipc, "endpoint_describe",
        lambda name: {"transport": "unix", "path": "/tmp/x.sock"})

    rc = cli._cmd_status(_StatusArgs(), Config(name="default"))
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is False
    assert out["version"] is None
