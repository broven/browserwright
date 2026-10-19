"""The explicit verbs may stop a daemon that stopped answering.

The asymmetry this closes: `stop` and `restart` refused to signal anything they
could not ping (`_ipc.ping_sync`, a live daemon at the resolved endpoint is
mandatory), so a genuinely wedged daemon could not be stopped by any CLI verb —
while `serve`'s *implicit* reclaim SIGTERMed on the weakest possible evidence and
killed a live one. The end state is the other way round: the implicit path is the
strict one (escalating evidence, `listener._reclaim_decision`), and the explicit
path may act on **already-confirmed identity**.

Confirmed identity, unchanged and in full, is `_stale.unresponsive_daemon`: a
pid lsof names as holding the endpoint's own TCP port, whose command line is a
browserwright daemon, and sharing our runtime dir. "Never signal a process we
cannot prove is browserwright" stays absolute — these tests pin both halves.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from browserwright.daemon import _ipc, _stale, cli, launchagent, supervise
from browserwright.daemon.config import Config


# ---- the identity lookup ---------------------------------------------------


def test_unresponsive_daemon_needs_both_proofs(monkeypatch):
    """A proven browserwright holder on our runtime dir. Miss either → None."""
    monkeypatch.setattr(_stale, "port_holder_pids", lambda port: [999])
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    assert _stale.unresponsive_daemon(39990) == 999

    # lsof/cmdline cannot name a browserwright daemon → never a kill target.
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: False)
    assert _stale.unresponsive_daemon(39990) is None

    # A browserwright daemon from ANOTHER runtime dir is someone else's live
    # daemon (the machine-global one) — the issue #44 B rule, unchanged.
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: False)
    assert _stale.unresponsive_daemon(39990) is None


def test_unresponsive_daemon_skips_itself_and_looks_only_at_the_endpoint_port(
        monkeypatch):
    """The lookup asks about ONE port — the endpoint that should have answered —
    so a stop aimed at one daemon can never nominate the holder of another's."""
    seen = []

    def port_holder_pids(port):
        seen.append(port)
        return [os.getpid()]

    monkeypatch.setattr(_stale, "port_holder_pids", port_holder_pids)
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    monkeypatch.setattr(_stale, "same_runtime_dir_as_us", lambda pid: True)

    assert _stale.unresponsive_daemon(39990) is None
    assert seen == [39990]


# ---- `stop` ----------------------------------------------------------------


@pytest.fixture
def stopped(monkeypatch):
    """Stub the signalling layer; return the pids `terminate` was called with."""
    signalled = []
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda: None)
    monkeypatch.setattr(_ipc, "log_lifecycle",
                        lambda event, **fields: signalled.append((event, fields)))
    monkeypatch.setattr("browserwright.daemon.platforms.proc_start_time",
                        lambda pid: 111)

    def fake_terminate(pid, **kwargs):
        signalled.append(pid)
        return supervise.Outcome.EXITED

    monkeypatch.setattr(supervise, "terminate", fake_terminate)
    return signalled


def test_stop_signals_a_confirmed_wedged_daemon(monkeypatch, stopped, tmp_path):
    """Nothing answers the ping, but our ports are provably held by a
    browserwright daemon on our runtime dir → stop it."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_ipc, "pid_path", lambda: tmp_path / "browserwright-daemon.pid")
    (tmp_path / "browserwright-daemon.pid").write_text("4242\n")
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    monkeypatch.setattr(_stale, "port_holder_pids", lambda port: [4242])
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    monkeypatch.setattr(_stale, "pid_env", lambda pid, name: str(tmp_path))

    # An unmodified Config: its facade port is unset, so ADR-0012 rule 6's
    # port/endpoint guard does not fire and this test is about the lookup.
    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0

    assert 4242 in stopped
    events = [a for a in stopped if isinstance(a, tuple)]
    assert events and events[0][0] == "stop"
    assert events[0][1]["probes"] == "unresponsive"


def test_stop_still_refuses_a_stranger_holding_the_port(monkeypatch, stopped,
                                                        tmp_path, capsys):
    """A port holder that is not a browserwright daemon is never signalled — the
    explicit path does not get to skip the identity proof."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_ipc, "pid_path", lambda: tmp_path / "browserwright-daemon.pid")
    (tmp_path / "browserwright-daemon.pid").write_text("4242\n")
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    monkeypatch.setattr(_stale, "port_holder_pids", lambda port: [4242])
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: False)

    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0

    assert stopped == []  # nothing signalled; only the "no live daemon" path
    assert "no live daemon" in capsys.readouterr().err


