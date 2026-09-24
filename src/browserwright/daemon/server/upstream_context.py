"""One upstream context: its state, router, lifecycle holder and adapter.

The daemon holds one `UpstreamContext` per upstream connection — the shared
one every extension session multiplexes onto, plus one per `cdp` session
(docs/session-workspaces.md "Routing"). This module is the only place that
turns a ledger record into a context, and so the only place that maps a
backend name to an adapter class.

Everything backend-specific lives in the adapter (`upstream.CdpUpstream`,
`extension_upstream.ExtensionUpstream`), behind the `upstream.Upstream`
protocol. What is left here is backend-agnostic: `UpstreamHolder` lazily opens
the adapter and runs the spec §6.5 close etiquette, and `UpstreamContext`
bundles the pieces and ends a session's workspace.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from .._net import redact_url
from ..config import Config
from .extension_upstream import ExtensionUpstream
from .proxy import Router
from .relay import RelayServer
from .state import CloseReason, DaemonState, UpstreamPhase
from .upstream import CdpUpstream, Upstream

logger = logging.getLogger(__name__)


class UnroutableRecord(LookupError):
    """A ledger record names no backend this daemon can serve."""


# ---- lifecycle holder ------------------------------------------------------


class UpstreamHolder:
    """Lazy-open + graceful-close around one long-lived `Upstream` adapter.

    Backend-agnostic by construction: it holds no backend fields and never
    asks which adapter it wraps. It owns exactly the transitions of the
    context's `DaemonState` (DISCONNECTED → CONNECTING → CONNECTED → CLOSING)
    and the adapter's publication to the `Router` (attach after open, detach
    after close), so a verb never observes a connected router with a
    partially-wired adapter.
    """

    def __init__(self, state: DaemonState, router: Router,
                 make_upstream: Callable[["UpstreamHolder"], Upstream]):
        self.state = state
        self.router = router
        self._open_lock = asyncio.Lock()
        #: Called (sync) when the adapter's connection died on its own. Set by
        #: the daemon for a per-session context, whose connection IS the
        #: session's workspace and must be dropped with it.
        self.on_upstream_lost: Callable[[], None] | None = None
        self.upstream: Upstream = make_upstream(self)

    @property
    def is_open(self) -> bool:
        """Open AND published: the router can use the adapter right now."""
        return self.router.upstream is self.upstream and self.upstream.is_open

    async def _broadcast_event(self, method: str, params: dict) -> None:
        """Fan a `{method, params}` envelope to every connected client.
        Same shape as the `upstreamClosed` broadcast (spec §6.5); v0.5.3 F-3
        surfaces `upstreamConnecting` / `upstreamReady` through it."""
        envelope = json.dumps({"method": method, "params": params})
        for cid in list(self.state.clients.keys()):
            try:
                await self.router._send_to_client(cid, envelope)
            except Exception:
                pass

    async def ensure_open(self) -> None:
        """Open the adapter if not already. Idempotent + reentrant-safe.

        Emits `BrowserwrightDaemon.upstreamConnecting {backend}` once the open
        attempt starts and `BrowserwrightDaemon.upstreamReady {backend,
        ws_url}` once it succeeds (v0.5.3 F-3). A failed open leaves the state
        DISCONNECTED with `last_close_reason = backend_lost` and re-raises.
        """
        if self.is_open:
            return
        async with self._open_lock:
            if self.is_open:
                return
            backend = self.state.backend_name or "auto"
            await self.state.begin_connecting(backend)
            await self._broadcast_event(
                "BrowserwrightDaemon.upstreamConnecting", {"backend": backend})
            upstream = self.upstream
            try:
                await upstream.open()
            except Exception as e:
                logger.warning("upstream open failed: %r", e)
                self.state.last_close_reason = "backend_lost"
                await self.state.set_disconnected()
                raise
            # Publish the complete adapter before CONNECTED becomes visible.
            upstream.attach(self.router)
            await self.state.set_connected(upstream.ws_url or "ext://relay")
            await self._broadcast_event(
                "BrowserwrightDaemon.upstreamReady",
                {"backend": backend, "ws_url": self.state.upstream_ws_url})
            # Task #76: any client frame that arrived during the lazy-open
            # window was buffered per-client. Replay them now that the
            # upstream is live and atomically attached to the router.
            try:
                await self.router.drain_pre_open_buffers()
            except Exception as e:
                logger.warning("drain pre-open buffers failed: %r", e)

    async def trigger_close(self, reason: CloseReason) -> None:
        """Run the spec §6.5 close etiquette + close the adapter.

          1. send Target.detachedFromTarget for each owned sessionId
          2. send BrowserwrightDaemon.upstreamClosed
          3. close the adapter (which ends whatever the adapter owns with its
             connection — e.g. a create-owned Chrome), then DISCONNECTED
        Client websockets are left to the client handler, whose read loop
        ends once the state is DISCONNECTED.
        """
        if self.state.upstream_phase in (UpstreamPhase.DISCONNECTED,
                                         UpstreamPhase.CLOSING):
            return  # already closing / closed — idempotent
        await self.state.begin_closing(reason)

        for cid, client in list(self.state.clients.items()):
            for local_sid, binding in list(client.sessions.items()):
                try:
                    await self.router._send_to_client(cid, json.dumps({
                        "method": "Target.detachedFromTarget",
                        "params": {
                            "sessionId": local_sid,
                            "targetId": binding.target_id,
                        },
                    }))
                except Exception:
                    pass
            # set_disconnected() below wipes everyone's sessions atomically.

        for cid in list(self.state.clients.keys()):
            try:
                await self.router._send_to_client(cid, json.dumps({
                    "method": "BrowserwrightDaemon.upstreamClosed",
                    "params": {"reason": reason},
                }))
            except Exception:
                pass

        up = self.upstream
        try:
            await up.close(code=1000, reason=reason)
        except Exception:
            pass
        await self.state.set_disconnected()
        # Inverse of open: publish DISCONNECTED before removing the one adapter
        # reference, so concurrent verbs lazy-open instead of seeing a connected
        # router with an absent implementation.
        up.detach(self.router)

    async def close_within(self, reason: CloseReason, *,
                           deadline: float | None) -> bool:
        """`trigger_close`, bounded by ``deadline``.

        On a budget miss (or any failure) the context is forced back to a
        retryable DISCONNECTED state instead of staying CLOSING, and False is
        returned; a failure other than the budget re-raises after that.
        """
        close = self.trigger_close(reason)
        try:
            if deadline is None:
                await close
            else:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    close.close()
                    await self.abort()
                    return False
                await asyncio.wait_for(close, timeout=remaining)
        except asyncio.TimeoutError:
            await self.abort()
            logger.warning("closing %s upstream exceeded its budget",
                           self.state.backend_name)
            return False
        except asyncio.CancelledError:
            await asyncio.shield(self.abort())
            raise
        except Exception:
            await self.abort()
            raise
        return True

    async def abort(self) -> None:
        """Restore a retryable, non-CLOSING context after a bounded close
        ran out of budget."""
        up = self.upstream
        await self.state.set_disconnected()
        up.detach(self.router)
        # Detaching only unhooks it from the Router; a live connection keeps
        # its reader running, and a retry would open a second one while frames
        # from this abandoned one still arrive. Best-effort: this path exists
        # because the close already ran out of budget.
        with contextlib.suppress(Exception):
            await up.close(reason="teardown_aborted")

    async def on_upstream_closed(self, reason: str) -> None:
        """The adapter's connection ended on its own (Chrome exited, relay
        gone): run the close etiquette, then tell the owner (a per-session
        context is dead once its browser is, and is dropped)."""
        if self.state.upstream_phase in (UpstreamPhase.DISCONNECTED,
                                         UpstreamPhase.CLOSING):
            return
        await self.trigger_close("chrome_exit")
        if self.on_upstream_lost is not None:
            try:
                self.on_upstream_lost()
            except Exception as e:
                logger.warning("dropping lost upstream context failed: %r", e)


# ---- the context -----------------------------------------------------------


class UpstreamContext:
    """One live upstream: `{state, router, holder}` plus its adapter.

    ``session_id`` is None for the shared context and the owning session's id
    for a per-session one. A per-session context's connection IS that
    session's workspace boundary, so it ends with the session.
    """

    def __init__(self, *, backend: str, state: DaemonState, router: Router,
                 holder: UpstreamHolder, session_id: str | None = None):
        self.backend = backend
        self.state = state
        self.router = router
        self.holder = holder
        self.session_id = session_id

    @property
    def upstream(self) -> Upstream:
        """The context's adapter — always present, open or not."""
        return self.holder.upstream

    def bind_recovery(self, machine: Any,
                      executor_alive: Callable[[str], bool]) -> None:
        self.upstream.bind_recovery(machine, executor_alive)

    async def start(self) -> None:
        await self.upstream.start()

    async def stop(self) -> None:
        await self.upstream.stop()

    async def end_session(self, session_id: str, *,
                          deadline: float | None = None) -> dict:
        """Tear down ``session_id``'s workspace (docs "Teardown").

        The adapter applies the owner rule; a per-session context then closes
        its own connection, bounded by the same deadline. A context that fails
        to close is a real partial, reported as such.
        """
        result = await self.upstream.end_session(session_id, deadline=deadline)
        if self.session_id != session_id or result.get("ok") is not True:
            return result
        if await self.holder.close_within("skill_disconnect", deadline=deadline):
            return result
        return {
            **result,
            "ok": False,
            "partial": True,
            "timedOut": deadline is not None and time.monotonic() >= deadline,
            "failed": ["workspace"],
            "unknown": ["workspace"],
        }


