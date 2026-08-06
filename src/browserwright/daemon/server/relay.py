"""Extension relay ws server (v0.4 — spec §8.4).

The relay sits between the Chrome extension (`chrome-extension/background.js`)
and the daemon's CDP proxy. When `backend=extension` is active in Mode B,
this server replaces the conventional upstream-ws-to-Chrome path: the
"upstream" is the relay + the extension's `chrome.debugger` calls.

Protocol on the wire (extension ↔ daemon, all JSON text frames):

  daemon → extension:
    {"type":"command","id":N,"tabId":42,"method":"Page.navigate","params":{...}}
    {"type":"detach","id":N,"tabId":42}

  extension → daemon:
    {"type":"hello","installId":"...","browser":"chrome","version":"1.2.3"}
    {"type":"response","id":N,"result":{...}}
    {"type":"response","id":N,"error":{"code":-32000,"message":"..."}}
    {"type":"attached","tabId":42,"targetInfo":{"url":"...","title":"..."}}
    {"type":"detached","tabId":42}
    {"type":"event","tabId":42,"method":"Page.frameNavigated","params":{...}}

Design points:

- **Anti-CSRF** (§A.4 OpenCLI borrow): web-page Origins on the ws upgrade
  are refused with HTTP 403. Drive-by browser pages can issue cross-origin
  ws upgrades unless we filter — Origin is the only header browsers can't
  lie about for ws. `chrome-extension://...` Origins are allowed (Chrome
  MV3 SW does emit one on connect — earlier docs claimed otherwise; real-
  world Chrome 144+ proves it does). Missing Origin is also allowed: that
  shape only comes from non-browser tooling (curl, raw ws clients) that
  can't be exploited through a drive-by page.
- **HTTP /__status__** doctor hook: `GET http://127.0.0.1:19989/__status__`
  returns `{"running":true,"extensions":N,"installIds":[...]}` so the v0.1
  doctor probe can answer `available=true` without opening a ws.
- **3-retry `chrome.debugger` conflict** (§A.4 OpenCLI borrow): when the
  extension responds with `error.message` containing "already attached"
  (DevTools, another extension), the relay's `send_command` retries up to
  3 times with exponential backoff. Surfacing the final failure is the
  caller's job.
- **Ghost targets** (spec §8.4): the relay tracks which tabs the user has
  attached via the popup; the daemon's router answers `Target.getTargets`
  from this list when the extension backend is active.

The relay is intentionally **synchronous and explicit**: no auto-attach,
no retry framework, no plugin system. It mirrors the daemon's core ethos.
"""
from __future__ import annotations

import asyncio
import contextlib
import http
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from browserwright.version import (
    EXTENSION_PROTOCOL_VERSION,
    VersionDrift,
    __version__,
    compare_versions,
)

logger = logging.getLogger(__name__)


# Spec §8.4: default relay port is 19989. Originally we mirrored playwriter's
# 19988 (`playwriter/src/cdp-relay.ts:71-90`) to ride its conflict-awareness
# convention; in practice users run both daemons side-by-side, so we shifted
# one port up to coexist. Tests can override via `RelayServer(port=0)` to
# bind an ephemeral port.
DEFAULT_RELAY_PORT = 19989

# Spec §A.4: OpenCLI `extension/src/cdp.ts:96-150` retries 3 times when
# chrome.debugger.attach fails with "Another debugger is already attached".
# We mirror the same cadence — keeps the user-visible retry feel consistent
# with the playwriter / OpenCLI experience.
ATTACH_RETRY_LIMIT = 3
ATTACH_RETRY_BACKOFF = (0.1, 0.3, 0.8)  # seconds; len must equal ATTACH_RETRY_LIMIT
APP_PING_INTERVAL = 5.0
STALE_FRAME_AFTER = 30.0
RECONNECT_WAIT_TIMEOUT = 35.0


@dataclass
class GhostTarget:
    """One user-attached tab visible as a CDP target.

    `target_id` is daemon-fabricated (we use `ext-tab-<tabId>`) so the regular
    router session/attacher tables don't need extension-specific code.
    """
    target_id: str
    tab_id: int
    url: str = ""
    title: str = ""
    type: str = "page"
    install_id: str = ""  # which extension owns this tab — for multi-extension support


@dataclass
class _InflightCall:
    """What one entry of ``_ExtensionConn.pending`` is *for*, and since when.

    ``pending`` itself stays a bare ``dict[int, Future]`` (several tests hand-
    insert futures into it, and a Future is all the resolve path needs). This
    parallel table carries the operator-facing answer — "which call, how long"
    — so a stuck relay hop can name itself in `browserwright-daemon ps` instead
    of showing up as an anonymous count. Entries are best-effort: a future
    inserted directly into ``pending`` simply has no meta row.
    """

    id: int
    kind: str                    # the app-level `type` (command / createTab / …)
    method: str = ""             # CDP method, for `type == "command"`
    tab_id: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    attempt: int = 1             # 2 on a post-reconnect retry

    def elapsed_s(self, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.started_at)

    def describe(self, *, now: float | None = None) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "method": self.method,
            "tab_id": self.tab_id,
            "attempt": self.attempt,
            "elapsed_s": round(self.elapsed_s(now=now), 3),
        }


def _oldest_pending_s(ext: "_ExtensionConn") -> float | None:
    """Age of this connection's longest-outstanding relay call, or None when it
    is idle (or when every pending future was inserted without meta)."""
    metas = getattr(ext, "pending_meta", None)
    if not metas:
        return None
    now = time.monotonic()
    return round(max(m.elapsed_s(now=now) for m in metas.values()), 3)


