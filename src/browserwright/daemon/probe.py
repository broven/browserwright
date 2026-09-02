"""Liveness observations for the single global daemon.

Two callers ask the same question — "is there a half-alive daemon holding the
relay/endpoint ports?" — and answer it from the same two facts: `/__ping__` is
silent, and the ports are held by a *confirmed* browserwright process. They
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
#: ``not_running``                       — no answer and nothing holding a port.
#: ``transient_probe_failed``            — a pid file is present, still no
#:                                          answer, but nothing holds the ports.
#: ``port_held_by_unresponsive_process`` — no answer, and the relay/endpoint
#:                                          ports ARE held. The half-alive
#:                                          daemon. Actionable:
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
    #: The cdp surface of that endpoint, as `{ws, port}` — what a Playwright
    #: client passes to `connect_over_cdp`. `None` when no daemon answered.
    #:
    #: ADR-0011 made this derived rather than reported: there is one port and
    #: one server, so a live daemon has a live cdp surface by construction.
    #: Before the collapse the facade was a *separate* listener that could fail
    #: to bind on its own, which is why this used to be accompanied by a
    #: `facade_error` explaining its absence. That state no longer exists — a
    #: daemon that cannot bind this port does not start at all.
    cdp_surface: dict | None = None
    #: Per-session recovery rows from the daemon's side-effect-free HTTP
    #: status snapshot. ``None`` means the daemon could not be asked.
    sessions: list[dict] | None = None

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
            "cdp_surface": self.cdp_surface,
            # Wire-compatible alias: `facade` was this field's name before the
            # term retired, and `status --json` is consumed by scripts.
            "facade": self.cdp_surface,
            "sessions": self.sessions,
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

        Carries pid and version. Returns ``_ipc.NO_PONG`` when nothing answers.
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

    def daemon_traces(self) -> bool:
        """Whether a daemon left traces that outlive a crash.

        ADR-0011 deleted the control socket file, which is what this used to
        look at. The **pid file** plays the same role: present but silent means
        "a daemon died here", which is the state worth re-probing and then
        classifying as half-alive.

        Deliberately NOT the endpoint state file, even though it is also
        daemon-written and also outlives a crash: that file is a *pointer to an
        address*, and anything may legitimately write one to redirect a client
        (the test suite does exactly that). Reading it as evidence of a corpse
        would send every probe into the port-held branch.
        """
        from . import _ipc
        try:
            return _ipc.pid_path().exists()
        except OSError:
            return False

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

    async def session_rows(self, timeout: float = 1.0) -> list[dict] | None:
        """Read recovery rows without opening a websocket or mutating state."""
        import httpx

        url = str(self.endpoint().get("url") or "").rstrip("/") + "/__status__"
        if not url.startswith(("http://", "https://")):
            return None
        try:
            async with httpx.AsyncClient(
                    timeout=timeout, trust_env=False, mounts={}) as client:
                response = await client.get(url)
            payload = response.json() if response.status_code == 200 else None
            rows = payload.get("sessions") if isinstance(payload, dict) else None
            return rows if isinstance(rows, list) else None
        except Exception:  # noqa: BLE001 - status enrichment is best-effort
            return None

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

    if pid is None and p.daemon_traces():
        pong, probe_state = _retry_then_classify(p)
        pid, version = pong.pid, pong.version
        if probe_state == TRANSIENT_PROBE_FAILED:
            # Still unresponsive, but a daemon left traces — a half-alive
            # daemon may be holding the relay/endpoint ports. Report the truth
            # instead of a bare "not_running" that loops the user through
            # restarts that crash on EADDRINUSE.
            ports = p.daemon_ports()
            if p.listening_ports(ports):
                probe_state = PORT_HELD
                port_holder_pid = (p.confirmed_stale_holder(ports)
                                   or p.live_pid_file_pid())

    # The cdp surface is derived, not reported: one endpoint, one server, so a
    # daemon that answered has one. Built from the SAME resolved URL the ping
    # just used, so `status` can never name an address it did not probe.
    endpoint_info = p.endpoint()
    cdp_surface = None
    sessions = None
    if pid is not None:
        from ..daemon_url import daemon_endpoint
        ep = daemon_endpoint()
        cdp_surface = {"ws": ep.ws("/cdp"), "port": ep.port}
        sessions = await p.session_rows()
    return DaemonStatus(
        alive=pid is not None,
        probe_state=probe_state,
        pid=pid,
        port_holder_pid=port_holder_pid,
        version=version,
        endpoint=endpoint_info,
        cdp_surface=cdp_surface,
        sessions=sessions,
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
    """    Re-ping a silent daemon that left traces behind.

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
