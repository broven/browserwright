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

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import _ipc


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
    #: Why there is no facade, straight from the daemon. Set exactly when
    #: `facade` is None and the daemon answered — the missing half of the old
    #: "facade: null, no idea why" report.
    facade_error: str | None = None

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
            # disabled, failed to bind, or the daemon predates auto-enable. The
            # client layer reads this to `connect_over_cdp` the heredoc
            # `page`/`context`.
            "facade": self.facade,
            # Populated whenever `facade` is None on a live daemon: the daemon's
            # own words for why. Answered live over `/__ping__` — there is no
            # discovery file to go missing behind our back.
            "facade_error": self.facade_error,
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

    def ping(self, timeout: float) -> "_ipc.PongInfo":
        """The daemon's ``/__ping__`` answer as an ``_ipc.PongInfo``.

        Carries pid, version AND the live facade state — one round trip, one
        source of truth. Returns ``_ipc.NO_PONG`` when nothing answers.
        """
        from . import _ipc
        return _ipc.ping_status_sync(timeout=timeout)

    async def ping_async(self, timeout: float) -> "_ipc.PongInfo":
        """The ``_ipc.PongInfo`` for async drivers (``daemon doctor``).

        The sync :meth:`ping` runs ``asyncio.run`` internally, which raises
        inside a running event loop — so async callers must not call it
        directly. Default: the sync observation in a worker thread, safe both
        outside a loop (``asyncio.run`` happens inside the worker) and inside
        one. Subclasses that override ``ping`` keep working from async drivers
        unchanged.
        """
        return await asyncio.to_thread(self.ping, timeout)

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

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


async def daemon_status_async(cfg, *, probe: DaemonProbe | None = None) -> DaemonStatus:
    """Probe the daemon and classify it. Pure state machine over ``probe``.

    Async driver for callers that already own an event loop (``daemon doctor``,
    issue #28) — the sync ``DaemonProbe.ping`` would raise inside one. The
    retry backoff still uses the probe's sync ``sleep``: this is only ever
    driven from CLI paths where nothing else runs concurrently.
    """
    p = probe if probe is not None else DaemonProbe(cfg)

    pong = await p.ping_async(1.0)
    pid, version = pong.pid, pong.version
    probe_state = OK if pid is not None else NOT_RUNNING
    port_holder_pid = None

    if pid is None and p.socket_present():
        pong, probe_state = _retry_then_classify(p)
        pid, version = pong.pid, pong.version
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

    # The facade is whatever the daemon just said it is. `pong.facade is None`
    # means the daemon predates facade advertising — reported as "unknown", not
    # as a healthy or a broken facade.
    facade = pong.facade
    facade_dict = ({"ws": facade.ws, "port": facade.port}
                   if (pid is not None and facade is not None and facade.available)
                   else None)
    facade_error = None
    if pid is not None and facade_dict is None:
        facade_error = (
            facade.error if facade is not None and facade.error
            else (f"daemon {version or 'of unknown version'} does not advertise "
                  "its Playwright facade; restart it with "
                  "`browserwright-daemon restart` after upgrading"))
    return DaemonStatus(
        alive=pid is not None,
        probe_state=probe_state,
        pid=pid,
        port_holder_pid=port_holder_pid,
        version=version,
        endpoint=p.endpoint(),
        facade=facade_dict,
        facade_error=facade_error,
    )


def daemon_status(cfg, *, probe: DaemonProbe | None = None) -> DaemonStatus:
    """Probe the daemon and classify it. Sync driver for CLI paths (``status``).

    Delegates to :func:`daemon_status_async` so ``status`` and ``doctor`` read
    the world through one state machine and can never drift into disagreeing
    about what "half-alive" means — the module's reason for existing. Must be
    called with no asyncio loop running (CLI paths).
    """
    return asyncio.run(daemon_status_async(cfg, probe=probe))


def _retry_then_classify(p: DaemonProbe):
    """Re-ping a silent daemon whose socket file is present.

    A daemon mid-GC / mid-reconnect can miss one 1s ping and still be perfectly
    alive, so a single miss must not be reported as death.

    Returns ``(pong, probe_state)``.
    """
    from . import _ipc
    deadline = time.monotonic() + p.retry_window
    while time.monotonic() < deadline:
        p.sleep(p.retry_interval)
        pong = p.ping(0.3)
        if pong.pid is not None:
            return pong, OK_AFTER_RETRY
    return _ipc.NO_PONG, TRANSIENT_PROBE_FAILED
