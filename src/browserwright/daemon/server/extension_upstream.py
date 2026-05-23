"""Extension upstream adapter — makes a RelayServer look like an
`UpstreamConnection` so the listener / router don't need extension-specific
branches in their hot paths.

When `backend=extension` is active in Mode B, the daemon's "upstream" is no
longer a real Chrome CDP ws — it's the RelayServer plus the connected
extension's `chrome.debugger` calls. This wrapper translates the CDP frames
the router emits into relay operations, and vice versa.

CDP commands intercepted here (not forwarded as `chrome.debugger.sendCommand`):

  - `Target.getTargets` → answered from `RelayServer.list_ghost_targets()`
  - `Target.attachToTarget` → `RelayServer.attach_tab(tabId)` + fabricated
    sessionId
  - `Target.detachFromTarget` → `RelayServer.detach_tab(tabId)`
  - `Target.setDiscoverTargets` / `Target.setAutoAttach` → silent ack
    (we don't need Chrome's discover stream — ghost targets come from the
    extension via "attached"/"detached" event types instead)
  - `Browser.getVersion` → daemon-stamped result, used for heartbeat
  - `Browser.crash`, `Browser.close` and other unsupported browser-level
    methods → -32601 ("method not implemented in extension backend")

Session-scoped commands (have `sessionId`) → routed through
`RelayServer.send_cdp(tab_id, method, params)` where `tab_id` is recovered
from the session-id naming convention (`ext-sid-<tabId>-<random>`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable

from .. import __version__
from .relay import RelayServer, GhostTarget, _CommandError

logger = logging.getLogger(__name__)


# Browser-level methods that have no meaningful chrome.debugger analog.
# v0.4 returns -32601 per spec §8.4.
_UNSUPPORTED_BROWSER_METHODS = frozenset({
    "Browser.crash",
    "Browser.close",
    "Browser.setDownloadBehavior",
    "Browser.getWindowForTarget",
    "Browser.getWindowBounds",
    "Browser.setWindowBounds",
})


def _build_requires_session_error(method: str) -> str:
    return (
        f"{method!r} requires a sessionId in extension backend — "
        "no tab attached. Attach one first via "
        "BrowserwrightDaemon.attachActiveTab (focused tab) or "
        "BrowserwrightDaemon.openBackgroundTab (background tab), then retry."
    )


def _build_create_target_error() -> str:
    """Target.createTarget can't be honored by the extension backend (it can't
    issue browser-level CDP). The old code reported the misleading 'requires a
    sessionId' error; instead point clients at the real tab-opening verbs."""
    return (
        "Target.createTarget is not supported by the extension backend — "
        "it cannot open browser-level targets. Open a tab via the skill "
        "primitive open_background(url, group=\"Agent\") (or "
        "BrowserwrightDaemon.openBackgroundTab for a background tab) instead. "
        "new_tab() works only on the rdp/env backend."
    )


def _build_unknown_session_error(session_id: str) -> str:
    return (
        f"unknown sessionId {session_id!r} — likely from a transient ws "
        "(e.g. CLI subprocess) which the daemon has since released. "
        "Re-attach from the same ws that will send subsequent commands."
    )


def _new_upstream_session_id(tab_id: int) -> str:
    """Synthetic upstream sessionId. Format chosen so the upstream side
    parser in `UpstreamSession.from_id` can recover the tabId without an
    extra table."""
    return f"ext-sid-{tab_id}-{secrets.token_hex(6).upper()}"


def _tab_id_from_session_id(session_id: str) -> int | None:
    if not session_id.startswith("ext-sid-"):
        return None
    rest = session_id[len("ext-sid-"):]
    head, _, _ = rest.partition("-")
    try:
        return int(head)
    except ValueError:
        return None


def _tab_id_from_target_id(target_id: str) -> int | None:
    if not target_id.startswith("ext-tab-"):
        return None
    try:
        return int(target_id[len("ext-tab-"):])
    except ValueError:
        return None


