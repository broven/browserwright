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
import inspect
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Protocol, TYPE_CHECKING, runtime_checkable
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .proxy import Router


@runtime_checkable
class Upstream(Protocol):
    """Session-shaped browser upstream used by :class:`proxy.Router`.

    ``attach`` / ``detach`` make publication to the router atomic.  An adapter
    is attached before the state becomes CONNECTED and detached only after the
    state becomes DISCONNECTED, so a verb can never observe a connected router
    with a partially-wired implementation.
    """

    @property
    def is_open(self) -> bool: ...

    def attach(self, router: "Router") -> None: ...

    def detach(self, router: "Router") -> None: ...

    async def open_tab(self, url: str, *, background: bool = True,
                       session_id: str | None = None,
                       group_name: str | None = None,
                       skip_post_attach_commands: bool = False) -> dict: ...

    async def close_tab(self, target: str) -> dict: ...

    async def list_tabs(self, session_id: str | None = None) -> list[dict]: ...

    async def get_targets(self, params: dict,
                          session_id: str | None = None) -> dict: ...

    async def target_belongs_to_session(
        self, session_id: str, target_id: str,
    ) -> bool: ...

    async def current_page(self, session_id: str | None = None) -> dict: ...

    async def attach_active(self, *, session_id: str | None = None,
                            group_name: str | None = None) -> dict: ...

    async def end_session(self, session_id: str,
                          group_id: int | None = None) -> dict: ...

    async def recover(self, session_id: str | None = None, *,
                      group_id: int | None = None) -> dict: ...

    async def send_cdp(self, frame: str) -> None: ...

    async def open(self, ws_url: str | None = None, *,
                   timeout: float = 30.0) -> None: ...

    async def close(self, *, code: int = 1000, reason: str = "") -> None: ...

    async def wait_session_announce(self, session_id: str,
                                    timeout: float = 2.0) -> bool: ...

    async def userscript_request(self, verb: str, payload: dict,
                                 **kwargs: Any) -> dict: ...

    async def reload_extensions(self, *, reason: str = "manual",
                                expected_version: str | None = None) -> dict: ...

# 30s upstream heartbeat — spec §10 open question "Browser.getVersion 心跳频率"
# resolved to 30s.
HEARTBEAT_INTERVAL = 30.0
# Number of synthetic command ids reserved for daemon-internal use (heartbeat,
# Target subscriptions). Client ids passthrough unchanged; daemon uses big
# negatives to avoid colliding with anything a CDP client might send.
_DAEMON_ID_BASE = -2_000_000_000


class CdpUpstream:
    """Wraps a single ws to Chrome's browser-level CDP endpoint.

    Lifecycle:
      open(ws_url) → forward() pumps frames → close() ends it cleanly.

    `on_frame(text)` is called for every frame *from* upstream. It is the
    caller's job to forward it downstream (modulo BrowserwrightDaemon.* answers
    which never enter here).
    """

    def __init__(
        self,
        on_frame: Callable[[str], Awaitable[None]],
        on_close: Callable[[str], Awaitable[None]],
        *,
        state: Any | None = None,
        on_end_session: Callable[..., Awaitable[bool | None]] | None = None,
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
        self._on_end_session = on_end_session

    @property
    def backend_name(self) -> str:
        name = getattr(self._state, "backend_name", None)
        return name if isinstance(name, str) and name else "raw-cdp"

    # ---- public API -------------------------------------------------------

    @property
    def ws_url(self) -> str | None:
        return self._ws_url

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

    async def open(self, ws_url: str | None = None, *, timeout: float = 30.0) -> None:
        """Connect to upstream. Raises on failure; caller transitions state."""
        if not ws_url:
            raise ValueError("raw-CDP upstream requires a websocket URL")
        if self._ws is not None:
            raise RuntimeError("upstream already open")
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
            self._ws = await asyncio.wait_for(
                websockets.connect(ws_url, **connect_kwargs),
                timeout=timeout,
            )
        self._ws_url = ws_url
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

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
        ``session_id`` is intentionally unused because rdp/env already scope
        the workspace at the browser connection.
        """
        return await self.send_command("Target.getTargets", params)

    async def target_belongs_to_session(
        self, session_id: str, target_id: str,
    ) -> bool:
        """Raw-CDP workspaces are already isolated by browser/context."""
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
        try:
            tabs = await self.list_tabs(session_id)
        except Exception:
            tabs = []
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

    async def end_session(self, session_id: str,
                          group_id: int | None = None) -> dict:
        """End an rdp workspace; env/attach ownership remains external."""
        ended: bool | None = None
        if self._on_end_session is not None:
            ended = await self._on_end_session(session_id)
        return {"ok": ended is not False, "closed": [],
                "failed": [] if ended is not False else ["workspace"],
                "kept": [],
                "backend": self.backend_name}

    async def end_session_before(
        self, session_id: str, group_id: int | None = None, *, deadline: float,
    ) -> dict:
        """Bounded raw-workspace teardown with an honest retryable result."""
        ended: bool | None = None
        if self._on_end_session is not None:
            parameters = inspect.signature(
                self._on_end_session).parameters.values()
            accepts_deadline = any(
                parameter.name == "deadline"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters)
            if accepts_deadline:
                ended = await self._on_end_session(
                    session_id, deadline=deadline)
            else:
                # Compatibility for embedders/test doubles predating the
                # cooperative-deadline callback. Production's daemon callback
                # accepts the keyword.
                ended = await self._on_end_session(session_id)
        ok = ended is not False
        return {
            "ok": ok,
            "partial": not ok,
            "timedOut": not ok and time.monotonic() >= deadline,
            "closed": [],
            "failed": [] if ok else ["workspace"],
            "unknown": [] if ok else ["workspace"],
            "kept": [],
            "backend": self.backend_name,
        }

    async def recover(self, session_id: str | None = None, *,
                      group_id: int | None = None) -> dict:
        """Return the nearest honest raw-CDP recovery equivalent.

        Raw workspaces have no durable group binding, so there are no group
        members to reconstruct. Rebind the current live page (or create the
        documented blank fallback when the workspace is empty) and return the
        same representative-tab shape as the extension adapter.
        """
        result = await self.current_page(session_id)
        return {**result, "groupId": -1, "recovered": []}

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

    async def _register_userscript(
        self, entry: dict, sessions: list[str],
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
        entry["ids"] = identifiers
        return failed

    def _userscript_sync(self, failed: list[dict] | None = None) -> dict:
        failures = failed or []
        registered = sum(
            len(script.get("ids", []))
            for script in self._userscripts.values())
        return {
            "ok": not failures,
            "registered": registered,
            "failed": failures,
        }

    async def userscript_request(self, verb: str, payload: dict,
                                 **kwargs: Any) -> dict:
        """Raw-CDP userscript shim using new-document page scripts."""
        sessions = [sid for sid in kwargs.get("session_ids", [])
                    if isinstance(sid, str)]
        if verb == "install":
            script = payload.get("script") if isinstance(payload.get("script"), dict) else {}
            source = (script.get("source") or script.get("body")
                      or script.get("code") or "")
            script_id = script.get("id") or (
                f"rdp-us-{len(self._userscripts) + 1}")
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
        """Close the upstream cleanly. Idempotent."""
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
    """When the upstream URL is loopback, ensure NO_PROXY covers it. Same
    rationale as `active_tab._localhost_bypass_proxy`. Spec doesn't mention
    this — but Chrome runs on the user's machine, and the user often has
    HTTPS_PROXY / ALL_PROXY set."""
    host = (urlparse(ws_url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
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
# adapter, covering both rdp and env.
UpstreamConnection = CdpUpstream
