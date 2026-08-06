"""Liveness observations for the single global daemon.

Two callers ask the same question — "is there a half-alive daemon holding the
relay/facade ports?" — and answer it from the same two facts: the control socket
is dead, and the ports are held by a *confirmed* browserwright process. They
differ only in what they do next.

  `status`  (`cli._cmd_status` → :func:`daemon_status`)  reports it.
  `serve`   (`listener._reclaim_stale_daemon_ports`)     reclaims it.

Both now read the world through one :class:`DaemonProbe`, which is what stops
them from drifting into disagreeing about what "half-alive" means. The probe
only *observes*; the reclaim (the one destructive step) stays in
``_stale.reclaim_ports`` where the safety rules for signalling a stranger live.

Why an object rather than free functions: every observation is side-effecting
(opens sockets, runs ``lsof``, sleeps), and controlling them is exactly what a
test of either caller needs. Before this module both callers reached directly
into ``_ipc`` and ``_stale``, so testing `status` meant monkeypatching six
module globals across three modules — and the test said so, in its own
docstring. Now a test subclasses :class:`DaemonProbe`, overrides the handful of
observations it cares about, and patches nothing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


#: Probe states, in the order `status` can reach them.
#:
#: ``ok``                                — the daemon answered the first ping.
#: ``ok_after_retry``                    — silent at first, answered within the
#:                                          retry window (a busy daemon, not a
#:                                          dead one).
#: ``not_running``                       — no answer and no socket file.
#: ``transient_probe_failed``            — socket file present, still no answer,
#:                                          but nothing holds the TCP ports.
#: ``port_held_by_unresponsive_process`` — socket file present, no answer, and
#:                                          the relay/facade ports ARE held. The
#:                                          half-alive daemon. Actionable:
#:                                          `browserwright-daemon restart`.
OK = "ok"
OK_AFTER_RETRY = "ok_after_retry"
NOT_RUNNING = "not_running"
TRANSIENT_PROBE_FAILED = "transient_probe_failed"
PORT_HELD = "port_held_by_unresponsive_process"


@dataclass(frozen=True)
class DaemonStatus:
    """One `status` answer. :meth:`to_dict` is the wire shape (schema 1)."""

    alive: bool
    probe_state: str
    pid: int | None
    port_holder_pid: int | None
    version: str | None
    endpoint: dict
    facade: dict | None

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "alive": self.alive,
            "probe_state": self.probe_state,
            "pid": self.pid,
            # issue #15 (2.1): pid of the process holding the relay/facade ports
            # when the daemon is unresponsive. Never a kill target we picked —
            # a hint the user (or `restart`) acts on.
            "port_holder_pid": self.port_holder_pid,
            "version": self.version,
            "endpoint": self.endpoint,
            # Playwright facade discovery (Phase C). None when the facade is
            # disabled or the daemon predates auto-enable. The skill layer reads
            # this to `connect_over_cdp` the heredoc `page`/`context`.
            "facade": self.facade,
        }


class DaemonProbe:
    """Every side-effecting observation `status` makes, in one overridable place.

    Subclass and override the methods a test needs; the defaults are the real
    ``_ipc`` / ``_stale`` calls. Nothing here interprets — :func:`daemon_status`
    owns the state machine, this owns the I/O.
    """

    #: How long to keep re-pinging a silent-but-socket-present daemon before
    #: concluding it is not merely busy.
    retry_window = 0.6
    retry_interval = 0.15

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def ping(self, timeout: float) -> tuple[int | None, str | None]:
        """``(pid, version)`` from the daemon's ``/__ping__``, or ``(None, None)``."""
        from . import _ipc
        return _ipc.ping_status_sync(timeout=timeout)

    def socket_present(self) -> bool:
        """Whether the control socket *file* exists (it outlives a crashed daemon)."""
        from . import _ipc
        return _ipc.sock_path().exists()

    def daemon_ports(self) -> list[int]:
        """The relay + facade TCP ports this cfg says a daemon would bind."""
        from . import _stale
        return _stale.daemon_tcp_ports(self.cfg)

    def listening_ports(self, ports: list[int]) -> list[int]:
        """Which of ``ports`` something is already holding.

        `status` only needs "any?"; `serve` needs the actual subset, because it
        only ever reclaims ports it saw held. One primitive answers both.
        """
        from . import _stale
        return [p for p in ports if _stale.port_is_listening("127.0.0.1", p)]

    def confirmed_stale_holder(self, ports: list[int]) -> int | None:
        """Pid of a *confirmed browserwright* daemon holding one of ``ports``."""
        from . import _stale
        return _stale.confirmed_stale_holder(ports)

    def live_pid_file_pid(self) -> int | None:
        """Best-effort fallback holder hint: the pid file's pid, if still alive.

        Used only when ``lsof`` is unavailable or the holder isn't identifiable,
        so we still tell the user *a* pid instead of nothing. Never a kill target.
        """
        from . import _ipc, _stale
        fp = _ipc.read_pid()
        return fp if fp and _stale.pid_alive(fp) else None

    def endpoint(self) -> dict:
        from . import _ipc
        return _ipc.endpoint_describe()

    def facade(self) -> tuple[str | None, int | None]:
        from . import _ipc
        return _ipc.read_facade_file()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def daemon_status(cfg, *, probe: DaemonProbe | None = None) -> DaemonStatus:
    """Probe the daemon and classify it. Pure state machine over ``probe``."""
    p = probe if probe is not None else DaemonProbe(cfg)

    pid, version = p.ping(1.0)
    probe_state = OK if pid is not None else NOT_RUNNING
    port_holder_pid = None

    if pid is None and p.socket_present():
        pid, version, probe_state = _retry_then_classify(p)
        if probe_state == TRANSIENT_PROBE_FAILED:
            # Still unresponsive, but its socket file is present — a half-alive
            # daemon may be holding the relay/facade ports. Report the truth
            # instead of a bare "not_running" that loops the user through
            # restarts that crash on EADDRINUSE.
            ports = p.daemon_ports()
            if p.listening_ports(ports):
                probe_state = PORT_HELD
                port_holder_pid = (p.confirmed_stale_holder(ports)
                                   or p.live_pid_file_pid())

    facade_ws, facade_port = p.facade()
    return DaemonStatus(
        alive=pid is not None,
        probe_state=probe_state,
        pid=pid,
        port_holder_pid=port_holder_pid,
        version=version,
        endpoint=p.endpoint(),
        facade=({"ws": facade_ws, "port": facade_port}
                if (pid is not None and facade_ws) else None),
    )


def _retry_then_classify(p: DaemonProbe):
    """Re-ping a silent daemon whose socket file is present.

    A daemon mid-GC / mid-reconnect can miss one 1s ping and still be perfectly
    alive, so a single miss must not be reported as death.
    """
    deadline = time.monotonic() + p.retry_window
    while time.monotonic() < deadline:
        p.sleep(p.retry_interval)
        pid, version = p.ping(0.3)
        if pid is not None:
            return pid, version, OK_AFTER_RETRY
    return None, None, TRANSIENT_PROBE_FAILED