def test_stop_still_refuses_a_daemon_from_another_runtime_dir(
        monkeypatch, stopped, tmp_path, capsys):
    """The machine-global daemon's case: provably browserwright, holds our
    ports, but lives under /tmp. Never signal it (issue #44 B)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_ipc, "pid_path", lambda: tmp_path / "browserwright-daemon.pid")
    (tmp_path / "browserwright-daemon.pid").write_text("4242\n")
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    monkeypatch.setattr(_stale, "port_holder_pids", lambda port: [4242])
    monkeypatch.setattr(_stale, "pid_is_browserwright_daemon", lambda pid: True)
    monkeypatch.setattr(_stale, "pid_env", lambda pid, name: "/tmp")

    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0

    assert stopped == []
    assert "no live daemon" in capsys.readouterr().err


def test_stop_wedged_death_is_measured_by_the_process_table(
        monkeypatch, tmp_path):
    """A daemon that was already not answering cannot prove death by ping."""
    seen = {}

    def fake_terminate(pid, **kwargs):
        seen["is_dead"] = kwargs["is_dead"]
        seen["guard"] = kwargs["guard"]
        return supervise.Outcome.EXITED

    monkeypatch.setattr(supervise, "terminate", fake_terminate)
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda: None)
    monkeypatch.setattr(_ipc, "log_lifecycle", lambda event, **fields: None)
    monkeypatch.setattr("browserwright.daemon.platforms.proc_start_time",
                        lambda pid: 111)
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    monkeypatch.setattr(_stale, "unresponsive_daemon", lambda port: 4242)
    alive = {"v": True}
    monkeypatch.setattr(_stale, "pid_alive", lambda pid: alive["v"])

    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0

    assert seen["is_dead"]() is False
    alive["v"] = False
    assert seen["is_dead"]() is True
    # The pid-reuse guard stays armed: this pid is not one we own.
    assert seen["guard"]() is True


# ---- `restart`'s incumbent stop -------------------------------------------


def test_restart_stops_a_confirmed_wedged_incumbent(monkeypatch):
    signalled = []
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    monkeypatch.setattr(_stale, "unresponsive_daemon", lambda port: 4242)
    monkeypatch.setattr(_stale, "pid_alive", lambda pid: False)
    monkeypatch.setattr("browserwright.daemon.platforms.proc_start_time",
                        lambda pid: 111)
    monkeypatch.setattr(supervise, "terminate",
                        lambda pid, **kw: signalled.append(pid)
                        or supervise.Outcome.EXITED)

    assert launchagent._stop_incumbent(0.0) == {"stopped": 4242}
    assert signalled == [4242]


def test_restart_still_signals_nothing_it_cannot_identify(monkeypatch):
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    monkeypatch.setattr(_stale, "unresponsive_daemon", lambda port: None)
    monkeypatch.setattr(supervise, "terminate",
                        lambda pid, **kw: pytest.fail("must not signal"))

    assert launchagent._stop_incumbent(0.0) == {"stopped": None}


def test_restart_still_refuses_a_non_local_endpoint(monkeypatch):
    """ADR-0011: a remote endpoint's pid is a pid over there, whoever holds it."""
    monkeypatch.setenv("BW_DAEMON_URL", "http://100.72.20.32:19990")
    monkeypatch.setattr(_stale, "unresponsive_daemon",
                        lambda port: pytest.fail("must not look for a pid"))
    with pytest.raises(launchagent.LaunchAgentError) as ei:
        launchagent._stop_incumbent(0.0)
    assert ei.value.exit_code == 3


def test_stop_never_touches_the_lookup_for_a_healthy_daemon(monkeypatch):
    """The ordinary path is untouched: a pong pinpoints the pid, and the port
    lookup is never even consulted."""
    signalled = []
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: 4242)
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda: None)
    monkeypatch.setattr(_ipc, "log_lifecycle", lambda event, **fields: None)
    monkeypatch.setattr("browserwright.daemon.platforms.proc_start_time",
                        lambda pid: os.getpid())
    monkeypatch.setattr(_stale, "unresponsive_daemon",
                        lambda port: pytest.fail("ping answered; no lookup"))
    monkeypatch.setattr(supervise, "terminate",
                        lambda pid, **kw: signalled.append(pid)
                        or supervise.Outcome.EXITED)

    assert cli._cmd_stop(SimpleNamespace(timeout=0.0), Config()) == 0
    assert signalled == [4242]
