"""Local, destructive daemon operations must refuse a non-local endpoint.

ADR-0011 replaced a unix socket in our own runtime dir with a URL. That quietly
broke an invariant two code paths were built on: "the pid the endpoint reports
is a pid on this machine". `stop` and `restart` work by pinging for a pid and
signalling it, and their PID-reuse guard only consults the LOCAL process table —
so against a remote daemon the guard would *confirm* an unrelated local process
holding that number and then SIGTERM/SIGKILL it.

The discriminator is the endpoint's **source**, not its host. A daemon started
here and bound to a tailnet IP (`--facade-host`, the documented remote-exposure
setup) publishes that non-loopback host into our runtime dir — it is still our
process, and stopping it from its own machine must keep working. Only an
endpoint someone *named* can point at another machine.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from browserwright.daemon import cli, launchagent
from browserwright.daemon.config import Config


class _Signalled(AssertionError):
    """Raised if anything reaches the signalling layer at all."""


@pytest.fixture
def tripwire(monkeypatch):
    """Fail loudly if a refused path still pings for or signals a pid."""
    from browserwright.daemon import _ipc, supervise

    def _boom(*a, **k):
        raise _Signalled("a non-local endpoint reached the signalling layer")

    monkeypatch.setattr(_ipc, "ping_sync", _boom)
    monkeypatch.setattr(supervise, "terminate", _boom)
    monkeypatch.setattr(_ipc, "cleanup_endpoint", _boom)


@pytest.mark.parametrize("url", [
    "http://100.72.20.32:19990",      # a tailnet peer
    "http://box.example:19990",       # a hostname
])
def test_stop_refuses_a_non_local_endpoint(monkeypatch, capsys, tripwire, url):
    monkeypatch.setenv("BW_DAEMON_URL", url)

    rc = cli._cmd_stop(SimpleNamespace(timeout=0.0), Config())

    assert rc == 3
    err = capsys.readouterr().err
    assert url in err
    assert "not this machine" in err
    # Actionable: says where to run it instead.
    assert "on the machine that serves" in err


def test_restart_refuses_a_non_local_endpoint(monkeypatch, tripwire):
    monkeypatch.setenv("BW_DAEMON_URL", "http://100.72.20.32:19990")

    with pytest.raises(launchagent.LaunchAgentError) as ei:
        launchagent._stop_incumbent(timeout=0.0)
    assert ei.value.exit_code == 3
    assert "100.72.20.32" in str(ei.value)


def _stoppable(monkeypatch):
    """Stub the signalling layer and return the list it records pids into."""
    from browserwright.daemon import _ipc, platforms, supervise

    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: 4242)
    monkeypatch.setattr(platforms, "proc_start_time", lambda pid: 111)
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda: None)
    signalled = []
    monkeypatch.setattr(
        supervise, "terminate",
        lambda pid, **k: signalled.append(pid) or supervise.Outcome.EXITED)
    return signalled


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:19990",
    "http://localhost:19990",
    "http://[::1]:19990",
])
def test_an_explicit_loopback_endpoint_is_still_stoppable(monkeypatch, url):
    """An operator who pins a loopback URL is still naming a process on this
    machine, so its pid is meaningful and `stop` must keep working."""
    monkeypatch.setenv("BW_DAEMON_URL", url)
    signalled = _stoppable(monkeypatch)

    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0
    assert signalled == [4242]


@pytest.mark.parametrize("host", [
    "100.72.20.32",   # a tailnet IP, per the documented remote-exposure setup
    "0.0.0.0",        # all interfaces
    "192.168.1.20",   # a LAN IP
])
def test_a_local_daemon_bound_off_loopback_is_still_stoppable(
        monkeypatch, tmp_path, host):
    """`--facade-host <non-loopback>` is a supported, documented bind.

    The daemon publishes that host into OUR runtime dir, so the endpoint is
    state-file-sourced and the process is ours — `stop` on its own machine must
    not refuse. Note it genuinely is not reachable on 127.0.0.1 when bound to a
    specific IP, so "publish loopback instead" would be a lie, not a fix.
    """
    import json

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.delenv("BD_CONFIG", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": f"http://{host}:19990", "pid": 4242}))

    from browserwright.daemon_url import daemon_endpoint

    endpoint = daemon_endpoint()
    assert endpoint.source == "state_file"
    assert endpoint.is_loopback is False
    assert endpoint.is_locally_signalable is True

    signalled = _stoppable(monkeypatch)
    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0
    assert signalled == [4242]


def test_restart_allows_a_local_daemon_bound_off_loopback(monkeypatch, tmp_path):
    import json

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.delenv("BD_CONFIG", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": "http://100.72.20.32:19990", "pid": 4242}))

    signalled = _stoppable(monkeypatch)
    assert launchagent._stop_incumbent(timeout=0.0) == {"stopped": 4242}
    assert signalled == [4242]


def test_an_explicit_toml_endpoint_is_refused_too(monkeypatch, tmp_path,
                                                  capsys, tripwire):
    """`daemon_url` in the toml is an explicit source like the flag and env."""
    from browserwright import daemon_url as du

    cfg = tmp_path / "config.toml"
    cfg.write_text('daemon_url = "http://box.tailnet:19990"\n')
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setenv("BD_CONFIG", str(cfg))
    monkeypatch.setattr(du, "_cli_config_path", None)

    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 3
    assert "box.tailnet" in capsys.readouterr().err