def _inflight_from_body(body: dict, cmd_id: int, *, attempt: int = 1) -> _InflightCall:
    tab_id = body.get("tabId")
    return _InflightCall(
        id=cmd_id,
        kind=str(body.get("type") or "?"),
        method=str(body.get("method") or ""),
        tab_id=tab_id if isinstance(tab_id, int) else None,
        attempt=attempt,
    )


@dataclass
class _ExtensionConn:
    """One connected extension. v0.4 supports multiple in theory (e.g., user
    runs Chrome + Edge with the extension installed in both); the daemon
    fans commands out to whichever extension owns the target by `install_id`.
    """
    conn: ServerConnection
    install_id: str = ""
    browser: str = ""
    version: str = ""
    browserwright_version: str = ""
    extension_protocol_version: str = ""
    version_drift: str = VersionDrift.UNKNOWN.value
    hello_received: asyncio.Event = field(default_factory=asyncio.Event)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    #: Same keys as `pending`, describing what each awaited call is (see
    #: `_InflightCall`). Maintained only by `_request` / the retry path.
    pending_meta: dict[int, _InflightCall] = field(default_factory=dict)
    tabs: dict[int, GhostTarget] = field(default_factory=dict)
    last_frame_ts: float = field(default_factory=time.monotonic)
    app_ping_task: asyncio.Task | None = None


