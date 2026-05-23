"""Extension relay ws server (v0.4 — spec §8.4).

The relay sits between the Chrome extension (`chrome-extension/background.js`)
and the daemon's CDP proxy. When `backend=extension` is active in Mode B,
this server replaces the conventional upstream-ws-to-Chrome path: the
"upstream" is the relay + the extension's `chrome.debugger` calls.

Protocol on the wire (extension ↔ daemon, all JSON text frames):

  daemon → extension:
    {"type":"command","id":N,"tabId":42,"method":"Page.navigate","params":{...}}
    {"type":"queryActiveTab","id":N}
    {"type":"detach","id":N,"tabId":42}

  extension → daemon:
    {"type":"hello","installId":"...","browser":"chrome","version":"1.2.3"}
    {"type":"response","id":N,"result":{...}}
    {"type":"response","id":N,"error":{"code":-32000,"message":"..."}}
    {"type":"attached","tabId":42,"targetInfo":{"url":"...","title":"..."}}
    {"type":"detached","tabId":42}
    {"type":"event","tabId":42,"method":"Page.frameNavigated","params":{...}}
    {"type":"activeTab","id":N,"tabId":42,"url":"...","title":"..."}

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
class _ExtensionConn:
    """One connected extension. v0.4 supports multiple in theory (e.g., user
    runs Chrome + Edge with the extension installed in both); the daemon
    fans commands out to whichever extension owns the target by `install_id`.
    """
    conn: ServerConnection
    install_id: str = ""
    browser: str = ""
    version: str = ""
    hello_received: asyncio.Event = field(default_factory=asyncio.Event)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    tabs: dict[int, GhostTarget] = field(default_factory=dict)


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
        self._next_cmd_id: int = 1
        self._first_ready = asyncio.Event()
        # Hook: every event-frame from any extension gets called back here so
        # the daemon's CDP proxy can route it. Set by the listener.
        self._on_event: Callable[[dict], Awaitable[None]] | None = None

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

    def set_event_handler(
        self, handler: Callable[[dict], Awaitable[None]] | None,
    ) -> None:
        """Register a coroutine that receives every async event from the
        extension (`Page.frameNavigated` etc). The daemon's router uses this
        to translate extension events back into CDP frames for clients.
        """
        self._on_event = handler

    # ---- public command API (used by extension upstream wrapper) ---------

    def list_ghost_targets(self) -> list[GhostTarget]:
        """All currently-attached tabs across every extension."""
        out: list[GhostTarget] = []
        for ext in self._extensions.values():
            out.extend(ext.tabs.values())
        return out

    async def query_active_tab(self, *, timeout: float = 5.0) -> dict | None:
        """Spec §8.4: `BrowserwrightDaemon.getActiveTab` accuracy=`exact` path.

        Asks the first ready extension `chrome.tabs.query({active:true})`. If
        no extension is connected, returns None — caller falls back to the
        heuristic-recent-activate table.
        """
        ext = self._pick_active_extension()
        if ext is None:
            return None
        return await self._request(ext, {"type": "queryActiveTab"},
                                   timeout=timeout)

    async def query_group_tabs(self, group_name: str, *,
                               timeout: float = 5.0) -> dict | None:
        """Session-reconnect-recovery: ask the extension for the tabs of the
        tab group whose title == ``group_name`` (the durable per-session
        anchor). Returns ``{"groupId":int,"tabs":[{tabId,url,title,active,
        lastAccessed}, ...]}`` — ``groupId == -1`` / empty tabs when no group
        matches. Returns None when no extension is connected (mirrors
        query_active_tab's caller-falls-back contract)."""
        ext = self._pick_active_extension()
        if ext is None:
            return None
        return await self._request(
            ext, {"type": "queryGroup", "groupName": group_name},
            timeout=timeout)

    async def attach_active_tab(self, *,
                                timeout: float = 10.0) -> GhostTarget:
        """Daemon-driven equivalent of the popup's "Attach this tab" — asks
        the extension to attach Chrome's currently-focused-window active tab
        without needing the user to click the popup.

        Retries on "already attached" the same way `attach_tab` does. Returns
        the GhostTarget once the extension confirms. The extension also emits
        `attached` as part of announceAttached, so the ghost ends up in
        `ext.tabs` for the regular routing path.
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        last_err: Exception | None = None
        for i in range(ATTACH_RETRY_LIMIT):
            try:
                result = await self._request(
                    ext, {"type": "attachActive"}, timeout=timeout)
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
                         timeout: float = 5.0) -> GhostTarget:
        """Tell the extension to `chrome.debugger.attach({tabId})`. Retries
        up to ATTACH_RETRY_LIMIT on "already attached" errors.

        Returns the GhostTarget once the extension confirms.
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
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
        timeout: float = 10.0,
    ) -> GhostTarget:
        """Spec Phase B Feature 1: open a tab in the background (active=false)
        in tab group ``group_name`` (default "Agent"), attach
        ``chrome.debugger`` to it, and return a GhostTarget bound to the new
        tab. The user's currently-active tab keeps focus.

        ``group_name=None`` skips the grouping step; the resulting GhostTarget
        carries the extension-reported ``group_id`` (which may be ``-1`` when
        no group was requested or when grouping failed in a recoverable way).
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        body: dict = {"type": "createTab", "url": url}
        if group_name:
            body["groupName"] = group_name
        result = await self._request(ext, body, timeout=timeout) or {}
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
                        timeout: float = 5.0) -> None:
        """Spec Phase B Feature 2: close a tab via chrome.tabs.remove (not a
        debugger detach). Clears the ghost-target entry whether or not the
        extension confirmed.

        Raises if no extension is connected at all — silently returning
        success here would lie to callers about a close that never went
        over the wire. `_extension_for_tab` already falls back to any ready
        extension if no ext owns the tab (race between popup attach and
        ghost registration), so a None return means "no extension exists"
        rather than "no extension owns this specific tab id".
        """
        ext = self._extension_for_tab(tab_id)
        if ext is None:
            raise RuntimeError(f"no extension knows tab {tab_id}")
        try:
            await self._request(
                ext, {"type": "closeTab", "tabId": tab_id}, timeout=timeout)
        except Exception as e:
            logger.warning("close_tab(tab=%d) failed: %r", tab_id, e)
        ext.tabs.pop(tab_id, None)

    async def send_cdp(self, tab_id: int, method: str, params: dict,
                       *, timeout: float = 10.0) -> dict:
        """Forward a CDP method+params through the extension's
        `chrome.debugger.sendCommand(tabId, method, params)`.
        """
        ext = self._extension_for_tab(tab_id)
        if ext is None:
            raise RuntimeError(f"no extension owns tab {tab_id}")
        return await self._request(ext, {
            "type": "command",
            "tabId": tab_id,
            "method": method,
            "params": params,
        }, timeout=timeout) or {}

    async def userscript_request(self, verb: str, payload: dict,
                                 *, timeout: float = 5.0) -> dict | None:
        """Forward a userscript control request to any ready extension.

        Userscript operations are extension-global rather than tab-scoped, so
        unlike ``send_cdp`` they only need a connected extension, not a tab
        owner.
        """
        ext = self._pick_active_extension()
        if ext is None:
            raise RuntimeError("no extension connected")
        return await self._request(
            ext, {"type": f"userscript.{verb}", **payload}, timeout=timeout)

    # ---- internals -------------------------------------------------------

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

    async def _request(self, ext: _ExtensionConn, body: dict, *,
                       timeout: float) -> dict | None:
        cmd_id = self._alloc_id()
        body = {**body, "id": cmd_id}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        ext.pending[cmd_id] = fut
        try:
            await ext.conn.send(json.dumps(body))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            ext.pending.pop(cmd_id, None)

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
            body = json.dumps({
                "running": True,
                "extensions": len(self._extensions),
                "install_ids": [e.install_id for e in self._extensions.values()
                                if e.hello_received.is_set()],
                "tab_count": sum(len(e.tabs) for e in self._extensions.values()),
            })
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
            self._extensions.pop(key, None)
            self._extensions.pop(temp_key, None)
            for fut in list(ext.pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("extension disconnected"))

    async def _dispatch_from_extension(self, ext: _ExtensionConn,
                                       temp_key: str, msg: dict) -> None:
        kind = msg.get("type")

        if kind == "hello":
            ext.install_id = str(msg.get("installId") or "")
            ext.browser = str(msg.get("browser") or "")
            ext.version = str(msg.get("version") or "")
            # Re-key the extension by install_id (so multiple extensions don't
            # collide on temp_key collisions).
            self._extensions.pop(temp_key, None)
            self._extensions[ext.install_id or temp_key] = ext
            ext.hello_received.set()
            self._first_ready.set()
            logger.info("extension hello: install_id=%s browser=%s version=%s",
                        ext.install_id, ext.browser, ext.version)
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
            return

        if kind == "detached":
            tab_id = int(msg.get("tabId", -1))
            if tab_id < 0:
                return
            ext.tabs.pop(tab_id, None)
            return

        if kind == "response":
            rid = msg.get("id")
            if isinstance(rid, int) and rid in ext.pending:
                fut = ext.pending.pop(rid)
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
            return

        logger.debug("extension sent unknown type %r: %s", kind, str(msg)[:100])


class _CommandError(Exception):
    """Wrapped extension-side CDP error. Surfaced to the caller in
    `send_cdp` / `attach_tab` so the daemon can map to CDP -32xxx codes."""

    def __init__(self, *, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
