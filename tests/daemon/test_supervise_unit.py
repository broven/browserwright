"""Unit coverage for `daemon/supervise.py` — the one graceful→forced kill loop.

`stop` and `_stale.reclaim_ports` both route through `terminate()` now, so its
outcome taxonomy is load-bearing: `stop` prints a different line per outcome and
`reclaim_ports` maps them to its True/False contract. These tests pin the
taxonomy directly rather than through either caller.

No real process is signalled — `os.kill` is stubbed throughout.
"""
from __future__ import annotations

import signal

import pytest

from browserwright.daemon import supervise
from browserwright.daemon.supervise import Outcome


@pytest.fixture
def killed(monkeypatch):
    """Record every signal `terminate` sends instead of sending it."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(supervise.time, "sleep", lambda s: None)
    return sent


def _dies_after(n_polls: int):
    """A death predicate that reports 'dead' from the nth poll onwards."""
    seen = {"n": 0}

    def is_dead() -> bool:
        seen["n"] += 1
        return seen["n"] > n_polls
    return is_dead


# ---- wait_until -------------------------------------------------------------


def test_wait_until_checks_once_even_when_timeout_is_zero():
    """A zero/short timeout must still get a real answer, not an automatic
    False — `stop --timeout 0` depends on this."""
    calls = []
    assert supervise.wait_until(lambda: calls.append(1) or True, 0) is True
    assert calls == [1]


def test_wait_until_gives_up_and_reports_the_last_answer(monkeypatch):
    monkeypatch.setattr(supervise.time, "sleep", lambda s: None)
    assert supervise.wait_until(lambda: False, 0.05, interval=0.01) is False


# ---- pid_alive --------------------------------------------------------------


def test_pid_alive_rejects_nonpositive_pids():
    # 0 and negatives mean "process group" to kill(2); asking is a caller bug.
    assert supervise.pid_alive(0) is False
    assert supervise.pid_alive(-1) is False


def test_pid_alive_counts_not_ours_as_alive(monkeypatch):
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert supervise.pid_alive(1) is True


def test_pid_alive_reports_gone(monkeypatch):
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert supervise.pid_alive(4242) is False


# ---- terminate --------------------------------------------------------------


def test_terminate_stops_at_sigterm_when_the_process_goes_away(killed):
    out = supervise.terminate(99, is_dead=lambda: True, grace=1.0)
    assert out is Outcome.EXITED
    assert killed == [(99, signal.SIGTERM)]


def test_terminate_escalates_to_sigkill(killed):
    # Survives the grace poll, dies once the SIGKILL lands.
    out = supervise.terminate(99, is_dead=_dies_after(1), grace=0)
    assert out is Outcome.KILLED
    assert killed == [(99, signal.SIGTERM), (99, signal.SIGKILL)]


def test_terminate_reports_alive_when_sigkill_does_not_take(killed):
    out = supervise.terminate(99, is_dead=lambda: False, grace=0.02,
                              kill_grace=0.02, interval=0.01)
    assert out is Outcome.ALIVE
    assert killed == [(99, signal.SIGTERM), (99, signal.SIGKILL)]


def test_terminate_skips_the_confirmation_poll_when_kill_grace_is_zero(killed):
    """`stop` passes kill_grace=0: it exits right after the SIGKILL, so an extra
    round of pings would only add latency."""
    polls = []

    out = supervise.terminate(99, is_dead=lambda: polls.append(1) or False,
                              grace=0, kill_grace=0)
    assert out is Outcome.KILLED
    assert killed == [(99, signal.SIGTERM), (99, signal.SIGKILL)]
    assert len(polls) == 1  # the grace check only; nothing after the SIGKILL


def test_terminate_reports_already_gone(monkeypatch):
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert supervise.terminate(99, is_dead=lambda: True,
                               grace=1.0) is Outcome.ALREADY_GONE


def test_terminate_reports_signal_failure(monkeypatch):
    monkeypatch.setattr(supervise.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert supervise.terminate(99, is_dead=lambda: True,
                               grace=1.0) is Outcome.SIGNAL_FAILED


def test_terminate_refuses_a_recycled_pid_before_signalling(killed):
    out = supervise.terminate(99, is_dead=lambda: True, grace=1.0,
                              guard=lambda: False)
    assert out is Outcome.REFUSED
    assert killed == []  # never signalled a stranger


def test_terminate_withholds_sigkill_when_the_pid_is_recycled_mid_wait(killed):
    """The pid-reuse window that makes `stop`'s guard necessary: the daemon dies
    during the grace wait and the OS hands its pid to something else before we
    escalate. The SIGKILL must not go out."""
    answers = iter([True, False])

    out = supervise.terminate(99, is_dead=lambda: False, grace=0,
                              guard=lambda: next(answers))
    assert out is Outcome.REFUSED_ESCALATION
    assert killed == [(99, signal.SIGTERM)]
