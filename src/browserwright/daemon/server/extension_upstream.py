"""Extension implementation of the session-shaped ``Upstream`` protocol.

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
import contextlib
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from ... import session_registry
from .. import __version__
from .relay import RelayServer, GhostTarget, _CommandError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .proxy import Router


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
        "new_tab() works only on the cdp/env backend."
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


def session_group_title(session_id: str | None) -> str | None:
    """The Chrome tab group title that IS this session's binding (ADR-0009).

    ``<name>-BW<sid>``. Derived from the ledger, never supplied by a caller —
    an override would let two callers disagree about a session's identity,
    which is the whole failure mode #29 was created to stop.

    Unique by construction rather than by entropy: ``sid`` comes from the
    ledger's monotonic ``next_id`` and is never reused. The visible ``-BW``
    token is what guarantees a title match can never land on a group the user
    created themselves.

    Returns None when the session has no ledger row — the caller then has no
    group to look for.
    """
    if not session_id:
        return None
    record = session_registry.get(session_id)
    if not isinstance(record, dict):
        return None
    name = record.get("name")
    name = name.strip() if isinstance(name, str) else ""
    return f"{name or 'session'}-BW{session_id}"


def make_target_info(*, target_id: str, type: str = "page", url: str = "",
                     title: str = "", attached: bool = True,
                     browser_context_id: str = "") -> dict:
    """The ONE builder for the CDP ``targetInfo`` shape the extension backend
    reports. Both the agent path (`Target.getTargets` interception,
    `scoped_target_infos`) and the Playwright facade (`facade_extension.py`)
    synthesize targetInfos through this, so the shape can't drift between the
    two paths."""
    return {
        "targetId": target_id,
        "type": type,
        "url": url,
        "title": title,
        "attached": attached,
        "canAccessOpener": False,
        "browserContextId": browser_context_id,
    }


def _ghost_target_info(g: GhostTarget) -> dict:
    """targetInfo for a relay ghost target, as enumerated by the agent path."""
    return make_target_info(
        target_id=g.target_id, type=g.type, url=g.url, title=g.title)


def _filter_target_infos(infos: list[dict], params: dict) -> list[dict]:
    """Apply CDP TargetFilter's ordered first-match include/exclude rules."""
    rules = params.get("filter")
    if not isinstance(rules, list):
        return infos
    out: list[dict] = []
    for info in infos:
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_type = rule.get("type")
            if rule_type is not None and rule_type != info.get("type"):
                continue
            if rule.get("exclude") is not True:
                out.append(info)
            break
    return out