class RelayServer:
    """ws://127.0.0.1:19989 — extension talks to us here.

    Lifecycle: `start()` binds; `wait_ready(timeout)` blocks until at least
    one extension has sent `hello`; `stop()` closes everything cleanly.
    """

    def __init__(self, *, port: int = DEFAULT_RELAY_PORT,
                 host: str = "127.0.0.1"):
        self._port = port
        self._host = host
        self._server: Any = None
        self._extensions: dict[str, _ExtensionConn] = {}
        # Monotonic connection epoch. A fresh extension hello may represent a
        # Chrome restart, where numeric tab/group ids can be recycled. Session
        # adapters use this to demote in-memory group bindings back to
        # ownership-checked recovery candidates after every reconnect.
        self._connection_generation: int = 0
        self._next_cmd_id: int = 1
        self._first_ready = asyncio.Event()
        # Hook: every event-frame from any extension gets called back here so
        # the daemon's CDP proxy can route it. Set by the listener.
        self._on_event: Callable[[dict], Awaitable[None]] | None = None
        # Task #tab-handle-model PR2: the Playwright facade needs to observe the
        # SAME extension event stream as the agent path (so it can translate
        # `Page.frameNavigated` etc. into per-Playwright-session frames), but the
        # single `_on_event` slot is already claimed by the agent's
        # ExtensionUpstream. We keep a fan-out set of ADDITIONAL listeners that
        # the relay calls alongside `_on_event` — the facade registers/removes
        # itself here per connection without disturbing the agent handler.
        self._event_listeners: set[Callable[[dict], Awaitable[None]]] = set()
        self._session_announce_events: dict[str, asyncio.Event] = {}
        self._reload_attempts: set[tuple[str, str, str]] = set()

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> int:
        """Bind the relay. Returns the actual port (useful with port=0)."""
        self._server = await serve(
            self._handler,
            self._host,
            self._port,
            process_request=self._process_request,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,  # screenshots can exceed the 1 MiB default
        )
        # Discover the actually-bound port (for port=0 tests).
        for sock in self._server.sockets:
            sa = sock.getsockname()
            if isinstance(sa, tuple) and len(sa) >= 2:
                self._port = sa[1]
                break
        logger.info("extension relay listening on ws://%s:%d",
                    self._host, self._port)
        return self._port

    async def stop(self) -> None:
        if self._server is None:
            return
        # Cancel every pending command future so callers see a clean error.
        for ext in list(self._extensions.values()):
            for fut in list(ext.pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("relay shutting down"))
            try:
                await ext.conn.close(code=1001, reason="relay shutdown")
            except Exception:
                pass
        self._extensions.clear()
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    async def wait_ready(self, timeout: float = 30.0) -> None:
        """Block until at least one extension has sent its `hello`."""
        await asyncio.wait_for(self._first_ready.wait(), timeout=timeout)

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_ready(self) -> bool:
        return any(e.hello_received.is_set() for e in self._extensions.values())

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def _require_generation(self, expected_generation: int | None) -> None:
        """Refuse a destructive command after its ownership epoch changed."""
        if (expected_generation is not None
                and expected_generation != self._connection_generation):
            raise ConnectionError(
                "extension generation changed after ownership validation; "
                "destructive request was not sent")

    def set_event_handler(
        self, handler: Callable[[dict], Awaitable[None]] | None,
    ) -> None:
        """Register THE primary coroutine that receives every async event from
        the extension (`Page.frameNavigated` etc). The daemon's router uses this
        to translate extension events back into CDP frames for clients.

        This is single-slot (the agent path). Secondary observers (the
        Playwright facade) use `add_event_listener` / `remove_event_listener`.
        """
        self._on_event = handler

    def add_event_listener(
        self, handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Register an ADDITIONAL fan-out observer of the extension event
        stream (Task #tab-handle-model PR2). Called alongside the primary
        `_on_event` handler — used by the Playwright facade so it sees the same
        `attached`/`event` stream the agent path does without stealing the
        single primary slot."""
        self._event_listeners.add(handler)

    def remove_event_listener(
        self, handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Drop a fan-out observer (facade disconnect/stop). Idempotent."""
        self._event_listeners.discard(handler)

    def reset_session_announce(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._session_announce_events.setdefault(session_id, asyncio.Event()).clear()

    def set_session_announce(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._session_announce_events.setdefault(session_id, asyncio.Event()).set()

    async def wait_session_announce(self, session_id: str,
                                    timeout: float = 2.0) -> bool:
        event = self._session_announce_events.setdefault(
            session_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
            return True
        except asyncio.TimeoutError:
            return False

    # ---- public command API (used by extension upstream wrapper) ---------

    def list_ghost_targets(self) -> list[GhostTarget]:
        """All currently-attached tabs across every extension."""
        out: list[GhostTarget] = []
        for ext in self._extensions.values():
            out.extend(ext.tabs.values())
        return out

    async def query_group_tabs(self, group_name: str | None = None, *,
                               group_id: int | None = None,
                               timeout: float = 15.0) -> dict | None:
        """Live membership query: ask the extension for the tabs of the
        session's tab group. ``group_id`` is the durable primary key (the
        numeric Chrome groupId); ``group_name`` is accepted for older callers
        but is not a lookup key because titles are not unique. Returns
        ``{"groupId":int,"tabs":
        [{tabId,url,title,active,lastAccessed}, ...]}`` — ``groupId == -1`` /
        empty tabs when no group matches (the session's browser has no tabs).
        Returns None when no extension is connected (caller falls back)."""
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        ext = self._pick_active_extension()
        if ext is None:
            return None
        ext = await self._ensure_extension_fresh(
            ext, timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        if ext is None:
            return None
        body: dict = {"type": "queryGroup"}
        if group_name:
            body["groupName"] = group_name
        if isinstance(group_id, int) and group_id >= 0:
            body["groupId"] = group_id
        return await self._request(
            ext, body,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            replay_safe=True)

    async def attach_active_tab(self, *,
                                group_name: str | None = None,
                                group_id: int | None = None,
                                expected_generation: int | None = None,
                                timeout: float = 10.0,
                                session_id: str | None = None) -> GhostTarget:
        """Daemon-driven adopt (docs C1): ask the extension to MOVE Chrome's
        currently-focused-window active tab into this session's tab group and
        attach the debugger. ``group_id`` identifies the destination group;
        ``group_name`` is only the title to apply if a new group is created.
        The extension refuses (error) if the focused tab already belongs to a
        DIFFERENT session's group.

        The adopted tab is a regular group member — it closes with the group on
        ``end_session`` (no separate borrowed/owned flag).

        Retries on "already attached" the same way `attach_tab` does. Returns
        the GhostTarget (with a ``group_id`` attribute) once the extension
        confirms. The extension also emits `attached`, so the ghost ends up in
        `ext.tabs` for the regular routing path.
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        ext = await self._ensure_extension_fresh_or_raise(ext, timeout=timeout)
        self._require_generation(expected_generation)
        last_err: Exception | None = None
        body: dict = {"type": "attachActive"}
        if group_name:
            body["groupName"] = group_name
        if isinstance(group_id, int) and group_id >= 0:
            body["groupId"] = group_id
        # Issue #29: the session id lets the extension stamp the adopted tab
        # with its per-tab ownership marker (chrome.storage.session).
        if session_id:
            body["sessionId"] = session_id
        for i in range(ATTACH_RETRY_LIMIT):
            try:
                result = await self._request(ext, body, timeout=timeout)
                info = result or {}
                tab_id_raw = info.get("tabId")
                if not isinstance(tab_id_raw, int):
                    raise RuntimeError(
                        f"attachActive response missing tabId: {info!r}")
                gt = GhostTarget(
                    target_id=f"ext-tab-{tab_id_raw}",
                    tab_id=tab_id_raw,
                    url=str(info.get("url", "")),
                    title=str(info.get("title", "")),
                    install_id=ext.install_id,
                )
                try:
                    gt.group_id = int(info.get("groupId", -1))  # type: ignore[attr-defined]
                except (TypeError, ValueError):
                    gt.group_id = -1  # type: ignore[attr-defined]
                ext.tabs[tab_id_raw] = gt
                return gt
            except _CommandError as e:
                last_err = e
                if "already attached" not in (e.message or "").lower():
                    raise
                await asyncio.sleep(ATTACH_RETRY_BACKOFF[i])
        raise last_err if last_err is not None else RuntimeError(
            "attach active failed (no error captured)")

    async def attach_tab(self, tab_id: int, *,
                         expected_generation: int | None = None,
                         timeout: float = 5.0) -> GhostTarget:
        """Tell the extension to `chrome.debugger.attach({tabId})`. Retries
        up to ATTACH_RETRY_LIMIT on "already attached" errors.

        Returns the GhostTarget once the extension confirms.
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        ext = await self._ensure_extension_fresh_or_raise(ext, timeout=timeout)
        self._require_generation(expected_generation)
        # Idempotency: extension may already hold chrome.debugger.attach on
        # this tab (popup click, prior daemon lifecycle — the SW survives
        # daemon restarts and re-announces attached tabs on reconnect, so
        # ext.tabs is authoritative). Skip the redundant attach call to
        # avoid "Another debugger is already attached" from Chrome.
        existing = ext.tabs.get(tab_id)
        if existing is not None:
            return existing
        last_err: Exception | None = None
        for i in range(ATTACH_RETRY_LIMIT):
            try:
                result = await self._request(
                    ext, {"type": "attach", "tabId": tab_id}, timeout=timeout)
                # Result shape: {"targetInfo": {...}}
                info = (result or {}).get("targetInfo") or {}
                gt = GhostTarget(
                    target_id=f"ext-tab-{tab_id}",
                    tab_id=tab_id,
                    url=str(info.get("url", "")),
                    title=str(info.get("title", "")),
                    install_id=ext.install_id,
                )
                ext.tabs[tab_id] = gt
                return gt
            except _CommandError as e:
                last_err = e
                if "already attached" not in (e.message or "").lower():
                    raise
                await asyncio.sleep(ATTACH_RETRY_BACKOFF[i])
        # Exhausted retries.
        raise last_err if last_err is not None else RuntimeError(
            "attach failed (no error captured)")

    async def detach_tab(self, tab_id: int, *,
                         timeout: float = 5.0) -> None:
        ext = self._extension_for_tab(tab_id)
        if ext is None:
            return
        ext = await self._ensure_extension_fresh(ext, timeout=timeout)
        if ext is None:
            return
        try:
            await self._request(
                ext, {"type": "detach", "tabId": tab_id}, timeout=timeout)
        except Exception as e:
            logger.warning("detach(tab=%d) failed: %r", tab_id, e)
        ext.tabs.pop(tab_id, None)

    async def create_background_tab(
        self,
        url: str,
        *,
        group_name: str | None = "Agent",
        group_id: int | None = None,
        background: bool = True,
        skip_post_attach_commands: bool = False,
        expected_generation: int | None = None,
        timeout: float = 10.0,
        session_id: str | None = None,
    ) -> GhostTarget:
        """Spec Phase B Feature 1: open a tab in the background (active=false)
        in the session's tab group, attach ``chrome.debugger`` to it, and
        return a GhostTarget bound to the new tab. The user's currently-active
        tab keeps focus.

        The session's group is identified by ``group_id`` (the durable numeric
        Chrome groupId) when known; ``group_name`` (= session name) is only the
        human-visible title to use when a new group must be created. The
        extension resolves by id, or creates a new group when the id is absent
        or invalid.

        ``group_name=None`` and no ``group_id`` skips the grouping step; the
        resulting GhostTarget carries the extension-reported ``group_id``
        (which may be ``-1`` when no group was requested or grouping failed in
        a recoverable way).
        """
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        ext = await self._ensure_extension_fresh_or_raise(
            ext, timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        self._require_generation(expected_generation)
        body: dict = {"type": "createTab", "url": url}
        if group_name:
            body["groupName"] = group_name
        if isinstance(group_id, int) and group_id >= 0:
            body["groupId"] = group_id
        # Issue #29: the session id lets the extension stamp the new tab with
        # its per-tab ownership marker (chrome.storage.session) — the durable
        # anchor that replaces the title/groupId heuristic.
        if session_id:
            body["sessionId"] = session_id
        # background=False opens the tab in the foreground (active:true);
        # default True keeps the user's focus tab. Only sent when foreground
        # is requested so existing extensions default to background.
        if not background:
            body["background"] = False
        if skip_post_attach_commands:
            body["skipPostAttachCommands"] = True
        result = await self._request(
            ext, body,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time())) or {}
        tab_id = int(result.get("tabId", -1))
        if tab_id < 0:
            raise RuntimeError(
                f"extension createTab returned invalid tabId: {result!r}")
        gt = GhostTarget(
            target_id=f"ext-tab-{tab_id}",
            tab_id=tab_id,
            url=str(result.get("url", url)),
            title=str(result.get("title", "")),
            install_id=ext.install_id,
        )
        # Stash a group_id attribute on the dataclass instance for callers
        # that want to expose it (we don't widen GhostTarget's dataclass
        # shape — using object.__setattr__ keeps the schema-locked fields
        # frozen for everyone else).
        try:
            gt.group_id = int(result.get("groupId", -1))  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            gt.group_id = -1  # type: ignore[attr-defined]
        ext.tabs[tab_id] = gt
        return gt

    async def close_tab(self, tab_id: int, *,
                        expected_generation: int | None = None,
                        timeout: float = 5.0) -> None:
        """Spec Phase B Feature 2: close a tab via chrome.tabs.remove (not a
        debugger detach). Clears the ghost-target entry only after the
        extension confirms Chrome removed the tab.

        Raises if no extension is connected at all — silently returning
        success here would lie to callers about a close that never went
        over the wire. `_extension_for_tab` already falls back to any ready
        extension if no ext owns the tab (race between popup attach and
        ghost registration), so a None return means "no extension exists"
        rather than "no extension owns this specific tab id".
        """
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        ext = self._extension_for_tab(tab_id)
        if ext is None:
            raise RuntimeError(f"no extension knows tab {tab_id}")
        ext = await self._ensure_extension_fresh_or_raise(
            ext, timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        self._require_generation(expected_generation)
        await self._request(
            ext, {"type": "closeTab", "tabId": tab_id},
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        ext.tabs.pop(tab_id, None)

    async def send_cdp(self, tab_id: int, method: str, params: dict,
                       *, timeout: float = 10.0) -> dict:
        """Forward a CDP method+params through the extension's
        `chrome.debugger.sendCommand(tabId, method, params)`.

        Timeout pairing with the extension (chrome-extension/background.js,
        "bounded chrome.debugger calls"): the extension bounds each
        chrome.debugger call BELOW this wait — sendCommand 9000ms vs 10.0s,
        attach/detach 3000ms vs the 5.0s attach/detach waits — and answers
        with an error frame carrying code -32001 when its budget expires, so
        this future normally settles with a distinguishable `_CommandError`
        instead of a bare `asyncio.TimeoutError`. This timeout is the
        last-resort net for a wedged extension, not the primary bound; a test
        locks the two sides' agreement.
        """
        ext = self._extension_for_tab(tab_id)
        if ext is None:
            raise RuntimeError(f"no extension owns tab {tab_id}")
        ext = await self._ensure_extension_fresh_or_raise(ext, timeout=timeout)
        return await self._request(ext, {
            "type": "command",
            "tabId": tab_id,
            "method": method,
            "params": params,
        }, timeout=timeout) or {}

    async def userscript_request(self, verb: str, payload: dict,
                                 *, timeout: float = 5.0,
                                 **kwargs: Any) -> dict | None:
        """Forward a userscript control request to any ready extension.

        Userscript operations are extension-global rather than tab-scoped, so
        unlike ``send_cdp`` they only need a connected extension, not a tab
        owner.

        ``session_ids`` (sent by the verb layer to scope installs on the
        raw-CDP backend, where the workspace is the browser instance) is
        accepted and dropped here: the extension owns the scripts, the
        extension's ``background.js`` dispatch reads no session ids, and the
        message must not carry keys it does not understand.
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        ext = await self._ensure_extension_fresh_or_raise(ext, timeout=timeout)
        return await self._request(
            ext, {"type": f"userscript.{verb}", **payload}, timeout=timeout)

    def inflight_snapshot(self) -> list[dict]:
        """Every relay call currently awaiting an extension response.

        This is the hop the original diagnosis could not see: a future left in
        `_ExtensionConn.pending` forever produced no log line, no `/__status__`
        change, and no error — the daemon just went quiet. One row per awaited
        call, newest last, each carrying how long it has been outstanding.

        Read-only and allocation-cheap; safe to call from an RPC handler.
        """
        now = time.monotonic()
        rows: list[dict] = []
        for ext in self._extensions.values():
            for cmd_id, fut in sorted(ext.pending.items()):
                meta = ext.pending_meta.get(cmd_id)
                row = (meta.describe(now=now) if meta is not None
                       else {"id": cmd_id, "kind": "?", "method": "",
                             "tab_id": None, "attempt": 1, "elapsed_s": None})
                row["install_id"] = ext.install_id
                row["done"] = fut.done()
                rows.append(row)
        rows.sort(key=lambda r: (r["elapsed_s"] is None, -(r["elapsed_s"] or 0.0)))
        return rows

    def status_payload(self) -> dict:
        extensions = [
            self._extension_status(ext)
            for ext in self._extensions.values()
            if ext.hello_received.is_set()
        ]
        return {
            "running": True,
            "extensions": len(extensions),
            "install_ids": [e["install_id"] for e in extensions],
            "daemon_version": __version__,
            "extension_protocol_version": EXTENSION_PROTOCOL_VERSION,
            "extension_details": extensions,
            "tab_count": sum(len(e.tabs) for e in self._extensions.values()),
        }

    async def reload_extensions(
        self,
        *,
        reason: str = "manual",
        expected_version: str | None = None,
    ) -> dict:
        """Ask every connected extension to reload from disk immediately.

        ``chrome.runtime.reload()`` tears down the service worker, so this is a
        best-effort one-way message rather than a request/response round trip.
        """
        details: list[dict] = []
        for ext in list(self._extensions.values()):
            if not ext.hello_received.is_set():
                continue
            ok = await self._send_reload_extension(
                ext,
                reason=reason,
                expected_version=expected_version or __version__,
                guard=False,
            )
            details.append({
                "install_id": ext.install_id,
                "browser": ext.browser,
                "version": ext.browserwright_version or ext.version,
                "sent": ok,
            })
        return {
            "ok": True,
            "sent": sum(1 for item in details if item["sent"]),
            "extensions": details,
        }

    # ---- internals -------------------------------------------------------

    def _extension_status(self, ext: _ExtensionConn) -> dict:
        ext_protocol = getattr(ext, "extension_protocol_version", "")
        ext_version = getattr(ext, "version", "")
        ext_browserwright_version = getattr(ext, "browserwright_version", "") or ext_version
        protocol_compatible = (
            ext_protocol in ("", EXTENSION_PROTOCOL_VERSION)
        )
        comparison = compare_versions(ext_browserwright_version, __version__)
        recorded_drift = getattr(ext, "version_drift", VersionDrift.UNKNOWN.value)
        drift = (
            recorded_drift
            if recorded_drift != VersionDrift.UNKNOWN.value
            else comparison.drift.value
        )
        app_compatible = comparison.compatible
        return {
            "install_id": getattr(ext, "install_id", ""),
            "browser": getattr(ext, "browser", ""),
            "version": ext_version,
            "browserwright_version": ext_browserwright_version,
            "daemon_version": __version__,
            "extension_protocol_version": ext_protocol,
            "compatible": protocol_compatible and app_compatible,
            "protocol_compatible": protocol_compatible,
            "app_compatible": app_compatible,
            "version_drift": drift,
            # How many relay calls this connection is currently awaiting. The
            # diagnosis that motivated C1 had exactly one stuck entry here while
            # `/__status__` reported a perfectly healthy `extensions=1`.
            "pending": len(getattr(ext, "pending", ())),
            "oldest_pending_s": _oldest_pending_s(ext),
        }

    async def _send_reload_extension(
        self,
        ext: _ExtensionConn,
        *,
        reason: str,
        expected_version: str,
        guard: bool,
    ) -> bool:
        ext_version = ext.browserwright_version or ext.version
        key = (ext.install_id, ext_version, expected_version)
        if guard and key in self._reload_attempts:
            logger.warning(
                "extension version drift persists after reload attempt: "
                "install_id=%s extension=%s daemon=%s",
                ext.install_id,
                ext_version,
                expected_version,
            )
            return False
        if guard:
            self._reload_attempts.add(key)
        try:
            await ext.conn.send(json.dumps({
                "type": "reloadExtension",
                "reason": reason,
                "expectedVersion": expected_version,
            }))
            logger.info(
                "requested extension reload: install_id=%s reason=%s "
                "extension=%s daemon=%s",
                ext.install_id,
                reason,
                ext_version,
                expected_version,
            )
            return True
        except Exception as e:  # noqa: BLE001 - explicit reload is best-effort.
            logger.warning(
                "extension reload request failed: install_id=%s error=%r",
                ext.install_id,
                e,
            )
            return False

    async def _maybe_reload_for_version_drift(self, ext: _ExtensionConn) -> None:
        comparison = compare_versions(ext.browserwright_version or ext.version, __version__)
        if comparison.drift in {VersionDrift.EQUAL, VersionDrift.UNKNOWN}:
            return
        if comparison.order is None or comparison.order >= 0:
            return
        await self._send_reload_extension(
            ext,
            reason="version_drift",
            expected_version=__version__,
            guard=True,
        )

    def _pick_active_extension(self) -> _ExtensionConn | None:
        for ext in self._extensions.values():
            if ext.hello_received.is_set():
                return ext
        return None

    def _extension_for_tab(self, tab_id: int) -> _ExtensionConn | None:
        for ext in self._extensions.values():
            if tab_id in ext.tabs:
                return ext
        # Fall back to any ready extension — for tabs the extension is about
        # to attach to (race between popup click and ghost registration).
        return self._pick_active_extension()

    def _alloc_id(self) -> int:
        v = self._next_cmd_id
        self._next_cmd_id += 1
        return v

    def _extension_is_stale(self, ext: _ExtensionConn) -> bool:
        if not ext.hello_received.is_set():
            return False
        return (time.monotonic() - ext.last_frame_ts) > STALE_FRAME_AFTER

    async def _ensure_extension_fresh(
        self, ext: _ExtensionConn, *, timeout: float | None = None,
    ) -> _ExtensionConn | None:
        """Return a live extension connection, force-closing ghost sockets.

        MV3 can suspend the SW while Chrome's network process keeps the TCP
        websocket ESTABLISHED. Protocol pings still succeed there, but no app
        frames arrive. The daemon treats missing app frames as authoritative
        and tears down the ghost before sending a user command.
        """
        if not self._extension_is_stale(ext):
            return ext
        deadline = (
            None if timeout is None
            else asyncio.get_running_loop().time() + max(0.0, timeout))
        await self._force_close_extension(ext, reason="stale app-level heartbeat")
        wait_timeout = RECONNECT_WAIT_TIMEOUT
        if deadline is not None:
            wait_timeout = min(
                wait_timeout,
                max(0.0, deadline - asyncio.get_running_loop().time()))
        return await self._wait_for_replacement(ext, timeout=wait_timeout)

    async def _ensure_extension_fresh_or_raise(
        self, ext: _ExtensionConn, *, timeout: float | None = None,
    ) -> _ExtensionConn:
        fresh = await self._ensure_extension_fresh(ext, timeout=timeout)
        wait_timeout = (
            RECONNECT_WAIT_TIMEOUT if timeout is None
            else min(RECONNECT_WAIT_TIMEOUT, max(0.0, timeout)))
        if fresh is None:
            raise RuntimeError(
                "extension relay connection appears stale and did not reconnect "
                f"within {wait_timeout:.3g}s")
        if fresh is not ext:
            raise ConnectionError(
                "extension reconnected before a non-replayable request; "
                "request was not sent on the replacement connection and the "
                "caller must revalidate ownership")
        return fresh

    async def _force_close_extension(self, ext: _ExtensionConn, *, reason: str) -> None:
        logger.warning(
            "force-closing stale extension relay connection: install_id=%s reason=%s",
            ext.install_id or "(pending)",
            reason,
        )
        for fut in list(ext.pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError(f"extension relay closed: {reason}"))
            if fut.cancelled():
                continue
            with contextlib.suppress(BaseException):
                fut.exception()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ext.conn.close(code=1011, reason=reason),
                timeout=1.0,
            )

    async def _wait_for_replacement(
        self, old_ext: _ExtensionConn, *, timeout: float,
        allow_any_install: bool = False,
    ) -> _ExtensionConn | None:
        deadline = time.monotonic() + max(0.0, timeout)
        install_id = old_ext.install_id
        while time.monotonic() < deadline:
            candidates = [
                ext for ext in self._extensions.values()
                if ext is not old_ext and ext.hello_received.is_set()
            ]
            if install_id:
                for ext in candidates:
                    if ext.install_id == install_id:
                        return ext
            if allow_any_install and candidates:
                return candidates[0]
            await asyncio.sleep(0.1)
        if install_id:
            return None
        if candidates := [
            ext for ext in self._extensions.values()
            if ext is not old_ext and ext.hello_received.is_set()
        ]:
            return candidates[0]
        return None

    async def _retry_request_on_replacement(
        self,
        ext: _ExtensionConn,
        body: dict,
        *,
        timeout: float,
        loop: asyncio.AbstractEventLoop,
    ) -> dict | None:
        deadline = loop.time() + max(0.0, timeout)
        await self._force_close_extension(ext, reason=f"{body.get('type')} request failed")
        remaining = max(0.0, deadline - loop.time())
        replacement = await self._wait_for_replacement(
            ext, timeout=min(RECONNECT_WAIT_TIMEOUT, remaining))
        if replacement is None:
            raise ConnectionError(
                "extension relay did not reconnect after request failure")
        retry_id = self._alloc_id()
        retry_body = {**{k: v for k, v in body.items() if k != "id"}, "id": retry_id}
        retry_fut: asyncio.Future = loop.create_future()
        replacement.pending[retry_id] = retry_fut
        replacement.pending_meta[retry_id] = _inflight_from_body(
            retry_body, retry_id, attempt=2)
        try:
            remaining = max(0.0, deadline - loop.time())
            await asyncio.wait_for(
                replacement.conn.send(json.dumps(retry_body)),
                timeout=remaining)
            remaining = max(0.0, deadline - loop.time())
            return await asyncio.wait_for(retry_fut, timeout=remaining)
        finally:
            replacement.pending.pop(retry_id, None)
            replacement.pending_meta.pop(retry_id, None)

    async def _request(
        self, ext: _ExtensionConn, body: dict, *, timeout: float,
        replay_safe: bool = False,
    ) -> dict | None:
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        cmd_id = self._alloc_id()
        body = {**body, "id": cmd_id}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        ext.pending[cmd_id] = fut
        ext.pending_meta[cmd_id] = _inflight_from_body(body, cmd_id)
        try:
            remaining = max(0.0, deadline - loop.time())
            await asyncio.wait_for(
                ext.conn.send(json.dumps(body)), timeout=remaining)
            remaining = max(0.0, deadline - loop.time())
            return await asyncio.wait_for(fut, timeout=remaining)
        except asyncio.TimeoutError:
            if not self._extension_is_stale(ext):
                raise
            if not replay_safe:
                await self._force_close_extension(
                    ext, reason=f"{body.get('type')} outcome unknown")
                raise ConnectionError(
                    "non-replayable extension request lost its connection; "
                    "outcome is unknown, request was not retried, and the "
                    "caller must revalidate ownership")
            return await self._retry_request_on_replacement(
                ext, body, timeout=max(0.0, deadline - loop.time()), loop=loop)
        except (ConnectionError, websockets.exceptions.ConnectionClosed):
            if not replay_safe:
                await self._force_close_extension(
                    ext, reason=f"{body.get('type')} outcome unknown")
                raise ConnectionError(
                    "non-replayable extension request lost its connection; "
                    "outcome is unknown, request was not retried, and the "
                    "caller must revalidate ownership")
            return await self._retry_request_on_replacement(
                ext, body, timeout=max(0.0, deadline - loop.time()), loop=loop)
        finally:
            if not fut.cancelled():
                with contextlib.suppress(BaseException):
                    fut.exception()
            ext.pending.pop(cmd_id, None)
            ext.pending_meta.pop(cmd_id, None)

    # ---- ws handlers -----------------------------------------------------

    def _process_request(self, conn: ServerConnection, request) -> Any:
        """Intercept the HTTP handshake before upgrade.

        - `GET /__status__` answered as JSON (doctor probe hook).
        - Web-page `Origin` header → 403 (anti-CSRF, OpenCLI borrow).
        - `Origin: chrome-extension://<id>` → allowed. NOTE: this admits ANY
          extension installed in the user's Chrome profile, not just ours.
          We rely on (a) the daemon binding to 127.0.0.1 (local-only) and
          (b) the user-trusted extension install model. A malicious
          extension on the same profile already has `chrome.debugger`
          primitives strictly more powerful than what the relay exposes,
          so admitting unknown-id extension Origins here doesn't widen the
          attack surface beyond what the user already implicitly trusts.
        - Missing Origin → allowed (curl, raw ws clients — not exploitable
          via drive-by page since the browser would always set Origin).
        """
        path = request.path or "/"
        if path.startswith("/__status__"):
            body = json.dumps(self.status_payload())
            resp = conn.respond(http.HTTPStatus.OK, body)
            resp.headers["Content-Type"] = "application/json"
            return resp

        # Anti-CSRF: refuse web-page Origins. Allow Origin: chrome-extension://*
        # — note this admits ANY extension installed in the user's profile, not
        # just ours. We rely on the daemon binding to 127.0.0.1 + the user-
        # trusted extension install model. A malicious extension on the same
        # profile already has chrome.debugger primitives strictly more powerful
        # than what the relay exposes. Chrome MV3 SW does emit Origin
        # (chrome-extension://<id>) on ws upgrades from Chrome 144+ — earlier
        # comments here claimed otherwise and 403'd legitimate extension
        # connections. We allow that prefix and 403 anything else non-empty.
        origin = request.headers.get("Origin", "") or request.headers.get("origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            resp = conn.respond(
                http.HTTPStatus.FORBIDDEN,
                "extension relay refuses non-extension Origin (anti-CSRF)\n",
            )
            return resp
        return None  # allow upgrade

    async def _handler(self, conn: ServerConnection) -> None:
        ext = _ExtensionConn(conn=conn)
        # Use the conn's id() as a temp key until hello arrives.
        temp_key = f"_pending-{id(conn)}"
        self._extensions[temp_key] = ext
        try:
            async for raw in conn:
                ext.last_frame_ts = time.monotonic()
                if not isinstance(raw, (str, bytes)):
                    continue
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(text)
                except (ValueError, TypeError):
                    logger.warning("extension sent non-JSON: %s", text[:80])
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch_from_extension(ext, temp_key, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.warning("extension handler crashed: %r", e)
        finally:
            key = ext.install_id or temp_key
            if self._extensions.get(key) is ext:
                self._extensions.pop(key, None)
            if self._extensions.get(temp_key) is ext:
                self._extensions.pop(temp_key, None)
            if ext.app_ping_task is not None:
                ext.app_ping_task.cancel()
            for fut in list(ext.pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("extension disconnected"))

    async def _dispatch_from_extension(self, ext: _ExtensionConn,
                                       temp_key: str, msg: dict) -> None:
        kind = msg.get("type")

        if kind == "hello":
            self._connection_generation += 1
            ext.install_id = str(msg.get("installId") or "")
            ext.browser = str(msg.get("browser") or "")
            ext.version = str(msg.get("version") or "")
            ext.browserwright_version = str(msg.get("browserwrightVersion") or ext.version)
            ext.extension_protocol_version = str(
                msg.get("extensionProtocolVersion") or ""
            )
            comparison = compare_versions(ext.browserwright_version or ext.version, __version__)
            ext.version_drift = comparison.drift.value
            # Re-key the extension by install_id (so multiple extensions don't
            # collide on temp_key collisions).
            self._extensions.pop(temp_key, None)
            self._extensions[ext.install_id or temp_key] = ext
            ext.hello_received.set()
            if ext.app_ping_task is None or ext.app_ping_task.done():
                ext.app_ping_task = asyncio.create_task(self._app_ping_loop(ext))
            self._first_ready.set()
            if (
                ext.extension_protocol_version
                and ext.extension_protocol_version != EXTENSION_PROTOCOL_VERSION
            ):
                logger.warning(
                    "extension protocol mismatch: install_id=%s extension=%s daemon=%s",
                    ext.install_id,
                    ext.extension_protocol_version,
                    EXTENSION_PROTOCOL_VERSION,
                )
            if comparison.drift == VersionDrift.PATCH:
                logger.info(
                    "extension version patch drift: install_id=%s extension=%s daemon=%s",
                    ext.install_id,
                    ext.browserwright_version or ext.version,
                    __version__,
                )
            elif comparison.drift in {VersionDrift.MINOR, VersionDrift.MAJOR}:
                logger.warning(
                    "extension version %s drift: install_id=%s extension=%s daemon=%s",
                    comparison.drift.value,
                    ext.install_id,
                    ext.browserwright_version or ext.version,
                    __version__,
                )
            logger.info(
                "extension hello: install_id=%s browser=%s version=%s protocol=%s",
                ext.install_id,
                ext.browser,
                ext.version,
                ext.extension_protocol_version or "legacy",
            )
            try:
                await ext.conn.send(json.dumps({
                    "type": "helloAck",
                    "daemonVersion": __version__,
                    "extensionProtocolVersion": EXTENSION_PROTOCOL_VERSION,
                    "versionDrift": comparison.drift.value,
                }))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "failed to send extension helloAck: install_id=%s error=%r",
                    ext.install_id,
                    e,
                )
            await self._maybe_reload_for_version_drift(ext)
            return

        if kind == "ping":
            # MV3 SW lifetime keepalive. Chrome only extends the SW's 30s idle
            # timer on application-level ws frames (the `onmessage` kind);
            # the protocol PING the `websockets` lib sends is handled by the
            # browser internally and never reaches the SW. So the extension
            # drives this app-level heartbeat and we echo back — both an
            # outgoing send (in the extension) and an incoming onmessage
            # (when this pong lands) reset the reaper.
            try:
                await ext.conn.send(json.dumps({
                    "type": "pong", "ts": msg.get("ts"),
                }))
            except Exception:
                pass
            return

        if kind == "pong":
            return

        if kind == "attached":
            tab_id = int(msg.get("tabId", -1))
            if tab_id < 0:
                return
            info = msg.get("targetInfo") or {}
            ext.tabs[tab_id] = GhostTarget(
                target_id=f"ext-tab-{tab_id}",
                tab_id=tab_id,
                url=str(info.get("url", "")),
                title=str(info.get("title", "")),
                install_id=ext.install_id,
            )
            # PR2: notify fan-out observers (the Playwright facade) of new tab
            # lifecycle so they can synthesize Target.targetCreated /
            # attachedToTarget for a live `connect_over_cdp` client. The agent
            # path ignores these (its `_on_event` only handles `event`).
            self._schedule_fanout_listeners(msg)
            return

        if kind == "detached":
            tab_id = int(msg.get("tabId", -1))
            if tab_id < 0:
                return
            ext.tabs.pop(tab_id, None)
            if self._on_event is not None:
                try:
                    await self._on_event(msg)
                except Exception as e:
                    logger.warning("relay detached handler raised: %r", e)
            self._schedule_fanout_listeners(msg)
            return

        if kind == "response":
            rid = msg.get("id")
            if isinstance(rid, int) and rid in ext.pending:
                fut = ext.pending.pop(rid)
                ext.pending_meta.pop(rid, None)
                if not fut.done():
                    if "error" in msg:
                        err = msg["error"] or {}
                        fut.set_exception(_CommandError(
                            code=int(err.get("code", -32000)),
                            message=str(err.get("message", "extension error")),
                        ))
                    else:
                        fut.set_result(msg.get("result") or {})
            return

        if kind == "event":
            if self._on_event is not None:
                try:
                    await self._on_event(msg)
                except Exception as e:
                    logger.warning("relay event handler raised: %r", e)
            await self._fanout_listeners(msg)
            return

        logger.debug("extension sent unknown type %r: %s", kind, str(msg)[:100])

    async def _app_ping_loop(self, ext: _ExtensionConn) -> None:
        try:
            while True:
                await asyncio.sleep(APP_PING_INTERVAL)
                if not ext.hello_received.is_set():
                    continue
                if self._extension_is_stale(ext):
                    await self._force_close_extension(
                        ext, reason="missing app-level frames")
                    return
                try:
                    await ext.conn.send(json.dumps({
                        "type": "ping",
                        "ts": int(time.time() * 1000),
                    }))
                except Exception:
                    return
        except asyncio.CancelledError:
            raise

    async def _fanout_listeners(self, msg: dict) -> None:
        """Call every additional fan-out observer with the raw extension
        message (PR2). Isolated from the primary `_on_event` so one observer
        raising can't drop the message for the others or the agent path."""
        for listener in list(self._event_listeners):
            try:
                await listener(msg)
            except Exception as e:  # noqa: BLE001
                logger.warning("relay fan-out listener raised: %r", e)

    def _schedule_fanout_listeners(self, msg: dict) -> None:
        """Notify secondary observers without blocking the relay reader.

        An extension ``attached`` frame is often followed immediately by the
        response to the create/attach request. Facade listeners may issue their
        own relay requests for scoped visibility checks, so awaiting them inline
        can deadlock the single websocket reader before it consumes the pending
        response. Schedule the fan-out instead and keep draining extension
        frames.
        """
        if not self._event_listeners:
            return
        task = asyncio.create_task(self._fanout_listeners(dict(msg)))

        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("relay fan-out task raised: %r", e)

        task.add_done_callback(_done)


class _CommandError(Exception):
    """Wrapped extension-side CDP error. Surfaced to the caller in
    `send_cdp` / `attach_tab` so the daemon can map to CDP -32xxx codes."""

    def __init__(self, *, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
