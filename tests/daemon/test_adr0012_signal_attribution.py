"""ADR-0012 rule 5 for the one stop no verb can record: a signal.

`browserwright-daemon stop` writes its own `LIFECYCLE stop` line before it
signals, so a stop a *verb* asked for is attributable. A SIGTERM from anywhere
else — a script, a shell, `kill` — left the daemon log with only a bare
`shutdown requested`: the daemon that was SIGTERMed mid-call on 2026-10-19 left
nothing behind that named a signal, a pid, or who was running it, and
reconstructing the sequence cost hours.

These tests pin the daemon's own line, and pin that the CLI's record is not
duplicated when the CLI was the one asking.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.server import listener


@pytest.fixture(autouse=True)
def one_log_file_per_test(monkeypatch, tmp_path):
    """`TMPDIR` is redirected suite-wide, but one directory for the whole
    session — and these tests count `LIFECYCLE` lines by hand."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _lifecycle_events() -> list[str]:
    """Every `LIFECYCLE <event> …` payload in the daemon log, timestamps
    checked on the way past."""
    try:
        text = _ipc.log_path().read_text()
    except FileNotFoundError:
        return []
    events = []
    for line in text.splitlines():
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} LIFECYCLE \w+ ",
            line), line
        events.append(line.split(" LIFECYCLE ", 1)[1])
    return events


def _write_stale_stop_line(pid: int) -> None:
    """A `LIFECYCLE stop` for *pid* from long before the signal arrives."""
    path = _ipc.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"2000-01-01T00:00:00+0000 LIFECYCLE stop pid={pid} probes=ping "
        "initiator=cli:stop cwd=/elsewhere parent='zsh'\n")


def test_a_signalled_daemon_records_the_signal_pid_and_initiator(monkeypatch):
    """Nobody else will ever record this: the killer writes no log line."""
    monkeypatch.setenv(_ipc.INITIATOR_ENV, "cli:stop cwd=/w parent='zsh'")
    stop = asyncio.Event()

    listener._on_stop_signal(stop, signal.SIGTERM)

    assert stop.is_set()
    assert _lifecycle_events() == [
        f"signal sig=SIGTERM pid={os.getpid()} initiator=cli:stop cwd=/w "
        "parent='zsh'"
    ]


def test_a_sigint_is_named_as_sigint(monkeypatch):
    """Ctrl-C in a terminal is a stop too, and it is a different signal."""
    monkeypatch.setenv(_ipc.INITIATOR_ENV, "cli:serve cwd=/w parent='zsh'")

    listener._on_stop_signal(asyncio.Event(), signal.SIGINT)

    assert "signal sig=SIGINT " in _lifecycle_events()[0]


def test_a_stop_the_verb_asked_for_is_not_recorded_twice(monkeypatch):
    """`stop` logs `LIFECYCLE stop pid=<us>` and *then* signals (see
    `cli._cmd_stop`). The daemon must read that trail rather than write a
    second, competing attribution for one event."""
    monkeypatch.setenv(_ipc.INITIATOR_ENV, "cli:stop cwd=/w parent='zsh'")
    _ipc.log_lifecycle("stop", pid=os.getpid(), probes="ping",
                       initiator="cli:stop cwd=/w parent='zsh'")

    stop = asyncio.Event()
    listener._on_stop_signal(stop, signal.SIGTERM)

    assert stop.is_set()
    assert [event.split()[0] for event in _lifecycle_events()] == ["stop"]


def test_a_stop_recorded_for_another_pid_is_not_evidence(monkeypatch):
    """A pid that merely starts with ours (`1234` vs `12349`) is not us."""
    monkeypatch.setenv(_ipc.INITIATOR_ENV, "launchd")
    _ipc.log_lifecycle("stop", pid=int(f"{os.getpid()}9"), probes="ping",
                       initiator="cli:stop cwd=/w parent='zsh'")

    listener._on_stop_signal(asyncio.Event(), signal.SIGTERM)

    assert [event.split()[0] for event in _lifecycle_events()] == ["stop", "signal"]


def test_a_stale_stop_line_is_not_evidence(monkeypatch):
    """The window is what makes it evidence: a stop recorded minutes ago says
    nothing about *this* signal."""
    monkeypatch.setenv(_ipc.INITIATOR_ENV, "launchd")
    _write_stale_stop_line(os.getpid())

    listener._on_stop_signal(asyncio.Event(), signal.SIGTERM)

    assert [event.split()[0] for event in _lifecycle_events()] == ["stop", "signal"]


def test_both_stop_signals_are_wired_to_the_attributed_handler():
    """`run_serve` arms SIGTERM *and* SIGINT; a handler that only sets the
    event is exactly the silence this file exists to prevent."""

    class _RecordingLoop:
        def __init__(self):
            self.handlers: dict = {}

        def add_signal_handler(self, sig, callback, *args):
            self.handlers[sig] = (callback, args)

    loop, stop = _RecordingLoop(), asyncio.Event()
    listener._install_stop_signals(loop, stop)

    assert set(loop.handlers) == {signal.SIGTERM, signal.SIGINT}
    for sig, (callback, args) in loop.handlers.items():
        assert callback is listener._on_stop_signal
        assert args == (stop, sig)

    callback, args = loop.handlers[signal.SIGINT]
    callback(*args)
    assert stop.is_set()
    assert _lifecycle_events()[0].startswith("signal sig=SIGINT ")


def test_a_loop_that_cannot_hold_signal_handlers_is_not_fatal():
    """Unchanged behaviour for a loop without signal support (Windows, and
    pytest's own loop): serve continues, it just has no signal handler."""

    class _NoSignalLoop:
        def add_signal_handler(self, *args):
            raise NotImplementedError

    listener._install_stop_signals(_NoSignalLoop(), asyncio.Event())


@pytest.mark.asyncio
async def test_run_serve_arms_the_attributed_handler(monkeypatch):
    """Wiring, through the real `run_serve`: the handler above is only worth
    anything if the daemon arms it, on the event it then waits on.

    Reuses the sibling stubs of `test_stale_daemon_wiring.py::
    test_serve_wires_logging_before_it_can_reclaim` to stop serve at its
    endpoint bind.
    """
    from browserwright.daemon.config import Config

    armed = []

    async def fake_ping(timeout=1.0, *, host=None, port=None):
        return _ipc.NO_PONG

    class _RefusedEndpoint:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            raise OSError("address already in use")

    monkeypatch.setattr(listener._ipc, "ping_status_async", fake_ping)
    monkeypatch.setattr(listener._ipc, "cleanup_endpoint", lambda: None)
    # `run_serve` writes the pid file before it binds, and `DaemonProbe`
    # reads a pid file as "a daemon died here" (`daemon_traces`) — left behind,
    # it would push every later `status` probe in this session into the
    # half-alive branch.
    monkeypatch.setattr(listener._ipc, "write_pid", lambda pid: None)
    monkeypatch.setattr(listener, "_wire_logging", lambda: None)
    monkeypatch.setattr(listener, "install_json_logging_if_requested",
                        lambda: None)
    monkeypatch.setattr(listener, "_reclaim_stale_daemon_ports", lambda cfg: None)
    monkeypatch.setattr(listener, "_cleanup_orphan_cdp_chrome", lambda: None)
    monkeypatch.setattr(listener, "PlaywrightFacade", _RefusedEndpoint)
    monkeypatch.setattr(listener, "_install_stop_signals",
                        lambda loop, stop: armed.append(stop))

    assert await listener.run_serve(Config(backend="extension")) == 2

    assert len(armed) == 1
    assert isinstance(armed[0], asyncio.Event)