class ExtensionUpstream:
    """Adapter that quacks like `UpstreamConnection` but talks to a
    `RelayServer`.

    The listener wires this in as `self.upstream` when backend=extension; the
    router calls `send_text` on every client frame, and the adapter handles
    interception + translation.
    """

    def __init__(
        self,
        relay: RelayServer,
        on_frame: Callable[[str], Awaitable[None]],
        on_close: Callable[[str], Awaitable[None]],
    ):
        self._relay = relay
        self._on_frame = on_frame
        self._on_close = on_close
        self._open = False
        # Map: upstream sessionId → tabId (for the rare path where commands
        # specify sessionId without our naming convention).
        self._sessions: dict[str, int] = {}
        # The session IS a tab group (docs "extension browser = tab group").
        # We bind to the durable numeric Chrome groupId and key all ops on it;
        # the group's live membership (chrome.tabs.query({groupId})) is the
        # SINGLE source of truth for what's in the session — there is no
        # owned/borrowed bookkeeping. ``group_name`` (= session name) is only a
        # recovery anchor used to re-find the group after the groupId is lost.
        self._groups: dict[str, int] = {}        # bs session → tab-group id
        self._tab_url: dict[int, str] = {}        # tab_id → last-known url

    # ---- per-session group binding helpers -------------------------------

    def _bind_group(self, session_id: str, group_id: int) -> None:
        """Record the session's durable groupId (the session's browser id).
        A negative/invalid id is ignored — the group may have been auto-deleted
        (empty) and will be recreated on the next open."""
        if isinstance(group_id, int) and group_id >= 0:
            self._groups[session_id] = group_id

    async def _group_member_tabs(self, session_id: str | None,
                                 group_name: str | None) -> tuple[int, list[int]]:
        """Resolve the session's live group membership = the source of truth.
        Returns ``(group_id, [tab_id, ...])``. Keys on the bound groupId first,
        falls back to the title (recovery anchor). Empty list when the session
        has no live group (e.g. its last tab closed and Chrome auto-deleted the
        group)."""
        gid = self._groups.get(session_id) if session_id else None
        info = await self._relay.query_group_tabs(group_name, group_id=gid)
        if not info:
            return (-1, [])
        live_gid = int(info.get("groupId", -1))
        if session_id and live_gid >= 0:
            self._groups[session_id] = live_gid
        tabs = sorted({
            t.get("tabId") for t in (info.get("tabs") or [])
            if isinstance(t.get("tabId"), int)
        })
        return (live_gid, list(tabs))

    def session_info(self, session_id: str) -> dict:
        """Live view of a session's browser: its bound group id, the number of
        tabs we currently track for it (best-effort, in-memory), and a sample
        url. Used to fill `whoami`'s live fields. Membership-as-truth means the
        authoritative count comes from the live group query (``list_tabs``);
        this synchronous view reports the in-memory tabs bound to the group's
        recorded sessions."""
        gid = self._groups.get(session_id, -1)
        sample = next((u for u in (self._tab_url.get(t) for t in self._sessions.values())
                       if u), "")
        return {
            "session_id": session_id,
            "group_id": gid,
            "tab_count": sum(1 for _ in self._sessions),
            "sample_url": sample,
        }

    async def end_session(self, session_id: str,
                          group_name: str | None = None) -> dict:
        """Tear down a session's browser (DECIDED): close the WHOLE tab group —
        every member tab — then the group disappears. Membership is resolved
        from the live group (groupId first, title fallback), NOT from any
        owned/borrowed set. Returns ``{closed: [...], kept: []}`` (``kept`` is
        always empty now — there is no borrowed distinction; drag a tab out of
        the group first to spare it)."""
        group_id, members = await self._group_member_tabs(session_id, group_name)
        self._groups.pop(session_id, None)
        closed: list[int] = []
        for tab_id in members:
            try:
                await self._relay.close_tab(tab_id)
                closed.append(tab_id)
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            # Evict any fabricated CDP sessions bound to a closed tab.
            for sid in [s for s, t in self._sessions.items() if t == tab_id]:
                self._sessions.pop(sid, None)
            self._tab_url.pop(tab_id, None)
        return {"closed": closed, "kept": []}

    async def list_tabs(self, session_id: str | None = None,
                        group_name: str | None = None) -> dict:
        """The session's tabs, resolved from LIVE group membership (the source
        of truth) — never from an in-memory set. Returns
        ``{groupId, tabs:[{tabId, url, title, attached}, ...]}``."""
        gid = self._groups.get(session_id) if session_id else None
        info = await self._relay.query_group_tabs(group_name, group_id=gid)
        if not info:
            return {"groupId": -1, "tabs": []}
        live_gid = int(info.get("groupId", -1))
        if session_id and live_gid >= 0:
            self._groups[session_id] = live_gid
        attached_tabs = {t for t in self._sessions.values()}
        tabs = [
            {
                "tabId": t.get("tabId"),
                "url": t.get("url", ""),
                "title": t.get("title", ""),
                "attached": t.get("tabId") in attached_tabs,
            }
            for t in (info.get("tabs") or [])
            if isinstance(t.get("tabId"), int)
        ]
        return {"groupId": live_gid, "tabs": tabs}

    @property
    def ws_url(self) -> str | None:
        # Pseudo-URL for log / state.upstream_ws_url. The proxy never opens
        # a ws to this; it's just informational.
        return f"ws://127.0.0.1:{self._relay.port}/__extension_relay__"

    @property
    def is_open(self) -> bool:
        return self._open

    # ---- lifecycle -------------------------------------------------------

    async def open(self, ws_url: str | None = None, *,
                   timeout: float = 30.0) -> None:
        """Wait for the relay to have at least one extension connected.

        `ws_url` is ignored — kept for signature compatibility with
        UpstreamConnection.open. `timeout` matches the same arg shape.
        """
        await self._relay.wait_ready(timeout=timeout)
        # Wire event fan-in so async events (Page.frameNavigated etc.) get
        # surfaced into the daemon's normal event router.
        self._relay.set_event_handler(self._handle_extension_event)
        self._open = True

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self._open = False
        self._relay.set_event_handler(None)
        # We don't stop the relay here — the listener may want to keep it
        # alive across reconnects. The listener owns relay lifecycle.

    async def userscript_request(self, verb: str, payload: dict, **kw):
        return await self._relay.userscript_request(verb, payload, **kw)

    async def send_text(self, frame: str) -> None:
        """Client → 'upstream' CDP frame. We parse, intercept Target.* +
        Browser.*, and route session-scoped commands via the relay.
        """
        try:
            msg = json.loads(frame)
        except (ValueError, TypeError):
            logger.warning("extension upstream got non-JSON: %s", frame[:80])
            return
        if not isinstance(msg, dict):
            return

        method = msg.get("method")
        req_id = msg.get("id") if isinstance(msg.get("id"), int) else None
        params = msg.get("params") or {}
        session_id = msg.get("sessionId") if isinstance(msg.get("sessionId"), str) else None

        # --- intercepted browser-level methods ---
        if method == "Target.setDiscoverTargets" or method == "Target.setAutoAttach":
            # Silent ack — extension-driven discovery happens via push events.
            await self._respond(req_id, {})
            return

        if method == "Target.getTargets":
            ghosts = self._relay.list_ghost_targets()
            await self._respond(req_id, {
                "targetInfos": [
                    {
                        "targetId": g.target_id,
                        "type": g.type,
                        "url": g.url,
                        "title": g.title,
                        "attached": True,
                        "canAccessOpener": False,
                        "browserContextId": "",
                    } for g in ghosts
                ],
            })
            return

        if method == "Target.attachToTarget":
            target_id = params.get("targetId")
            tab_id = _tab_id_from_target_id(target_id) if isinstance(target_id, str) else None
            if tab_id is None:
                await self._error(req_id, -32602,
                                  f"unknown extension target {target_id!r}")
                return
            try:
                await self._relay.attach_tab(tab_id, timeout=10.0)
            except _CommandError as e:
                await self._error(req_id, e.code, e.message)
                return
            except Exception as e:
                await self._error(req_id, -32603, f"attach failed: {e!r}")
                return
            sid = _new_upstream_session_id(tab_id)
            self._sessions[sid] = tab_id
            await self._respond(req_id, {"sessionId": sid})
            return

        if method == "Target.detachFromTarget":
            sid = params.get("sessionId") or session_id
            tab_id = self._sessions.pop(sid, None) if isinstance(sid, str) else None
            if tab_id is None:
                # CDP doesn't error on detach of unknown — return empty result.
                await self._respond(req_id, {})
                return
            try:
                await self._relay.detach_tab(tab_id)
            except Exception as e:
                logger.warning("relay detach failed: %r", e)
            await self._respond(req_id, {})
            return

        if method == "Browser.getVersion":
            # Heartbeat — daemon-internal. Return a stable shape so the
            # proxy doesn't choke on the heartbeat loop in UpstreamConnection
            # land (not used in extension backend, but symmetric).
            await self._respond(req_id, {
                "product": f"browserwright-daemon-extension/{__version__}",
                "userAgent": "extension-relay",
                "protocolVersion": "1.3",
                "revision": "0",
                "jsVersion": "0",
            })
            return

        if isinstance(method, str) and method in _UNSUPPORTED_BROWSER_METHODS:
            await self._error(req_id, -32601,
                              "method not implemented in extension backend")
            return

        # --- session-scoped commands → forward via relay ---
        if session_id is None:
            # Browser-level method we don't intercept (e.g., Target.activateTarget).
            # Best effort: report -32601 since extensions can't issue
            # browser-level CDP without a session.
            if isinstance(method, str) and method.startswith("Target."):
                # Target.createTarget: the extension can't open browser-level
                # targets — fast-fail with a message naming the real verbs
                # (new_page / openBackgroundTab) rather than the misleading
                # "requires a sessionId".
                if method == "Target.createTarget":
                    await self._error(req_id, -32601, _build_create_target_error())
                    return
                # Target.activateTarget(targetId) → translate to chrome.tabs.update
                if method == "Target.activateTarget":
                    target_id = params.get("targetId")
                    tab_id = (_tab_id_from_target_id(target_id)
                              if isinstance(target_id, str) else None)
                    if tab_id is not None:
                        # We don't have a relay verb for tab activate yet;
                        # punt as success (the popup-driven attach model
                        # means user-driven activation already happened).
                        await self._respond(req_id, {})
                        return
            await self._error(req_id, -32601,
                              _build_requires_session_error(method or "<unknown>"))
            return

        tab_id = self._sessions.get(session_id) or _tab_id_from_session_id(session_id)
        if tab_id is None:
            await self._error(req_id, -32602, _build_unknown_session_error(session_id))
            return

        try:
            result = await self._relay.send_cdp(tab_id, method or "", params)
            await self._respond(req_id, result)
        except _CommandError as e:
            await self._error(req_id, e.code, e.message)
        except Exception as e:
            await self._error(req_id, -32603, f"relay send failed: {e!r}")

    async def attach_active_tab(self, *, session_id: str | None = None,
                                group_name: str | None = None) -> dict:
        """Daemon-driven ADOPT (docs C1): the relay asks the extension to move
        the focused-window active tab INTO this session's tab group and attach
        it. We fabricate a sessionId the same shape `Target.attachToTarget`
        would. Returned dict: `{sessionId, targetId, tabId, url, title,
        groupId}`.

        The adopted tab becomes a regular group member — it closes with the
        group on `end_session` (no separate borrowed flag). The extension
        REFUSES (raises) if the focused tab already belongs to another
        session's group; that error propagates to the caller.
        """
        gid = self._groups.get(session_id) if session_id else None
        ghost = await self._relay.attach_active_tab(
            group_name=group_name, group_id=gid, timeout=10.0)
        sid = _new_upstream_session_id(ghost.tab_id)
        self._sessions[sid] = ghost.tab_id
        group_id = getattr(ghost, "group_id", -1)
        group_id = int(group_id) if isinstance(group_id, int) else -1
        if session_id is not None:
            self._bind_group(session_id, group_id)
            if ghost.url:
                self._tab_url[ghost.tab_id] = ghost.url
        return {
            "sessionId": sid,
            "targetId": ghost.target_id,
            "tabId": ghost.tab_id,
            "url": ghost.url,
            "title": ghost.title,
            "groupId": group_id,
        }

    async def open_background_tab(
        self,
        url: str,
        *,
        group_name: str | None = "Agent",
        session_id: str | None = None,
    ) -> dict:
        """Open a background tab in the session's tab group via the relay,
        fabricate a sessionId, and return
        ``{sessionId, targetId, tabId, url, title, groupId}``.

        The session's group is keyed on the bound groupId (durable); the group
        NAME is passed as the recovery anchor so the extension can recreate the
        group when its last tab had closed and Chrome auto-deleted it. The
        returned groupId is (re)bound to the session — that's the only
        per-session state we keep; membership comes from the live group."""
        gid = self._groups.get(session_id) if session_id else None
        gt = await self._relay.create_background_tab(
            url, group_name=group_name, group_id=gid)
        sid = _new_upstream_session_id(gt.tab_id)
        self._sessions[sid] = gt.tab_id
        group_id = getattr(gt, "group_id", -1)
        group_id = int(group_id) if isinstance(group_id, int) else -1
        if session_id is not None:
            self._bind_group(session_id, group_id)
            if gt.url:
                self._tab_url[gt.tab_id] = gt.url
        return {
            "sessionId": sid,
            "targetId": gt.target_id,
            "tabId": gt.tab_id,
            "url": gt.url,
            "title": gt.title,
            "groupId": group_id,
        }

    async def recover_session(self, session_id: str | None,
                              group_name: str) -> dict:
        """Session-reconnect-recovery: after an extension↔daemon reconnect or a
        daemon restart, the in-memory session→tab bindings are gone but the
        Chrome tab group (title == session name) survives. Query that group,
        re-attach the debugger to each of its tabs, rebuild ``_sessions`` /
        ``_groups`` (the session→groupId binding), and return a representative
        target with the same shape as ``open_background_tab``.

        Title→groupId is the recovery path: across a restart the bound groupId
        is gone, so we look the group up by its title (= session name), which
        Chrome preserves on the surviving group.

        Raises (proxy maps to a CDP error) when no group matches or it has no
        tabs."""
        # Recovery anchor is the title; the bound id is gone after a restart.
        info = await self._relay.query_group_tabs(group_name)
        if not info or not info.get("tabs"):
            raise RuntimeError(
                f"no recoverable tabs for group {group_name!r} "
                "(group missing or empty)")
        group_id = int(info.get("groupId", -1))
        tabs = info["tabs"]
        recovered: list[int] = []
        # tab_id → (sid, url, title, lastAccessed) for picking a representative.
        meta: dict[int, dict] = {}
        for tab in tabs:
            tab_id = tab.get("tabId")
            if not isinstance(tab_id, int):
                continue
            # Idempotent: re-attaches the debugger (relay short-circuits if the
            # ghost already exists from a popup attach / re-announce).
            await self._relay.attach_tab(tab_id)
            sid = _new_upstream_session_id(tab_id)
            self._sessions[sid] = tab_id
            url = str(tab.get("url", ""))
            if session_id:
                self._bind_group(session_id, group_id)
                if url:
                    self._tab_url[tab_id] = url
            recovered.append(tab_id)
            meta[tab_id] = {
                "sid": sid,
                "url": url,
                "title": str(tab.get("title", "")),
                "lastAccessed": tab.get("lastAccessed", 0) or 0,
            }
        if not recovered:
            raise RuntimeError(
                f"group {group_name!r} had tabs but none had a usable tabId")
        # Representative tab: most-recently-accessed, else first.
        rep_id = max(recovered, key=lambda t: meta[t]["lastAccessed"])
        rep = meta[rep_id]
        return {
            "sessionId": rep["sid"],
            "targetId": f"ext-tab-{rep_id}",
            "tabId": rep_id,
            "url": rep["url"],
            "title": rep["title"],
            "groupId": group_id,
            "recovered": recovered,
        }

    async def close_tab(self, session_id: str) -> dict:
        """Close the tab bound to ``session_id`` (UPSTREAM sessionId). Raises
        ValueError if unknown — proxy translates to a CDP error."""
        tab_id = self._sessions.pop(session_id, None)
        if tab_id is None:
            tab_id = _tab_id_from_session_id(session_id)
        if tab_id is None:
            raise ValueError(f"unknown sessionId {session_id!r}")
        await self._relay.close_tab(tab_id)
        return {"ok": True, "tabId": tab_id}

    async def close_tab_by_target_id(self, target_id: str) -> dict:
        """Close-tab path used when the daemon proxy can't resolve a session
        binding (e.g. the original opener's transient ws disconnected and the
        per-client attacher was reaped). Derives tabId from ``ext-tab-N`` and
        calls the relay directly — no session lookup required. Also evicts
        any matching tab from ``_sessions`` to keep state tidy."""
        tab_id = _tab_id_from_target_id(target_id)
        if tab_id is None:
            raise ValueError(f"unknown targetId {target_id!r}")
        # Drop any sessions that still reference this tab so the upstream
        # doesn't hold stale entries.
        for sid in [s for s, t in self._sessions.items() if t == tab_id]:
            self._sessions.pop(sid, None)
        await self._relay.close_tab(tab_id)
        return {"ok": True, "tabId": tab_id}

    async def send_command(self, method: str, params: dict | None = None,
                           session_id: str | None = None,
                           timeout: float = 10.0) -> dict:
        """Daemon-internal command path (heartbeat, setDiscoverTargets).

        For the extension backend these are no-ops or trivial — we don't
        actually need them to hit Chrome. Return a synthesized success so
        the listener's startup sequence doesn't fail.
        """
        if method == "Target.setDiscoverTargets":
            return {}
        if method == "Browser.getVersion":
            return {
                "product": f"browserwright-daemon-extension/{__version__}",
                "userAgent": "extension-relay",
                "protocolVersion": "1.3",
                "revision": "0",
                "jsVersion": "0",
            }
        return {}

    # ---- helpers ---------------------------------------------------------

    async def _respond(self, req_id: int | None, result: dict) -> None:
        await self._on_frame(json.dumps({"id": req_id, "result": result}))

    async def _error(self, req_id: int | None, code: int, msg: str) -> None:
        await self._on_frame(json.dumps({
            "id": req_id, "error": {"code": code, "message": msg},
        }))

    async def _handle_extension_event(self, ext_msg: dict) -> None:
        """Translate an extension's `{"type":"event",...}` push into the
        equivalent CDP event frame so the daemon's router can fan it out.
        """
        tab_id = ext_msg.get("tabId")
        method = ext_msg.get("method")
        params = ext_msg.get("params") or {}
        if not isinstance(tab_id, int) or not isinstance(method, str):
            return
        # Find a sessionId we previously handed out for this tab.
        sid = None
        for s, t in self._sessions.items():
            if t == tab_id:
                sid = s
                break
        out: dict[str, Any] = {"method": method, "params": params}
        if sid is not None:
            out["sessionId"] = sid
        await self._on_frame(json.dumps(out))
