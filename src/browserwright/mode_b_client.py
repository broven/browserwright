"""Mode B daemon client — long-lived connection to the daemon endpoint.

Mode B is the happy path:

  - Skill connects to a running ``browserwright-daemon serve`` instance over the
    daemon's one TCP endpoint, on its **control surface** (ADR-0011).
  - Standard CDP commands are tunnelled through. ``BrowserwrightDaemon.*`` RPCs
    (``getActiveTab``, ``disconnect``, ``subscribeFocus``, ``uiState``) are
    answered by the daemon itself, not forwarded upstream.
  - Events fan out to the client: ``upstreamClosed``, ``activeTabChanged``,
    ``upstreamReady`` etc.

The Skill side here is a single-threaded sync wrapper that ``Session`` holds
as its sole daemon client (Mode A — the one-shot subprocess resolver — was
removed; the skill always talks to a running daemon over its socket).

Discovery:
  - The endpoint URL comes from :mod:`browserwright.daemon_url` — ``--daemon-url``
    / ``$BW_DAEMON_URL`` / the toml ``daemon_url`` key / the running daemon's
    state file / ``http://127.0.0.1:19990``. No subprocess, no socket path.
  - On connect, the client opens
    ``ws://<host>:<port>/control?client=skill-repl&session=<id>``.

This client never starts, stops or replaces a daemon — that is
:mod:`browserwright.daemon_lifecycle`, and only ``session new`` / ``recover``
call it.

:func:`client_for_session` builds the client from a resolved ledger record;
``DaemonUnavailable`` surfaces lazily when no daemon answers.
"""
from __future__ import annotations

from typing import Any, Optional

from .daemon_url import DaemonEndpoint, daemon_endpoint


class ModeBClient:
    """Mode B daemon endpoint. Use ``connect()`` to confirm reachability;
    ``ws_url()`` returns the CDP-compatible URL Skill's ``CDPSession`` can
    open. Active-tab / disconnect / uiState are sent over the same socket.
    """

    def __init__(self) -> None:
        self._endpoint: Optional[str] = None
        self._transport: Optional[str] = None  # always "tcp"
        self._cached_ws: Optional[str] = None
        # client label sent on the ws query string for daemon observability;
        # session-bound clients override this with ``skill-s<id>``.
        self._client_label: str = "skill-repl"
        # The session id, emitted on the ws query as ``?session=<id>`` — this is
        # the key the daemon's dispatcher routes on (cdp sessions reach their
        # own UpstreamContext through it). None for the bare REPL client.
        self._session_id: Optional[str] = None

    # ---- endpoint discovery ---------------------------------------------

    def endpoint(self) -> DaemonEndpoint:
        """The resolved daemon endpoint (URL + whether it was configured)."""
        return daemon_endpoint()

    def ws_url(self, *, client_label: Optional[str] = None) -> str:
        """Return the control-surface ws URL the ``CDPSession`` opens.

        Caches the result; call ``invalidate()`` to force a re-resolve (e.g.
        after a 1011 close).
        """
        if client_label is None:
            client_label = self._client_label
        if self._cached_ws:
            return self._cached_ws
        ep = self.endpoint()
        # Session-bound clients carry ``?session=<id>`` — the daemon dispatcher
        # routes on this (not on the client label). Without it a cdp session
        # resolves to None → the shared (extension) context.
        url = ep.ws("/control", client=client_label, session=self._session_id)
        self._cached_ws = url
        self._endpoint = ep.url
        self._transport = "tcp"
        return url

    def invalidate(self) -> None:
        self._cached_ws = None

    # Mode A / Mode B protocol alias — Session._resolve_ws_url() picks this.
    def resolve_ws_url(self) -> str:
        return self.ws_url()

    # ---- S6 (A2-b): rewrite -32601 "unknown method" --------------------

    @staticmethod
    def is_stale_method_error(error: Any) -> bool:
        """True iff a JSON-RPC error object is a ``-32601`` "method not found".
        Generic — keys only on the standard code, never on a method name."""
        return isinstance(error, dict) and error.get("code") == -32601

    @staticmethod
    def explain_rpc_error(method: str, error: Any) -> str:
        """Turn a JSON-RPC error object into a human-actionable message.

        For ``-32601`` (unknown method) — the signature of a daemon running
        older code than what's installed — we rewrite the raw envelope into a
        clear "the daemon is stale, restart it" message that names the offending
        method. Any other code is surfaced as its own message verbatim (those
        are real protocol errors, not staleness)."""
        if ModeBClient.is_stale_method_error(error):
            return (
                f"the running daemon doesn't have method {method!r} — it is "
                f"likely stale (older than the installed code). "
                f"`browserwright recover --session <id>` replaces a stale "
                f"daemon on the default endpoint; `browserwright version "
                f"check` shows both versions."
            )
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return f"RPC {method!r} failed: {error!r}"


# ---- factory: build a client bound to a resolved session ------------

def client_for_session(record: dict) -> ModeBClient:
    """Build a Mode B client for the single global daemon.

    The connection carries the session identity as its client label
    (``skill-s<id>``) for daemon-side observability and per-session routing;
    falls back to the default ``skill-repl`` when the record has no id.

    Pure construction: no probe, no subprocess, no lifecycle side effect.
    ``DaemonUnavailable`` surfaces only when a primitive first resolves the
    ws. Version coherence is :func:`daemon_lifecycle.ensure`'s job, run by
    ``session new`` / ``recover`` — never by opening a connection."""
    client = ModeBClient()
    sid = record.get("id")
    if sid:
        client._client_label = f"skill-s{sid}"
        client._session_id = str(sid)
    return client