# ---- the factory -----------------------------------------------------------


def build_context(*, backend: str, cfg: Config, session_id: str | None = None,
                  owns_browser: bool = False) -> UpstreamContext:
    """Build one context for ``backend`` — the only backend → adapter map.

    ``extension`` gets an `ExtensionUpstream` over a relay bound at the
    configured host/port (not started here: `UpstreamContext.start` binds it).
    Everything else speaks raw CDP through a `CdpUpstream` resolving ``cfg``.
    The router's lifecycle slots are bound once, here.
    """
    state = DaemonState(backend_name=backend)
    router = Router(state)

    def make_upstream(holder: UpstreamHolder) -> Upstream:
        if backend == "extension":
            host, port = cfg.backends.extension.resolved_host_port()
            ext = ExtensionUpstream(
                relay=RelayServer(host=host, port=port),
                on_frame=router.forward_from_upstream,
                on_close=holder.on_upstream_closed,
                # Generous on purpose: the user may have to load or enable the
                # extension (spec §8.4 'extension-permission' ux_cost).
                open_timeout=max(cfg.timeout, 60.0),
            )
            ext.observe_relay()
            return ext
        return CdpUpstream(
            on_frame=router.forward_from_upstream,
            on_close=holder.on_upstream_closed,
            state=state,
            cfg=cfg,
            session_id=session_id,
            owns_browser=owns_browser,
        )

    holder = UpstreamHolder(state, router, make_upstream)
    router.bind_lifecycle(
        ensure_upstream=holder.ensure_open,
        trigger_disconnect=holder.trigger_close,
        prepare_executor=holder.upstream.prepare_executor,
    )
    return UpstreamContext(backend=backend, state=state, router=router,
                           holder=holder, session_id=session_id)