class ExtensionUpstream:
    """Tab-group-backed implementation of the declared ``Upstream`` protocol.

    The listener wires this in as `self.upstream` when backend=extension; the
    router calls `send_text` on every client frame, and the adapter handles
    interception + translation.
    """

    def __init__(
        self,
        relay: RelayServer,
        on_frame: Callable[[str], Awaitable[None]],
        on_close: Callable[[str], Awaitable[None]],
        *,
        group_owner: "ExtensionUpstream | None" = None,
    ):
        self._relay = relay
        self._on_frame = on_frame
        self._on_close = on_close
        self._open = False
        # Map: upstream sessionId → tabId (for the rare path where commands
        # specify sessionId without our naming convention).
        self._sessions: dict[str, int] = {}
        # The session IS a tab group (docs "extension browser = tab group").
        # The group's live membership (chrome.tabs.query({groupId})) is the
        # SINGLE source of truth for what's in the session — there is no
        # owned/borrowed bookkeeping. The group itself is identified by its
        # TITLE, ``<name>-BW<sid>`` (ADR-0009); this map caches the numeric id
        # Chrome currently uses for it, per relay generation.
        self._groups: dict[str, int] = (
            group_owner._groups if group_owner is not None else {})
        # All adapters over the same relay/binding owner (agent path + each
        # Playwright facade bridge) must serialize first-bind operations for a
        # session together. Separate lock maps would still permit one group per
        # adapter under a concurrent first open.
        self._group_locks: dict[str, asyncio.Lock] = (
            group_owner._group_locks if group_owner is not None else {})
        self._group_generations: dict[str, int] = (
            group_owner._group_generations if group_owner is not None else {})
        # Per-instance title overrides for AUTO-assigned facade groups (no
        # ledger row, so `session_group_title` cannot derive one). A bridge
        # for a sessionless extension connection binds its own
        # ``<label>-BWauto-<sid>`` title here; every group-sensitive path then
        # resolves the group by that title exactly like a ledger session
        # (ADR-0009). Never shared with `group_owner`: each connection's auto
        # title is private to that connection.
        self._title_overrides: dict[str, str] = {}

    def bind_group_title(self, session_id: str, title: str) -> None:
        """Record an in-memory group title for a sessionless/auto session."""
        self._title_overrides[session_id] = title

    def _group_title_for(self, session_id: str | None) -> str | None:
        """Group title for ``session_id``: override first (auto sessions),
        then the ledger-derived title (ADR-0009)."""
        if not session_id:
            return None
        override = self._title_overrides.get(session_id)
        if override:
            return override
        return session_group_title(session_id)

    def reset_session_announce(self, session_id: str | None) -> None:
        self._relay.reset_session_announce(session_id)

    async def wait_session_announce(self, session_id: str,
                                    timeout: float = 2.0) -> bool:
        return await self._relay.wait_session_announce(session_id, timeout)

    async def reload_extensions(
        self,
        *,
        reason: str = "manual",
        expected_version: str | None = None,
    ) -> dict:
        result = await self._relay.reload_extensions(
            reason=reason,
            expected_version=expected_version,
        )
        return {
            **result,
            "applicable": True,
            "reason": "extension backend supports reload",
        }

    # ---- per-session group binding helpers -------------------------------

    def _bind_group(self, session_id: str, group_id: int) -> None:
        """Record the session's durable groupId (the session's browser id).
        A negative/invalid id is ignored — the group may have been auto-deleted
        (empty) and will be recreated on the next open."""
        if isinstance(group_id, int) and group_id >= 0:
            conflict = next(
                (owner for owner, bound in self._groups.items()
                 if owner != session_id and bound == group_id),
                None,
            )
            if conflict is not None:
                raise RuntimeError(
                    f"group ownership conflict: group {group_id} is already "
                    f"bound to session {conflict!r}")
            self._groups[session_id] = group_id
            self._group_generations[session_id] = self._relay_generation()

    def _relay_generation(self) -> int:
        generation = getattr(self._relay, "connection_generation", 0)
        return generation if isinstance(generation, int) else 0

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._group_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_locks[session_id] = lock
        return lock

    def group_for_session(self, session_id: str | None) -> int | None:
        """Return the adapter-owned live group binding for ``session_id``."""
        return self._groups.get(session_id) if session_id else None

    async def _resolve_session_group(
        self,
        session_id: str | None,
        *,
        timeout: float | None = None,
    ) -> tuple[int | None, dict | None]:
        """Resolve one session's group before any group-sensitive operation.

        ADR-0009: the group is found by TITLE — ``<name>-BW<sid>``, which we
        write at group creation and Chrome restores along with the group. It is
        the only anchor that is both ours and survives a browser restart, which
        is the one situation where a stale group can still exist. There is no
        second anchor: the numeric groupId is Chrome's handle (recycled across
        restarts, dropped when the group empties) and per-tab ownership markers
        are gone with #29.

        Adapter memory is the steady-state fast path, so the title lookup only
        costs a relay round trip after a daemon restart or an extension
        reconnect. The returned group query is reusable by membership callers,
        avoiding a second round trip on that cold path. Transport errors
        deliberately propagate: a transient failure must not be mistaken for
        permission to create a second group.

        Returns ``(None, None)`` when no group carries this session's title —
        which, per ADR-0009, means the group does not exist. A user who renames
        the group takes it out of the session by that act; we do not go looking
        for it under any other name.
        """
        if session_id:
            live = self._groups.get(session_id)
            live_generation = self._group_generations.get(session_id)
            if (isinstance(live, int) and live >= 0
                    and live_generation == self._relay_generation()):
                return live, None

        title = self._group_title_for(session_id)
        if not title:
            return None, None

        query_generation = self._relay_generation()
        query_kwargs: dict = {"group_name": title}
        if timeout is not None:
            query_kwargs["timeout"] = max(0.001, timeout)
        info = await self._relay.query_group_tabs(**query_kwargs)
        if query_generation != self._relay_generation():
            raise RuntimeError(
                f"extension reconnected while resolving group {title!r}")
        if info is None:
            raise RuntimeError(
                "extension group membership is unknown: no extension is connected")
        if not isinstance(info, dict):
            raise RuntimeError(
                f"could not resolve group {title!r}: "
                f"malformed relay response {info!r}")
        resolved = info.get("groupId")
        if not isinstance(resolved, int):
            raise RuntimeError(
                f"could not resolve group {title!r}: "
                f"malformed groupId {resolved!r}")
        if resolved < 0:
            return None, None
        if session_id:
            self._bind_group(session_id, resolved)
        return resolved, info

    @staticmethod
    def _group_required(*, group_name: str | None,
                        session_id: str | None) -> bool:
        """Whether this operation promised to land the tab in a session group."""
        return bool(group_name) or bool(session_id)

    @staticmethod
    def _require_group_result(group_id: int, *, op: str) -> None:
        if group_id < 0:
            raise RuntimeError(
                f"{op} did not return a tab group id; the extension failed to "
                "place the tab in the session tab group")

    async def _group_member_tabs(
        self, session_id: str | None, *,
        timeout: float | None = None,
    ) -> tuple[int, list[int]]:
        """Resolve the session's live group membership = the source of truth.
        Returns ``(group_id, [tab_id, ...])``. The group is found by TITLE
        (ADR-0009); the numeric id in the return value is Chrome's current
        handle for it, useful for the rest of this call and nothing longer.
        Empty list when the session has no live group — it never opened a tab,
        or its last tab closed and Chrome auto-deleted the group."""
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, timeout))
        gid, info = await self._resolve_session_group(
            session_id,
            timeout=(None if deadline is None
                     else max(0.001, deadline - time.monotonic())))
        if info is None:
            query_generation = self._relay_generation()
            query_kwargs: dict = {"group_name": self._group_title_for(session_id)}
            if deadline is not None:
                query_kwargs["timeout"] = max(
                    0.001, deadline - time.monotonic())
            info = await self._relay.query_group_tabs(**query_kwargs)
            if query_generation != self._relay_generation():
                raise RuntimeError(
                    "extension reconnected while resolving group membership")
        if info is None:
            raise RuntimeError(
                "extension group membership is unknown: no extension is connected")
        if not isinstance(info, dict):
            raise RuntimeError(
                f"extension group membership is unknown: malformed response {info!r}")
        live_gid = info.get("groupId")
        if not isinstance(live_gid, int):
            raise RuntimeError(
                "extension group membership is unknown: response has no groupId")
        # A live id that disagrees with the in-memory binding is not an error
        # any more — it is the case ADR-0009 exists to handle. We asked by
        # title; whatever numeric id Chrome hands back for that title IS the
        # group, and the cached number is simply out of date (Chrome reassigns
        # ids across a browser restart). Under the old id-keyed lookup this
        # disagreement could only mean corruption, so it raised; keeping the
        # raise now would fail exactly the restart recovery we just enabled.
        # The rebind below adopts the live id.
        raw_tabs = info.get("tabs")
        if not isinstance(raw_tabs, list):
            raise RuntimeError(
                "extension group membership is unknown: response has no tabs list")
        if session_id and live_gid >= 0:
            self._groups[session_id] = live_gid
        tabs = sorted({
            t.get("tabId") for t in raw_tabs
            if isinstance(t, dict) and isinstance(t.get("tabId"), int)
        })
        return (live_gid, list(tabs))

    # ---- fabricated CDP-session helpers (shared with the facade) ----------

    def register_session(self, tab_id: int, sid: str | None = None) -> str:
        """Bind (fabricating if needed) an upstream sessionId for ``tab_id``.
        Every path that hands out a session — Target.attachToTarget emulation,
        attach_active_tab, open_background_tab, recover_session, and the
        Playwright facade's announce — goes through here."""
        if sid is None:
            sid = _new_upstream_session_id(tab_id)
        self._sessions[sid] = tab_id
        return sid

    async def attach_target(self, tab_id: int, *, sid: str | None = None,
                            timeout: float = 10.0) -> str:
        """chrome.debugger attach (idempotent in the relay) + session binding —
        the single Target.attachToTarget core both the agent path and the
        Playwright facade drive. Raises `_CommandError` / other exceptions from
        the relay; callers map them to CDP errors."""
        # Both callers reach this method immediately after their live-membership
        # await; capture before our first await so that exact authorization
        # epoch, rather than a replacement relay's recycled tab id, reaches the
        # write boundary.
        validated_generation = self._relay_generation()
        await self._relay.attach_tab(
            tab_id, timeout=timeout,
            expected_generation=validated_generation)
        return self.register_session(tab_id, sid)

    def resolve_tab_id(self, session_id: str) -> int | None:
        """tabId for an upstream sessionId: the session table first, else the
        ``ext-sid-<tabId>-…`` naming convention."""
        return self._sessions.get(session_id) or _tab_id_from_session_id(session_id)

    def session_for_tab(self, tab_id: int) -> str | None:
        """A sessionId previously handed out for ``tab_id``, if any."""
        return next((s for s, t in self._sessions.items() if t == tab_id), None)

    def evict_tab_sessions(self, tab_id: int) -> None:
        """Drop every fabricated session bound to a (closed) tab."""
        for sid in [s for s, t in self._sessions.items() if t == tab_id]:
            self._sessions.pop(sid, None)

    async def end_session(self, session_id: str) -> dict:
        """Tear down a session's browser (DECIDED): close the WHOLE tab group —
        every member tab — then the group disappears. The group is found by its
        title (ADR-0009); membership comes from the live group, never from an
        owned/borrowed set. Returns an honest ``{ok, closed, failed, kept}``
        result (``kept`` is always empty — there is no borrowed distinction;
        drag a tab out of the group to spare it)."""
        async with self._lock_for(session_id):
            return await self._end_session_locked(session_id)

    async def end_session_before(
        self, session_id: str, *, deadline: float,
    ) -> dict:
        """Run teardown cooperatively within the daemon RPC's deadline."""
        async with self._lock_for(session_id):
            return await self._end_session_locked(
                session_id, deadline=deadline)

    async def close_auto_group(self, session_id: str) -> dict:
        """Teardown an AUTO-assigned facade group (sessionless connection).

        Closes every tab in the group, then drops the in-memory group binding
        and title override. Best-effort: the Chrome group disappears on its
        own once empty, and an empty/unbound group is a no-op. Leaves the
        user's real Chrome running — only the connection's own tabs are
        agent-owned (docs: extension teardown semantics).
        """
        async with self._lock_for(session_id):
            try:
                _gid, members = await self._group_member_tabs(
                    session_id, timeout=5.0)
            except asyncio.TimeoutError:
                members = []
            except Exception:  # noqa: BLE001 - best-effort sweep
                members = []
            generation = self._relay_generation()
            closed: list[int] = []
            failed: list[int] = []
            for tab_id in members:
                try:
                    await self._relay.close_tab(
                        tab_id, timeout=5.0,
                        expected_generation=generation)
                    closed.append(tab_id)
                except Exception:  # noqa: BLE001 - best-effort
                    failed.append(tab_id)
                self.evict_tab_sessions(tab_id)
            self._groups.pop(session_id, None)
            self._group_generations.pop(session_id, None)
            self._title_overrides.pop(session_id, None)
            return {"closed": closed, "failed": failed}

    async def _end_session_locked(self, session_id: str, *,
                                  deadline: float | None = None) -> dict:
        def budget_left() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        initial_timeout = budget_left()
        if initial_timeout is not None and initial_timeout <= 0:
            return self._budget_exhausted_result(session_id)
        try:
            group_id, members = await self._group_member_tabs(
                session_id, timeout=initial_timeout)
        except asyncio.TimeoutError:
            return self._budget_exhausted_result(session_id)

        # ADR-0009 removed the pre-close checkpoint that used to run here.
        # It existed because the group could only be found by numeric id, so a
        # retry had to be handed the tab ids in advance. The title finds the
        # group again on its own, and re-querying it yields the survivors
        # directly — which was always the more truthful answer anyway.
        teardown_generation = self._relay_generation()
        closed: list[int] = []
        uncertain: list[int] = []
        attempted: set[int] = set()
        timed_out = False
        for tab_id in members:
            close_timeout = budget_left()
            if close_timeout is not None and close_timeout <= 0:
                timed_out = True
                break
            attempted.add(tab_id)
            try:
                if close_timeout is None:
                    await self._relay.close_tab(
                        tab_id, expected_generation=teardown_generation)
                else:
                    await self._relay.close_tab(
                        tab_id, timeout=min(5.0, close_timeout),
                        expected_generation=teardown_generation)
            except asyncio.TimeoutError:
                uncertain.append(tab_id)
                timed_out = True
                break
            except asyncio.CancelledError:
                # The pre-close checkpoint already contains every possible
                # survivor. Do not risk masking cancellation with another
                # persistence error while the close outcome is unknown.
                raise
            except ConnectionError as e:
                uncertain.append(tab_id)
                logger.warning(
                    "end_session(%s) stopped at generation boundary: %r",
                    session_id, e)
                break
            except Exception as e:  # noqa: BLE001 — report every failed tab
                uncertain.append(tab_id)
                logger.warning(
                    "end_session(%s) could not close tab %d: %r",
                    session_id, tab_id, e)
            else:
                closed.append(tab_id)
                # Evict only after Chrome confirmed the tab is gone.
                self.evict_tab_sessions(tab_id)
                survivors = sorted(set(members) - set(closed))
                self._persist_current_target(
                    session_id,
                    survivors[0] if len(survivors) == 1 else None)
        unattempted = sorted(set(members) - attempted)
        membership_unknown = False
        if uncertain and not timed_out:
            try:
                query_timeout = budget_left()
                if query_timeout is not None and query_timeout <= 0:
                    raise asyncio.TimeoutError
                _live_gid, remaining = await self._group_member_tabs(
                    session_id, timeout=query_timeout)
            except Exception as e:  # noqa: BLE001 - uncertainty is the result
                membership_unknown = True
                remaining = list(uncertain)
                if isinstance(e, asyncio.TimeoutError):
                    timed_out = True
                logger.warning(
                    "end_session(%s) could not reconcile close results: %r",
                    session_id, e)
            else:
                # Re-query resolves the UNCERTAIN tabs only. A close that
                # returned success is a known fact and must not be erased by
                # an inference — group membership can lag or come back stale,
                # and dropping tab N from `closed` after we watched it close
                # makes the caller retry a tab that is already gone.
                remaining_set = set(remaining)
                closed = sorted(set(closed) | (set(uncertain) - remaining_set))
                for tab_id in closed:
                    self.evict_tab_sessions(tab_id)
        elif uncertain:
            membership_unknown = True
            remaining = list(uncertain)
        else:
            remaining = []
        # Symmetric to `closed` above: a tab we watched close is not "failed"
        # just because a lagging membership query still lists it. Only tabs we
        # could not confirm gone are failures.
        failed = sorted(
            (set(remaining) | set(unattempted)) - set(closed))
        if not failed:
            self._groups.pop(session_id, None)
            self._group_generations.pop(session_id, None)
        else:
            # A partial close keeps the live binding and points the current
            # target at a tab Chrome explicitly refused to close. Nothing
            # durable is needed for the retry itself: it finds the group again
            # by title and re-queries the survivors (ADR-0009).
            self._persist_current_target(
                session_id, failed[0] if len(failed) == 1 else None)
        return {
            "ok": not failed and not timed_out,
            "closed": closed,
            "failed": failed,
            "unknown": sorted(set(uncertain)) if membership_unknown else [],
            "kept": [],
            "backend": "extension",
            **({"partial": True, "timedOut": True} if timed_out else {}),
        }

    def _budget_exhausted_result(self, session_id: str) -> dict:
        """Teardown ran out of budget before it could see the group.

        ADR-0009 removed the persisted retry anchors this used to enumerate,
        and nothing is lost by that: the tabs were never named here anyway
        (the budget expired before the membership query), and the retry finds
        the group again by title. Reporting an honest "unknown" beats
        reporting a stale list as if it were fact.
        """
        return {
            "ok": False,
            "partial": True,
            "timedOut": True,
            "closed": [],
            "failed": [],
            "unknown": [],
            "kept": [],
            "backend": "extension",
        }

    @staticmethod
    def _persist_current_target(session_id: str, current: int | None) -> None:
        """Record which tab this session is currently driving.

        ADR-0009 dropped the retry anchors this used to write beside it
        (``group_id`` / ``retry_target_ids``). A retry no longer needs to
        remember which tabs might still be alive: it finds the group by title
        and re-queries it, and live membership was always the source of truth.

        Best-effort on purpose. The old version raised when the ledger write
        failed, which let a local file error abort a browser teardown midway —
        two unrelated facts welded together.
        """
        record = session_registry.get(session_id)
        if not isinstance(record, dict):
            return
        runtime = dict(record.get("runtime") or {})
        runtime["updated_at"] = time.time()
        runtime["current_target_id"] = (
            f"ext-tab-{current}" if current is not None else None)
        with contextlib.suppress(Exception):
            session_registry.update(session_id, runtime=runtime)

    async def close_session_tab(self, session_id: str, target_id: str) -> dict:
        """Authorize, close, and durably re-anchor one extension tab."""
        tab_id = _tab_id_from_target_id(target_id)
        if tab_id is None:
            raise ValueError(f"unknown targetId {target_id!r}")
        async with self._lock_for(session_id):
            _group_id, members = await self._group_member_tabs(session_id)
            validated_generation = self._relay_generation()
            if tab_id not in set(members):
                raise ValueError(
                    f"target {target_id} does not belong to session "
                    f"{session_id!r}")
            await self._relay.close_tab(
                tab_id, expected_generation=validated_generation)
            self.evict_tab_sessions(tab_id)
            survivors = sorted(set(members) - {tab_id})
            self._persist_current_target(
                session_id, survivors[0] if survivors else None)
            if not survivors:
                self._groups.pop(session_id, None)
                self._group_generations.pop(session_id, None)
            return {"ok": True, "tabId": tab_id}

    async def scoped_target_infos(self, session_id: str | None) -> list[dict]:
        """CDP ``targetInfos`` for the session's browser = its tab group ONLY.

        The source of truth is the live group membership (by the session's bound
        groupId); we filter the global ghost list down to tabs that belong to
        this session's group so two sessions sharing one Chrome stay mutually
        invisible at enumeration. Shape matches the unscoped ``Target.getTargets``
        interception."""
        _gid, member_tabs = await self._group_member_tabs(session_id)
        members = set(member_tabs)
        out: list[dict] = []
        for g in self._relay.list_ghost_targets():
            tab_id = _tab_id_from_target_id(g.target_id)
            if tab_id is None or tab_id not in members:
                continue
            out.append(_ghost_target_info(g))
        return out

    @property
    def ws_url(self) -> str | None:
        # Pseudo-URL for log / state.upstream_ws_url. The proxy never opens
        # a ws to this; it's just informational.
        return f"ws://127.0.0.1:{self._relay.port}/__extension_relay__"

    @property
    def is_open(self) -> bool:
        return self._open

    def attach(self, router: "Router") -> None:
        current = router.upstream
        if current is not None and current is not self:
            raise RuntimeError("router already has an upstream")
        router.upstream = self

    def detach(self, router: "Router") -> None:
        if router.upstream is self:
            router.upstream = None

    # ---- lifecycle -------------------------------------------------------

    async def open(self, ws_url: str | None = None, *,
                   timeout: float = 30.0) -> None:
        """Wait for the relay to have at least one extension connected.

        `ws_url` is ignored; `timeout` matches the shared protocol shape.
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
                "targetInfos": [_ghost_target_info(g) for g in ghosts],
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
                sid = await self.attach_target(tab_id, timeout=10.0)
            except _CommandError as e:
                await self._error(req_id, e.code, e.message)
                return
            except Exception as e:
                await self._error(req_id, -32603, f"attach failed: {e!r}")
                return
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

        tab_id = self.resolve_tab_id(session_id)
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

    async def send_cdp(self, frame: str) -> None:
        await self.send_text(frame)

    async def attach_active_tab(self, *, session_id: str | None = None,
                                group_name: str | None = None) -> dict:
        """Daemon-driven ADOPT (docs C1): the relay asks the extension to move
        the focused-window active tab INTO this session's tab group and attach
        it. We fabricate a sessionId the same shape `Target.attachToTarget`
        would. Returned dict: `{sessionId, targetId, tabId, url, title,
        groupId}`.

        The adopted tab becomes a regular group member — it closes with the
        group on `end_session` (no separate borrowed flag). The extension
        REFUSES (raises) if the focused tab already belongs to ANY other tab
        group (the user's own manual groups count as occupied); that error
        propagates to the caller.
        """
        if session_id is not None:
            async with self._lock_for(session_id):
                return await self._attach_active_tab_locked(
                    session_id=session_id, group_name=group_name)
        return await self._attach_active_tab_locked(
            session_id=session_id, group_name=group_name)

    async def _attach_active_tab_locked(
        self, *, session_id: str | None, group_name: str | None,
    ) -> dict:
        # ADR-0009: a group either carries this session's title or it is not
        # this session's group. There is no "exists but unproven" state left
        # for attachActiveTab to escape from — the extension joins the group
        # with this title or creates it. The title is DERIVED, never taken from
        # the caller: honouring a passed name would let two callers put one
        # session in two groups, the split #29 existed to prevent.
        group_name = self._group_title_for(session_id) or group_name
        validated_generation = self._relay_generation()
        ghost = await self._relay.attach_active_tab(
            group_name=group_name, timeout=10.0,
            expected_generation=validated_generation)
        group_id = getattr(ghost, "group_id", -1)
        group_id = int(group_id) if isinstance(group_id, int) else -1
        if self._group_required(
                group_name=group_name, session_id=session_id):
            self._require_group_result(group_id, op="attachActive")
        sid = self.register_session(ghost.tab_id)
        if session_id is not None:
            self._bind_group(session_id, group_id)
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
        background: bool = True,
        skip_post_attach_commands: bool = False,
    ) -> dict:
        """Open a background tab in the session's tab group via the relay,
        fabricate a sessionId, and return
        ``{sessionId, targetId, tabId, url, title, groupId}``.

        The session's group is keyed on its TITLE (ADR-0009), derived from the
        ledger rather than taken from ``group_name`` — that parameter only
        applies to the sessionless path. The returned groupId is (re)bound to
        the session as a live cache; membership comes from the live group."""
        if session_id is not None:
            async with self._lock_for(session_id):
                return await self._open_background_tab_locked(
                    url, group_name=group_name, session_id=session_id,
                    background=background,
                    skip_post_attach_commands=skip_post_attach_commands)
        return await self._open_background_tab_locked(
            url, group_name=group_name, session_id=session_id,
            background=background,
            skip_post_attach_commands=skip_post_attach_commands)

    async def _open_background_tab_locked(
        self,
        url: str,
        *,
        group_name: str | None,
        session_id: str | None,
        background: bool,
        skip_post_attach_commands: bool,
    ) -> dict:
        # ADR-0009: the group title is DERIVED, never taken from the caller.
        # Honouring a passed name would let two callers put one session in two
        # different groups — the split #29 existed to prevent. The parameter
        # survives only for the sessionless path, which has no title to derive.
        group_name = self._group_title_for(session_id) or group_name
        validated_generation = self._relay_generation()
        self.reset_session_announce(session_id)
        gt = await self._relay.create_background_tab(
            url,
            group_name=group_name,
            background=background,
            skip_post_attach_commands=skip_post_attach_commands,
            expected_generation=validated_generation,
        )
        group_id = getattr(gt, "group_id", -1)
        group_id = int(group_id) if isinstance(group_id, int) else -1
        if self._group_required(
                group_name=group_name, session_id=session_id):
            self._require_group_result(group_id, op="createTab")
        sid = self.register_session(gt.tab_id)
        if session_id is not None:
            self._bind_group(session_id, group_id)
        return {
            "sessionId": sid,
            "targetId": gt.target_id,
            "tabId": gt.tab_id,
            "url": gt.url,
            "title": gt.title,
            "groupId": group_id,
        }

    async def open_tab(
        self,
        url: str,
        *,
        background: bool = True,
        session_id: str | None = None,
        group_name: str | None = None,
        skip_post_attach_commands: bool = False,
    ) -> dict:
        return await self.open_background_tab(
            url,
            group_name=group_name,
            session_id=session_id,
            background=background,
            skip_post_attach_commands=skip_post_attach_commands,
        )

    async def list_tabs(self, session_id: str | None = None) -> list[dict]:
        return await self.scoped_target_infos(session_id)

    async def get_targets(self, params: dict,
                          session_id: str | None = None) -> dict:
        """Synthesize the session's tab-group-scoped browser enumeration."""
        infos = await self.scoped_target_infos(session_id)
        return {"result": {"targetInfos": _filter_target_infos(infos, params)}}

    async def target_belongs_to_session(
        self, session_id: str, target_id: str,
    ) -> bool:
        """Authorize from live group membership, never a facade/session cache."""
        tab_id = _tab_id_from_target_id(target_id)
        if tab_id is None:
            return False
        _group_id, members = await self._group_member_tabs(session_id)
        return tab_id in set(members)

    async def current_page(self, session_id: str | None = None) -> dict:
        infos = await self.scoped_target_infos(session_id)
        if not infos:
            return await self.open_tab(
                "about:blank", session_id=session_id,
                group_name=self._group_title_for(session_id) or "Agent")
        info = infos[0]
        tab_id = _tab_id_from_target_id(str(info.get("targetId", "")))
        if tab_id is None:
            raise RuntimeError(f"malformed extension target: {info!r}")
        sid = self.session_for_tab(tab_id)
        if sid is None:
            sid = await self.attach_target(tab_id)
        group_id = self.group_for_session(session_id)
        return {
            "sessionId": sid,
            "targetId": info["targetId"],
            "tabId": tab_id,
            "url": info.get("url", ""),
            "title": info.get("title", ""),
            "groupId": group_id if group_id is not None else -1,
        }

    async def attach_active(self, *, session_id: str | None = None,
                            group_name: str | None = None) -> dict:
        return await self.attach_active_tab(
            session_id=session_id, group_name=group_name)

    async def recover_session(self, session_id: str | None) -> dict:
        """Session-reconnect-recovery: after a daemon restart (Chrome still
        running) the in-memory session→tab bindings are gone, but the Chrome
        tab group survives. Find that group **by its title** (ADR-0009),
        re-attach the debugger to each of its tabs, rebuild ``_sessions`` /
        ``_groups``, and return a representative target with the same shape as
        ``open_background_tab``.

        Since ADR-0009 this also survives a browser restart: the title comes
        back with the restored group, where the numeric id would not and the
        old per-tab markers were wiped. A user who renames the group takes it
        out of the session by that act, and recovery reports nothing to
        recover.

        Raises (proxy maps to a CDP error) when no group matches or it has no
        tabs."""
        if not session_id:
            raise ValueError("extension recovery requires a session id")
        async with self._lock_for(session_id):
            return await self._recover_session_locked(session_id)

    async def _recover_session_locked(self, session_id: str) -> dict:
        resolved_group_id, info = await self._resolve_session_group(session_id)
        # The adapter-memory fast path returns ``(gid, None)`` — the group
        # query is deferred to the caller. Mirror `_group_member_tabs` and
        # re-query before concluding the group is empty (e2e recovery test: an
        # in-daemon session that just opened a tab hits the memory path and
        # would otherwise be reported "group missing" despite a live group).
        if info is None and isinstance(resolved_group_id, int) and resolved_group_id >= 0:
            query_generation = self._relay_generation()
            info = await self._relay.query_group_tabs(
                group_name=self._group_title_for(session_id))
            if query_generation != self._relay_generation():
                raise RuntimeError(
                    "extension reconnected while resolving group membership")
        if not info or not info.get("tabs"):
            raise RuntimeError(
                f"no recoverable tabs for session {session_id!r} "
                f"(no group titled {self._group_title_for(session_id)!r}, "
                "or it is empty)")
        group_id = int(resolved_group_id if resolved_group_id is not None else -1)
        tabs = info["tabs"]
        validated_generation = self._relay_generation()
        recovered: list[int] = []
        # tab_id → (sid, url, title, lastAccessed) for picking a representative.
        meta: dict[int, dict] = {}
        for tab in tabs:
            tab_id = tab.get("tabId")
            if not isinstance(tab_id, int):
                continue
            # Idempotent: re-attaches the debugger (relay short-circuits if the
            # ghost already exists from a popup attach / re-announce). NOTE:
            # deliberately NOT `attach_target` — recovery keeps the relay's
            # default attach timeout, not the emulation path's 10s.
            await self._relay.attach_tab(
                tab_id, expected_generation=validated_generation)
            sid = self.register_session(tab_id)
            url = str(tab.get("url", ""))
            recovered.append(tab_id)
            meta[tab_id] = {
                "sid": sid,
                "url": url,
                "title": str(tab.get("title", "")),
                "lastAccessed": tab.get("lastAccessed", 0) or 0,
            }
        if not recovered:
            raise RuntimeError(
                f"group id {group_id} had tabs but none had a usable tabId")
        self._bind_group(session_id, group_id)
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

    async def recover(self, session_id: str | None = None) -> dict:
        # ADR-0009: the verbs layer calls recover with only the session id and
        # the group is found by its TITLE; the numeric groupId param is gone
        # (a leftover signature would have made title recovery unreachable).
        return await self.recover_session(session_id)

    async def close_tab(self, target: str) -> dict:
        """Close a tab addressed by upstream sessionId or targetId.

        Supporting both forms keeps the protocol session-shaped while covering
        the targetId fallback used after the original downstream opener has
        disconnected and its per-client binding was reaped.
        """
        tab_id = self._sessions.get(target)
        if tab_id is None:
            tab_id = _tab_id_from_session_id(target)
        if tab_id is None:
            tab_id = _tab_id_from_target_id(target)
        if tab_id is None:
            raise ValueError(f"unknown sessionId/targetId {target!r}")
        await self._relay.close_tab(tab_id)
        self.evict_tab_sessions(tab_id)
        return {"ok": True, "tabId": tab_id}

    async def close_tab_by_target_id(
        self, target_id: str, *, expected_generation: int | None = None,
    ) -> dict:
        """Close-tab path used when the daemon proxy can't resolve a session
        binding (e.g. the original opener's transient ws disconnected and the
        per-client attacher was reaped). Derives tabId from ``ext-tab-N`` and
        calls the relay directly — no session lookup required. Also evicts
        any matching tab from ``_sessions`` to keep state tidy."""
        tab_id = _tab_id_from_target_id(target_id)
        if tab_id is None:
            raise ValueError(f"unknown targetId {target_id!r}")
        await self._relay.close_tab(
            tab_id, expected_generation=expected_generation)
        # Drop sessions only after the relay confirmed Chrome closed the tab.
        self.evict_tab_sessions(tab_id)
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
        if ext_msg.get("type") == "detached" and isinstance(tab_id, int):
            sessions = [
                sid for sid, bound_tab in self._sessions.items()
                if bound_tab == tab_id
            ]
            self.evict_tab_sessions(tab_id)
            for sid in sessions:
                await self._on_frame(json.dumps({
                    "method": "Target.detachedFromTarget",
                    "params": {
                        "sessionId": sid,
                        "targetId": f"ext-tab-{tab_id}",
                    },
                }))
            return
        method = ext_msg.get("method")
        params = ext_msg.get("params") or {}
        if not isinstance(tab_id, int) or not isinstance(method, str):
            return
        # Find a sessionId we previously handed out for this tab.
        sid = self.session_for_tab(tab_id)
        if sid is None:
            # Page/Runtime/Network events are tab-scoped even when the facade
            # (which has its own CDP sid table) created the tab. Emitting them
            # without a sid makes Router treat them as browser-level and
            # broadcast across every extension session. The facade receives
            # the same relay event through its own fan-out listener; the agent
            # path must drop an event it cannot route to a bound CDP session.
            return
        out: dict[str, Any] = {"method": method, "params": params}
        out["sessionId"] = sid
        await self._on_frame(json.dumps(out))
