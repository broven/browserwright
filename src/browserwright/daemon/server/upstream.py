"""Upstream ws connection — minimal CDP transport without cdp-use framing.

Why hand-rolled? We need raw frame-in/frame-out because the daemon is a
transparent proxy: a client's outbound text frame gets forwarded byte-for-byte
to upstream, and upstream's response/event frames get forwarded back without
re-parsing or rewriting (§6.3). cdp-use parses + re-emits + tracks ids on its
own; that's two layers of conflict we don't want.

websockets.connect gives us the right primitive: a raw async iterator of text
frames, with `.send(str|bytes)` for the other direction. We also handle the
localhost-proxy-bypass dance from active_tab here.

Spec §6.5 invariant: upstream never auto-reconnects. When the connection
drops, we mark CLOSING and signal up; the caller decides what comes next.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Protocol, TYPE_CHECKING, runtime_checkable

import websockets
from websockets.exceptions import ConnectionClosed

from .._net import is_loopback_host

logger = logging.getLogger(__name__)

# Target-membership answers are deliberately three-valued. ``True`` and
# ``False`` are authoritative answers; ``None`` means this adapter does not
# own the session binding and therefore cannot answer. Callers must handle
# ``None`` explicitly rather than treating it as either permission or denial.
TargetOwnership = bool | None

if TYPE_CHECKING:
    from .proxy import Router


@runtime_checkable
class Upstream(Protocol):
    """Session-shaped browser upstream: one adapter per backend.

    An adapter owns its backend's whole lifecycle, not just its frames:
    ``CdpUpstream`` launches and kills the Chrome a ``--create`` session owns
    and applies the owner rule at teardown; ``ExtensionUpstream`` owns the
    relay, the extension's hello/closed/target events and tab-group
    convergence. Everything outside the adapters (``UpstreamHolder``, the
    verbs, the daemon) calls these members and never asks which backend it is
    talking to.

    ``attach`` / ``detach`` make publication to the router atomic.  An adapter
    is attached before the state becomes CONNECTED and detached only after the
    state becomes DISCONNECTED, so a verb can never observe a connected router
    with a partially-wired implementation.

    The adapter object is long-lived: it exists from context creation to
    daemon exit and is opened/closed any number of times in between (lazy
    open, idle close, reconnect). ``start``/``stop`` bracket the daemon-lifetime
    resources (the extension relay's listening socket); ``open``/``close``
    bracket one connection.
    """

    @property
    def is_open(self) -> bool: ...

    #: The extension relay this adapter speaks through, or ``None`` when it
    #: speaks raw CDP. Read by status/doctor and the cdp surface only.
    @property
    def relay(self) -> Any: ...

    def attach(self, router: "Router") -> None: ...

    def detach(self, router: "Router") -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def open(self, ws_url: str | None = None, *,
                   timeout: float | None = None) -> None: ...

    async def close(self, *, code: int = 1000, reason: str = "") -> None: ...

    #: Wire the daemon's ADR-0013 recovery state machine. Called for every
    #: context the daemon builds, shared or per-session.
    def bind_recovery(self, machine: Any,
                      executor_alive: Callable[[str], bool]) -> None: ...

    #: Step 1 of the drivable path (``Daemon.ensure_session_drivable``): a
    #: short, bounded grace for the browser side to become reachable for
    #: ``session_id``. Raises an actionable ``Unavailable`` when it cannot
    #: inside the control-plane budget. Never mutates the upstream state.
    async def await_browser(self, session_id: str) -> None: ...

    #: Step 3 of the drivable path: make ``session_id`` own one live tab, once
    #: and bounded (ADR-0013). ``force`` skips the healthy fast path (the
    #: explicit recovery verbs). Returns the representative tab —
    #: ``{sessionId, targetId, groupId, recovered, ...}`` — or ``None`` when
    #: nothing needed doing; a forced converge always returns one.
    async def converge(self, session_id: str, *,
                       force: bool = False) -> dict | None: ...

    #: Recovery rung 1 (``recover`` verb): wait, bounded by the backend's own
    #: reconnect window, for the connection between daemon and browser to
    #: come back. Returns a step detail when it had something to wait for,
    #: ``None`` when there was nothing to do; raises ``Unavailable`` with the
    #: human diagnosis when the window runs out.
    async def reconnect(self, session_id: str) -> str | None: ...

    async def open_tab(self, url: str, *, background: bool = True,
                       session_id: str | None = None,
                       group_name: str | None = None,
                       skip_post_attach_commands: bool = False) -> dict: ...

    #: Close one tab *on behalf of a session* and leave that session's durable
    #: recovery anchor consistent. Declared here rather than probed for with
    #: `hasattr`: dispatching on whether a method exists is the backend fork
    #: this protocol removed, and it hides the change when an adapter later
    #: grows the method.
    async def close_session_tab(self, session_id: str,
                                target_id: str) -> dict: ...

    async def list_tabs(self, session_id: str | None = None) -> list[dict]: ...

    async def get_targets(self, params: dict,
                          session_id: str | None = None) -> dict: ...

    async def target_belongs_to_session(
        self, session_id: str, target_id: str,
    ) -> TargetOwnership: ...

    async def current_page(self, session_id: str | None = None) -> dict: ...

    async def attach_active(self, *, session_id: str | None = None,
                            group_name: str | None = None) -> dict: ...

    #: Tear down ``session_id``'s workspace, applying the owner rule to the
    #: *browser* (docs/session-workspaces.md "Teardown"). Returns the honest
    #: ``{ok, closed, failed, unknown, kept, backend}`` result; ``partial`` /
    #: ``timedOut`` mark a retryable budget miss. With a ``deadline`` the
    #: adapter stops cooperatively there and may wait that long for its browser
    #: to become reachable; ``deadline=None`` is the unattended sweep
    #: (auto-prune): unbounded, but it never waits for a disconnected browser.
    async def end_session(self, session_id: str, *,
                          deadline: float | None = None) -> dict: ...

    async def send_cdp(self, frame: str) -> None: ...

    async def wait_session_announce(self, session_id: str,
                                    timeout: float = 2.0) -> bool: ...

    async def userscript_request(self, verb: str, payload: dict,
                                 **kwargs: Any) -> dict: ...

    async def reload_extensions(self, *, reason: str = "manual",
                                expected_version: str | None = None) -> dict: ...

#: Profile-dir prefix of every Chrome a create-owned adapter launches
#: (``bs-s<session id>``). The startup orphan sweep keys on it, so the two can
#: never disagree about which profiles are ours.
OWNED_PROFILE_PREFIX = "bs-s"

# 30s upstream heartbeat — spec §10 open question "Browser.getVersion 心跳频率"
# resolved to 30s.
HEARTBEAT_INTERVAL = 30.0
# Number of synthetic command ids reserved for daemon-internal use (heartbeat,
# Target subscriptions). Client ids passthrough unchanged; daemon uses big
# negatives to avoid colliding with anything a CDP client might send.
_DAEMON_ID_BASE = -2_000_000_000


class CdpUpstream:
    """The raw-CDP adapter: one ws to a browser-level CDP endpoint, plus the
    lifecycle of the browser behind it.

    Lifecycle:
      open() → forward() pumps frames → close() ends it cleanly. The object
      outlives any one connection: idle close and reopen reuse it.

    Browser ownership (docs/session-workspaces.md, CONTEXT.md "owner") lives
    here and nowhere else. ``owns_browser`` is the ledger's ``owner ==
    "create"``: only then does ``open`` launch a dedicated Chrome (profile
    ``bs-s<sid>``), and only a pid this adapter launched is ever signalled.
    An attach-owned adapter has no pid, so every kill path is a no-op for it —
    ending the session closes our websocket and leaves the external browser
    running, through that one data dependency rather than a teardown branch.

    `on_frame(text)` is called for every frame *from* upstream. It is the
    caller's job to forward it downstream (modulo BrowserwrightDaemon.* answers
    which never enter here). `on_close(reason)` fires when the connection ends.
    """

    #: A raw-CDP adapter never speaks through the extension relay.
    relay = None

    def __init__(
        self,
        on_frame: Callable[[str], Awaitable[None]],
        on_close: Callable[[str], Awaitable[None]],
        *,
        state: Any | None = None,
        cfg: Any | None = None,
        session_id: str | None = None,
        owns_browser: bool = False,
    ):
        self._on_frame = on_frame
        self._on_close = on_close
        self._ws: websockets.ClientConnection | None = None  # type: ignore[name-defined]
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._next_internal_id = _DAEMON_ID_BASE
        self._pending_internal: dict[int, asyncio.Future] = {}
        self._ws_url: str | None = None
        self._current_target_id: str | None = None
        self._target_sessions: dict[str, str] = {}
        self._target_info: dict[str, dict] = {}
        self._userscripts: dict[str, dict] = {}
        self._state = state
        # The endpoint this adapter resolves on open. For a per-session
        # context it is pinned from the ledger `workspace` by the context
        # factory; a lazily allocated `--create` port is pinned back here.
        self._cfg = cfg
        self.session_id = session_id
        self.owns_browser = owns_browser
        self._browser_pid: int | None = None
        self._browser_profile_dir: str | None = None
        self._recovery: Any | None = None

    @property
    def backend_name(self) -> str:
        name = getattr(self._state, "backend_name", None)
        return name if isinstance(name, str) and name else "raw-cdp"

    # ---- public API -------------------------------------------------------

    @property
    def ws_url(self) -> str | None:
        return self._ws_url

    @property
    def cfg(self) -> Any | None:
        """The Config whose cdp endpoint this adapter resolves.

        The cdp surface resolves its own byte-for-byte passthrough from the
        same Config, so a per-session endpoint reaches it through exactly one
        channel."""
        return self._cfg

    @property
    def browser_pid(self) -> int | None:
        """Pid of the Chrome this adapter launched, or None (attach-owned, or
        not launched yet)."""
        return self._browser_pid

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    def attach(self, router: "Router") -> None:
        current = router.upstream
        if current is not None and current is not self:
            raise RuntimeError("router already has an upstream")
        router.upstream = self

    def detach(self, router: "Router") -> None:
        if router.upstream is self:
            router.upstream = None

    async def start(self) -> None:
        """Nothing lives longer than a connection on raw CDP."""

    async def stop(self) -> None:
        """Nothing lives longer than a connection on raw CDP."""

    def bind_recovery(self, machine: Any,
                      executor_alive: Callable[[str], bool]) -> None:
        """Accepted for uniformity. A raw-CDP adapter reports no recovery
        events of its own today: its browser/tab is re-proven by the
        executor's bind (``session_state.load`` marks it ``tab-gone``)."""
        self._recovery = machine

    async def await_browser(self, session_id: str) -> None:
        """Nothing to wait for: ``open`` launches/resolves the browser."""

    async def converge(self, session_id: str, *,
                       force: bool = False) -> dict | None:
        """No durable tab binding to reconstruct: the executor's bind resolves
        the current page lazily, so the unforced path has nothing to do.

        Forced (the recovery verbs) it returns the nearest honest equivalent
        of the extension's group recovery: the current live page, or the
        documented blank fallback when the workspace is empty, in the same
        representative-tab shape."""
        if not force:
            return None
        result = await self.current_page(session_id)
        return {**result, "groupId": -1, "recovered": []}

    async def reconnect(self, session_id: str) -> str | None:
        """Nothing outlives a raw-CDP connection to wait for: the drivable
        path's open launches or re-resolves the browser."""
        return None

    async def open(self, ws_url: str | None = None, *,
                   timeout: float | None = None) -> None:
        """Connect to upstream. Raises on failure; caller transitions state.

        With no ``ws_url`` this is the lifecycle open: launch the owned Chrome
        (create-owned, once), resolve the Config's endpoint, connect, and ask
        Chrome to gossip about targets. An explicit ``ws_url`` connects to
        exactly that endpoint and nothing else.
        """
        if self._ws is not None:
            raise RuntimeError("upstream already open")
        lifecycle = ws_url is None
        if lifecycle:
            if self._cfg is None:
                raise ValueError("raw-CDP upstream requires a websocket URL")
            from ..resolver import resolve

            if self.owns_browser and self.session_id is not None:
                await self._launch_browser()
            try:
                ws_url = (await resolve(self._cfg)).ws_url
            except Exception as e:
                logger.warning("upstream resolve failed: %s", e)
                raise
            if timeout is None:
                timeout = self._cfg.timeout
        if timeout is None:
            timeout = 30.0
        assert ws_url is not None
        with _localhost_bypass_proxy(ws_url):
            connect_kwargs: dict[str, Any] = {
                # Big max_size: CDP `Page.captureScreenshot` returns base64
                # blobs that comfortably exceed the websockets default 1MiB.
                "max_size": 100 * 1024 * 1024,
                # Disable per-message-deflate — Chrome's browser-level CDP
                # doesn't speak it, and websockets v15 sometimes negotiates
                # extensions that break the handshake.
                "compression": None,
                # Never route the daemon→browser CDP control channel through the
                # user's ambient web proxy. websockets v15 honors
                # http_proxy/all_proxy by default, which breaks any non-loopback
                # upstream (LAN / Tailscale / an env-backed CloakBrowser profile)
                # that the loopback-only NO_PROXY bypass above can't cover. Same
                # fix as the Playwright facade bridge. (issue #20)
                "proxy": None,
                # Keep the upstream alive with ws-level pings; CDP-level
                # Browser.getVersion heartbeat is layered on top for protocol
                # liveness.
                "ping_interval": 20,
                "ping_timeout": 20,
            }
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(ws_url, **connect_kwargs),
                    timeout=timeout,
                )
            except Exception as e:
                if lifecycle:
                    logger.warning("upstream open failed: %r", e)
                raise
        self._ws_url = ws_url
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if lifecycle:
            # Tell Chrome to gossip about all targets so the router can keep
            # its target table without the client having to enable it.
            try:
                await self.send_command(
                    "Target.setDiscoverTargets", {"discover": True})
            except Exception as e:
                logger.warning("setDiscoverTargets failed: %r", e)

    async def _launch_browser(self) -> None:
        """Launch the Chrome a ``--create`` session owns: a dedicated process
        on its own port with profile ``bs-s{id}``.

        Idempotent: once launched (pid set) this no-ops, so a reconnect never
        spawns a second Chrome while the first is still ours.

        Port selection: reuse the port the ledger pinned into the Config, else
        allocate a free one and pin it back onto ``self._cfg`` so the resolve
        that follows (and the cdp surface) probe the right port.

        ``launch_chrome`` runs in-process (not the CLI) so the spawned pid is
        visible here for teardown. It raises ``Unavailable`` on failure, which
        surfaces to the client as an ordinary upstream-open failure.
        """
        if self._browser_pid is not None:
            return  # already launched (warm reconnect)
        from ..launch_chrome import launch_chrome as _launch_chrome

        cfg = self._cfg
        port = cfg.backends.cdp.port
        if not port:
            import socket as _socket
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            finally:
                s.close()
            import dataclasses as _dc
            cfg = self._cfg = _dc.replace(
                cfg,
                backends=_dc.replace(
                    cfg.backends,
                    cdp=_dc.replace(cfg.backends.cdp, port=port),
                ),
            )

        profile = f"{OWNED_PROFILE_PREFIX}{self.session_id}"
        logger.info("launching cdp Chrome for session %s on port %d (profile %s)",
                    self.session_id, port, profile)
        out = await _launch_chrome(cfg, profile=profile, persistent=True,
                                   port=port, timeout=max(cfg.timeout, 30.0))
        extras = out.get("extras") or {}
        self._browser_pid = extras.get("pid")
        self._browser_profile_dir = extras.get("profile_path")

    def _kill_browser(self) -> bool:
        """SIGTERM the Chrome this adapter launched (best-effort; it may
        already be gone). Clears the pid so a later open relaunches fresh.
        Leaves the profile dir on disk — a persistent ``bs-s{id}`` dir that
        the startup orphan sweep removes; removing it inline races Chrome's
        shutdown writeback. Returns False only when the signal could not be
        delivered. A no-op returning True when no pid is owned — which is
        always the case for an attach-owned adapter."""
        pid = self._browser_pid
        if pid is None:
            return True
        import signal as _signal
        try:
            os.kill(pid, _signal.SIGTERM)
            self._browser_pid = None
            logger.info("killed cdp Chrome pid %d for session %s",
                        pid, self.session_id)
            return True
        except ProcessLookupError as e:
            self._browser_pid = None
            logger.debug("cdp Chrome pid %s already gone: %r", pid, e)
            return True
        except (PermissionError, OSError) as e:
            logger.warning("could not terminate cdp Chrome pid %s: %r", pid, e)
            return False

    async def send_text(self, frame: str) -> None:
        """Forward a downstream frame to upstream verbatim."""
        if self._ws is None:
            raise RuntimeError("upstream not open")
        await self._ws.send(frame)

    async def send_cdp(self, frame: str) -> None:
        """Forward one downstream CDP frame through this upstream."""
        await self.send_text(frame)

    async def send_command(self, method: str, params: dict | None = None,
                           session_id: str | None = None,
                           timeout: float = 10.0) -> dict:
        """Daemon-internal command — distinct id space from client ids so
        results never collide with downstream traffic.

        Used for: initial Target.setDiscoverTargets to populate the target
        table, the periodic Browser.getVersion heartbeat, and the close-time
        Target.detachFromTarget.
        """
        if self._ws is None:
            raise RuntimeError("upstream not open")
        cmd_id = self._alloc_id()
        msg: dict[str, Any] = {"id": cmd_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session_id is not None:
            msg["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_internal[cmd_id] = fut
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_internal.pop(cmd_id, None)

    @staticmethod
    def _result(envelope: object) -> dict:
        if not isinstance(envelope, dict):
            raise RuntimeError(f"malformed CDP response: {envelope!r}")
        if envelope.get("error"):
            raise RuntimeError(f"CDP error: {envelope['error']!r}")
        result = envelope.get("result")
        return result if isinstance(result, dict) else {}

    async def open_tab(self, url: str, *, background: bool = True,
                       session_id: str | None = None,
                       group_name: str | None = None,
                       skip_post_attach_commands: bool = False) -> dict:
        """Create and attach a raw browser target.

        ``background`` and ``group_name`` are intentionally ignored: a raw-CDP
        workspace has no user-owned focus to protect and never uses tab groups.
        """
        created = self._result(await self.send_command(
            "Target.createTarget", {"url": url}))
        target_id = created.get("targetId")
        if not isinstance(target_id, str):
            raise RuntimeError(f"Target.createTarget returned {created!r}")
        attached = self._result(await self.send_command(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}))
        upstream_sid = attached.get("sessionId")
        if not isinstance(upstream_sid, str):
            raise RuntimeError(f"Target.attachToTarget returned {attached!r}")
        self._target_sessions[target_id] = upstream_sid
        await self._apply_userscripts_to_session(upstream_sid)
        state_meta = (
            self._state.targets.get(target_id)
            if self._state is not None else None
        ) or {}
        self._target_info[target_id] = {
            "url": state_meta.get("url", url),
            "title": state_meta.get("title", ""),
            "attached": True,
        }
        self._current_target_id = target_id
        return {
            "sessionId": upstream_sid,
            "targetId": target_id,
            "tabId": None,
            "url": self._target_info[target_id]["url"],
            "title": self._target_info[target_id]["title"],
            "groupId": -1,
        }

    async def list_tabs(self, session_id: str | None = None) -> list[dict]:
        result = self._result(await self.send_command("Target.getTargets", {}))
        tabs: list[dict] = []
        for raw in result.get("targetInfos", []):
            if not isinstance(raw, dict) or raw.get("type") != "page":
                continue
            target_id = raw.get("targetId")
            if not isinstance(target_id, str):
                continue
            tab = dict(raw)
            tab.update({
                "targetId": target_id,
                "type": "page",
                "url": str(raw.get("url", "")),
                "title": str(raw.get("title", "")),
                "attached": bool(raw.get("attached", False)
                                 or target_id in self._target_sessions),
            })
            tabs.append(tab)
            self._target_info[target_id] = tabs[-1]
        return tabs

    async def get_targets(self, params: dict,
                          session_id: str | None = None) -> dict:
        """Return Chrome's native ``Target.getTargets`` response envelope.

        Raw-CDP is a compatibility boundary: unlike the high-level
        ``list_tabs`` helper, this path must preserve request filters, every
        target type, and every response field exactly as Chrome returned it.
        ``session_id`` is intentionally unused. CDP scopes each session at its
        per-session browser connection. Env uses the daemon's shared browser
        connection, but session creation admits only one env session per
        daemon, so that connection is still the sole env session's workspace.
        """
        return await self.send_command("Target.getTargets", params)

    async def target_belongs_to_session(
        self, session_id: str, target_id: str,
    ) -> bool:
        """Authorize at the raw-CDP browser-instance boundary.

        CDP has a per-session connection. Env relies on the atomic
        one-env-session-per-daemon creation invariant before using its shared
        connection as the same boundary.
        """
        return True

    async def current_page(self, session_id: str | None = None) -> dict:
        if self._state is not None:
            for target_id, attacher in self._state.attachers.items():
                owner = self._state.clients.get(attacher.primary_client_id)
                if (session_id is not None
                        and getattr(owner, "session_id", None) != session_id):
                    continue
                meta = self._state.targets.get(target_id) or {}
                if meta.get("type", "page") != "page":
                    continue
                self._target_sessions[target_id] = attacher.upstream_session_id
                self._current_target_id = target_id
                return {
                    "sessionId": attacher.upstream_session_id,
                    "targetId": target_id,
                    "tabId": None,
                    "url": meta.get("url", ""),
                    "title": meta.get("title", ""),
                    "groupId": -1,
                }
        if (self._current_target_id is not None
                and self._current_target_id in self._target_sessions):
            target_id = self._current_target_id
            meta = self._target_info.get(target_id) or {}
            return {
                "sessionId": self._target_sessions[target_id],
                "targetId": target_id,
                "tabId": None,
                "url": meta.get("url", ""),
                "title": meta.get("title", ""),
                "groupId": -1,
            }
        # Deliberately unguarded. Swallowing an enumeration failure here turns
        # "I could not ask Chrome" into "the browser has no tabs", and the
        # fallback below then creates an about:blank — duplicating a tab the
        # user can see and splitting the persisted target from the selected
        # page. session-workspaces.md requires failing retryably under
        # uncertainty rather than inventing a page, so let it propagate and
        # reserve open_tab for a *confirmed* empty browser.
        tabs = await self.list_tabs(session_id)
        current = next((tab for tab in tabs
                        if tab["targetId"] == self._current_target_id), None)
        if current is None:
            current = tabs[0] if tabs else None
        if current is None:
            return await self.open_tab("about:blank", session_id=session_id)
        target_id = current["targetId"]
        upstream_sid = self._target_sessions.get(target_id)
        if upstream_sid is None:
            attached = self._result(await self.send_command(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True}))
            upstream_sid = attached.get("sessionId")
            if not isinstance(upstream_sid, str):
                raise RuntimeError(f"Target.attachToTarget returned {attached!r}")
            self._target_sessions[target_id] = upstream_sid
            await self._apply_userscripts_to_session(upstream_sid)
        self._current_target_id = target_id
        return {
            "sessionId": upstream_sid,
            "targetId": target_id,
            "tabId": None,
            "url": current.get("url", ""),
            "title": current.get("title", ""),
            "groupId": -1,
        }

    async def attach_active(self, *, session_id: str | None = None,
                            group_name: str | None = None) -> dict:
        """Nearest honest raw-CDP equivalent: return the current page."""
        return await self.current_page(session_id)

    async def close_tab(self, target: str) -> dict:
        target_id = target
        for known_target, upstream_sid in self._target_sessions.items():
            if upstream_sid == target:
                target_id = known_target
                break
        closed = self._result(await self.send_command(
            "Target.closeTarget", {"targetId": target_id}))
        if closed.get("success") is False:
            raise RuntimeError(f"Target.closeTarget refused {target_id!r}")
        self._target_sessions.pop(target_id, None)
        self._target_info.pop(target_id, None)
        if self._current_target_id == target_id:
            self._current_target_id = None
        return {"ok": True, "tabId": None}

    async def close_session_tab(self, session_id: str,
                                target_id: str) -> dict:
        """Close one of this session's tabs and clear a stale ledger anchor.

        A raw-CDP session's workspace is the browser instance, so there is no
        tab-group ownership to re-prove — but the ledger still records a
        `current_target_id`, and leaving it pointing at a target we just closed
        makes the next bind resolve a target that no longer exists. Symmetric
        to the extension adapter's re-anchoring, minus the group.
        """
        result = await self.close_tab(target_id)
        try:
            from ... import session_registry
            record = session_registry.get(session_id)
            if isinstance(record, dict):
                runtime = dict(record.get("runtime") or {})
                if runtime.get("current_target_id") == target_id:
                    runtime["current_target_id"] = None
                    runtime["updated_at"] = time.time()
                    session_registry.update(session_id, runtime=runtime)
        except Exception as e:  # noqa: BLE001 — anchor hygiene, never fatal
            logger.warning(
                "close_session_tab(%s): could not clear ledger anchor: %r",
                session_id, e)
        return result

    async def end_session(self, session_id: str, *,
                          deadline: float | None = None) -> dict:
        """Apply the owner rule to the browser: kill it only if we launched it.

        The workspace of a raw-CDP session is the browser instance, so there
        are no tabs to close one by one. A create-owned adapter terminates its
        Chrome here — before any cancellable await, so a budget miss further
        up can lose notifications but never leak the process. An attach-owned
        adapter owns no pid and this is a no-op: the external browser keeps
        running. Closing the connection itself is the owning context's job
        (``UpstreamContext.end_session``).
        """
        if not self._kill_browser():
            raise RuntimeError(
                f"could not terminate cdp Chrome for session {session_id!r}")
        return {
            "ok": True,
            "partial": False,
            "timedOut": False,
            "closed": [],
            "failed": [],
            "unknown": [],
            "kept": [],
            "backend": self.backend_name,
        }

    async def wait_session_announce(self, session_id: str,
                                    timeout: float = 2.0) -> bool:
        return True

    async def reload_extensions(self, *, reason: str = "manual",
                                expected_version: str | None = None) -> dict:
        return {
            "ok": False,
            "sent": 0,
            "extensions": [],
            "applicable": False,
            "reason": "not applicable to a raw-CDP backend",
        }

    async def _unregister_userscript(self, entry: dict) -> list[dict]:
        """Remove live registrations, retaining handles for any failures."""
        remaining: list[tuple[str, str]] = []
        failed: list[dict] = []
        for sid, identifier in entry.get("ids", []):
            try:
                await self.send_command(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": identifier}, sid)
            except Exception as e:  # noqa: BLE001 - reflected in honest result
                remaining.append((sid, identifier))
                failed.append({
                    "id": entry.get("id"),
                    "sessionId": sid,
                    "error": repr(e),
                })
        entry["ids"] = remaining
        return failed

    def _forget_target(self, target_id: str) -> None:
        """Evict every adapter cache keyed on one destroyed target.

        Also drops the userscript handles registered in that page's session:
        the registration died with the session, so keeping the
        ``(sessionId, identifier)`` pair would make a later remove or toggle
        fail against a session that no longer exists — permanently, since
        nothing else prunes them.
        """
        upstream_sid = self._target_sessions.pop(target_id, None)
        self._target_info.pop(target_id, None)
        if self._current_target_id == target_id:
            self._current_target_id = None
        if upstream_sid is None:
            return
        for entry in self._userscripts.values():
            ids = entry.get("ids")
            if ids:
                entry["ids"] = [(sid, ident) for sid, ident in ids
                                if sid != upstream_sid]

    async def _apply_userscripts_to_session(self, upstream_sid: str) -> None:
        """Install every stored, enabled script into a freshly attached page.

        ``Page.addScriptToEvaluateOnNewDocument`` is per-session, so a script
        installed before a tab existed would otherwise never run in it — the
        caller was told the install succeeded, and it silently applied to
        nothing. Best-effort: a failure here costs one page its scripts, never
        the attach that triggered it.
        """
        for entry in list(self._userscripts.values()):
            if not entry.get("enabled", True):
                continue
            if any(sid == upstream_sid for sid, _ in entry.get("ids", [])):
                continue
            try:
                await self._register_userscript(entry, [upstream_sid],
                                                replace_ids=False)
            except Exception as e:  # noqa: BLE001 - never break the attach
                logger.warning(
                    "could not apply userscript %r to session %s: %r",
                    entry.get("id"), upstream_sid, e)

    async def _register_userscript(
        self, entry: dict, sessions: list[str], *, replace_ids: bool = True,
    ) -> list[dict]:
        """Register one stored script in each live page session."""
        source = (entry.get("source") or entry.get("body")
                  or entry.get("code") or "")
        identifiers: list[tuple[str, str]] = []
        failed: list[dict] = []
        for sid in dict.fromkeys(sessions):
            try:
                result = self._result(await self.send_command(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": source}, sid))
                identifier = result.get("identifier")
                if not isinstance(identifier, str):
                    raise RuntimeError(
                        "Page.addScriptToEvaluateOnNewDocument returned no identifier")
                identifiers.append((sid, identifier))
            except Exception as e:  # noqa: BLE001 - reflected in honest result
                failed.append({
                    "id": entry.get("id"),
                    "sessionId": sid,
                    "error": repr(e),
                })
        # replace_ids=False is the incremental path (a new page attaching to an
        # already-installed script): keep what is registered elsewhere instead
        # of forgetting it.
        entry["ids"] = identifiers if replace_ids else [
            *entry.get("ids", []), *identifiers]
        return failed

    def _userscript_sync(self, failed: list[dict] | None = None) -> dict:
        """Honest sync state: stored is not the same as active.

        ``ok`` means nothing failed. It does NOT mean the script is running —
        with no live page target there is nothing to register against, and
        reporting a bare success there tells the caller the script is active
        when it is not. ``pending`` marks exactly that case, so "stored, will
        apply to the next tab" is distinguishable from "running now".
        """
        failures = failed or []
        registered = sum(
            len(script.get("ids", []))
            for script in self._userscripts.values())
        return {
            "ok": not failures,
            "registered": registered,
            "pending": bool(self._userscripts) and registered == 0
            and not failures,
            "failed": failures,
        }

    async def userscript_request(self, verb: str, payload: dict,
                                 **kwargs: Any) -> dict:
        """Raw-CDP userscript shim using new-document page scripts."""
        sessions = [sid for sid in kwargs.get("session_ids", [])
                    if isinstance(sid, str)]
        if not sessions:
            # The caller's bindings are not the source of truth here. A one-shot
            # CLI websocket has no bindings at all, and passing its empty list
            # through would register the script against nothing and still report
            # success. For raw-CDP the workspace *is* the browser instance, so
            # this adapter's own attached page targets are exactly the session's
            # targets — ask ourselves rather than the transient client.
            sessions = list(dict.fromkeys(self._target_sessions.values()))
        if verb == "install":
            script = payload.get("script") if isinstance(payload.get("script"), dict) else {}
            source = (script.get("source") or script.get("body")
                      or script.get("code") or "")
            script_id = script.get("id") or (
                f"cdp-us-{len(self._userscripts) + 1}")
            identity = script.get("identity") or script_id
            if not isinstance(source, str) or not source:
                raise ValueError("userscript install requires script.source")
            existing = self._userscripts.get(str(script_id))
            if existing is not None:
                failed = await self._unregister_userscript(existing)
                if failed:
                    raise RuntimeError(
                        f"could not replace userscript {script_id!r}: {failed!r}")
            entry = {
                **script,
                "id": str(script_id),
                "identity": str(identity),
                "ids": [],
                "enabled": True,
            }
            self._userscripts[str(script_id)] = entry
            failed = await self._register_userscript(entry, sessions)
            return {
                "ok": True,
                "id": script_id,
                "identity": identity,
                "warnings": [
                    *list(script.get("warnings") or []),
                    "raw-CDP shim runs in MAIN world without match filtering",
                ],
                "sync": self._userscript_sync(failed),
            }
        if verb == "list":
            return {
                "scripts": [
                    {k: v for k, v in value.items() if k != "ids"}
                    for value in self._userscripts.values()
                ],
                "master": True,
            }
        if verb in ("remove", "toggle"):
            key = payload.get("key")
            entry_key = key if isinstance(key, str) and key in self._userscripts else next(
                (script_id for script_id, script in self._userscripts.items()
                 if script.get("identity") == key),
                None,
            )
            entry = self._userscripts.get(entry_key) if entry_key else None
            if entry is None:
                if verb == "remove":
                    return {
                        "ok": True,
                        "removed": None,
                        "sync": self._userscript_sync(),
                    }
                raise ValueError(f"userscript not found: {key}")
            failed = await self._unregister_userscript(entry)
            if verb == "remove":
                if failed:
                    return {
                        "ok": False,
                        "removed": None,
                        "sync": self._userscript_sync(failed),
                    }
                self._userscripts.pop(entry_key, None)
                return {
                    "ok": True,
                    "removed": entry_key,
                    "sync": self._userscript_sync(),
                }
            enabled = bool(payload.get("enabled"))
            if failed:
                return {
                    "ok": False,
                    "id": entry["id"],
                    "enabled": bool(entry.get("enabled", True)),
                    "sync": self._userscript_sync(failed),
                }
            if enabled:
                failed = await self._register_userscript(entry, sessions)
            entry["enabled"] = enabled
            return {
                "ok": not failed,
                "id": entry["id"],
                "enabled": enabled,
                "sync": self._userscript_sync(failed),
            }
        if verb == "logs":
            return {"logs": []}
        return {"ok": False,
                "reason": (f"unsupported userscript verb {verb!r} on "
                           f"{self.backend_name}")}

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        """Close the upstream cleanly. Idempotent.

        An owned Chrome is a daemon child and dies with its connection on
        every close path (session end, idle close, daemon shutdown, Chrome
        exit). Killed first, synchronously, so no await in the websocket close
        below can leak it.
        """
        self._kill_browser()
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        ws = self._ws
        self._ws = None
        for fut in self._pending_internal.values():
            if not fut.done():
                fut.set_exception(ConnectionError("upstream closing"))
        self._pending_internal.clear()
        if ws is not None:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass
        self._ws_url = None
        self._target_sessions.clear()
        self._target_info.clear()
        self._current_target_id = None
        # New-document scripts are registered per page session, and every
        # session died with this connection. A reopen starts with none, as a
        # freshly connected browser would.
        self._userscripts.clear()

    # ---- internal ---------------------------------------------------------

    def _alloc_id(self) -> int:
        v = self._next_internal_id
        self._next_internal_id += 1
        return v

    async def _reader_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                # Intercept responses to *our* internal ids (heartbeat etc).
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    cid = parsed.get("id")
                    if isinstance(cid, int) and cid in self._pending_internal:
                        fut = self._pending_internal.pop(cid)
                        if not fut.done():
                            fut.set_result(parsed)
                        continue
                    # Chrome is the authority on which targets exist. A tab can
                    # go away without passing through close_tab — the user
                    # closes it, or Playwright calls page.close() — and these
                    # caches would otherwise keep answering with a target that
                    # is gone.
                    if parsed.get("method") == "Target.targetDestroyed":
                        tid = (parsed.get("params") or {}).get("targetId")
                        if isinstance(tid, str):
                            self._forget_target(tid)
                # Forward to downstream.
                try:
                    await self._on_frame(text)
                except Exception as e:
                    logger.warning("on_frame raised: %r", e)
        except ConnectionClosed as e:
            logger.info("upstream closed: code=%s reason=%s", e.code, e.reason)
        except Exception as e:
            logger.warning("upstream reader crashed: %r", e)
        finally:
            # Always notify close — this is the canonical signal for the
            # state machine to enter CLOSING (caller decides reason).
            try:
                await self._on_close("upstream-eof")
            except Exception:
                pass

    async def _heartbeat_loop(self) -> None:
        """Keep CDP alive by pinging `Browser.getVersion` every 30s.

        Spec §10 open question: 30s is the chosen cadence. Too fast = wasted
        CDP traffic; too slow = stale-Chrome detection latency. Tunable later.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws is None:
                    return
                try:
                    await self.send_command("Browser.getVersion", timeout=10)
                except (asyncio.TimeoutError, ConnectionError, ConnectionClosed):
                    logger.warning("heartbeat failed, closing upstream")
                    return
        except asyncio.CancelledError:
            return


# ---- localhost proxy bypass (same trick as active_tab) --------------------


@contextlib.contextmanager
def _localhost_bypass_proxy(ws_url: str):
    """When the upstream URL is loopback, ensure NO_PROXY covers it.

    Spec doesn't mention this — but a browser we launched runs on the user's
    machine, and the user often has HTTPS_PROXY / ALL_PROXY set. An external
    endpoint is the opposite case: there the proxy is usually intentional, so
    we leave it alone. `is_loopback_host` is what decides which one this is,
    and it is the same predicate the cdp backend uses to pick `trust_env`.
    """
    if not is_loopback_host(ws_url):
        yield
        return
    prev = os.environ.get("NO_PROXY", "")
    augmented = prev
    for h in ("127.0.0.1", "localhost", "::1"):
        if h not in augmented:
            augmented = f"{augmented},{h}" if augmented else h
    os.environ["NO_PROXY"] = augmented
    try:
        yield
    finally:
        if prev:
            os.environ["NO_PROXY"] = prev
        else:
            os.environ.pop("NO_PROXY", None)


# Compatibility name for callers/tests that still import the old transport-
# shaped class.  The concrete implementation is now the raw-CDP Upstream
# adapter, covering both cdp and env.
UpstreamConnection = CdpUpstream