def endpoint_from_workspace(workspace: object) -> tuple[int | None, str | None]:
    """The ONE reader of a session's `workspace` endpoint. Returns `(port, url)`.

    | workspace | result | meaning |
    |---|---|---|
    | `{"port": 9222}` | `(9222, None)` | a browser on this machine |
    | `{"url": "ws://…"}` | `(None, "ws://…")` | an endpoint handed to us |
    | anything else | `(None, None)` | fall back to the daemon's default port |

    Total on purpose. The ledger is a JSON file a user can hand-edit, and a
    malformed record must fail *safe* rather than open: falling back to the
    operator-configured default port can never reach a browser they did not
    configure, whereas trusting a half-parsed value could.

    `{"port": ...}` and `{"url": ...}` are mutually exclusive by construction —
    there is deliberately no `kind` discriminator, because a discriminator can
    disagree with the value it describes. `int` vs `str` already carries it,
    and the URL's own scheme already separates verbatim-ws from HTTP discovery.
    """
    if not isinstance(workspace, dict):
        return None, None
    url = workspace.get("url")
    if isinstance(url, str) and url:
        return None, url
    port = workspace.get("port")
    # `bool` is an `int` subclass and must not be read as a port number.
    if isinstance(port, int) and not isinstance(port, bool):
        return port, None
    return None, None


def cdp_cfg_for(record: dict, base: Config) -> Config:
    """Derive a per-session cdp Config from the ledger record.

    Pins `backend="cdp"` plus whichever endpoint the session's workspace
    carries. This is the ONLY place a per-session endpoint enters the system,
    and it enters through the Config the adapter resolves on open — the cdp
    surface reads the same `CdpUpstream.cfg`, so there is one channel.
    """
    port, endpoint = endpoint_from_workspace(record.get("workspace"))
    cfg = dataclasses.replace(base, backend="cdp")
    if port is not None or endpoint is not None:
        # `replace` shares the nested BackendsConfig instance; copy the cdp
        # sub-config so per-session pinning never mutates the shared cfg (or
        # another session's context).
        fields: dict = {"endpoint": endpoint}
        if port is not None:
            fields["port"] = port
        cfg.backends = dataclasses.replace(
            cfg.backends,
            cdp=dataclasses.replace(cfg.backends.cdp, **fields),
        )
    return cfg


def context_for_record(session_id: str, record: dict,
                       base: Config) -> UpstreamContext | None:
    """The per-session context a ledger record needs, or None when the record
    rides the shared context.

    - `cdp` → its own context: the browser connection is the workspace
      boundary. `owner == "create"` is carried into the adapter as browser
      ownership (launch on open, kill on end); `attach` never kills.
    - `extension` → None: every extension session multiplexes onto the shared
      relay, scoped by its tab group.
    - anything else (malformed, forward-version, retired `env`) → raises
      `UnroutableRecord`; it must never inherit the shared browser merely
      because it is not named "cdp".
    """
    backend = record.get("backend")
    if backend == "extension":
        return None
    if backend == "cdp":
        cfg = cdp_cfg_for(record, base)
        # Redacted: a per-session endpoint can carry a bearer token, and daemon
        # logs get pasted into bug reports.
        logger.info("cdp context for session %s: port=%s endpoint=%s owner=%s",
                    session_id, cfg.backends.cdp.port,
                    redact_url(cfg.backends.cdp.endpoint), record.get("owner"))
        return build_context(
            backend="cdp", cfg=cfg, session_id=session_id,
            owns_browser=record.get("owner") == "create")
    raise UnroutableRecord(backend)
