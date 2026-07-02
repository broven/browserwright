"""Synchronous CDP client over a single browser-level WebSocket.

Design:
  - One root ws connection per Skill process.
  - sessionId multiplex: ``send(method, session=...)`` for per-target ops,
    no session for ``Target.*`` etc.
  - Auto-attach to a tab on demand via ``attach(targetId)``; the resulting
    session id is cached so subsequent calls reuse it.

The whole client is < 200 lines of plain websockets — easier to reason
about, easier to unit test, and we never need typed wrappers (spec §3
"raw CDP strings over typed wrappers").
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from .errors import CDPError


class _UnixSocketAdapter:
    """Wrap an ``AF_UNIX`` socket so ``setsockopt(IPPROTO_TCP, ...)`` becomes
    a no-op. websockets unconditionally calls
    ``sock.setsockopt(socket.IPPROTO_TCP, TCP_NODELAY, True)`` after
    receiving a user-provided socket — which AF_UNIX doesn't support and
    raises ``OSError: [Errno 102]``. Everything else delegates straight
    through.
    """

    __slots__ = ("_s",)

    def __init__(self, s):
        self._s = s

    def setsockopt(self, level, optname, value):
        import socket as _sock
        if level == _sock.IPPROTO_TCP:
            return None  # silently ignore — unix sockets have no TCP layer
        return self._s.setsockopt(level, optname, value)

    def __getattr__(self, name):
        return getattr(self._s, name)


def _open_unix_websocket(ws_unix_url: str, *, connect_timeout: float):
    """Open a ws connection over a unix socket. ``ws_unix_url`` has the form
    ``ws+unix:///path/to/sock?client=skill-repl``. websockets supports this
    via ``sock=`` + ``server_hostname=`` overrides, but we wrap the AF_UNIX
    socket in ``_UnixSocketAdapter`` to absorb the unconditional
    ``TCP_NODELAY`` set the library performs.
    """
    import socket as _sock
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(ws_unix_url)
    path = parsed.path
    query = parsed.query
    raw = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    raw.settimeout(connect_timeout)
    raw.connect(path)
    # CRITICAL: clear the connect-phase timeout before handing the socket to
    # websockets. `settimeout(connect_timeout)` only bounds `connect()`; if it
    # leaks into steady state it becomes a per-recv read timeout, so any RPC
    # whose reply takes longer than `connect_timeout` (e.g. the extension
    # `ensureExecutor` blocking on a slow upstream open) makes the socket
    # read time out and websockets tears the connection down as
    # "ConnectionClosedError: no close frame received or sent" — surfacing the
    # confusing `ws closed` error instead of the real RPC result/timeout.
    # Liveness is websockets' job (ping_interval/ping_timeout), not a stray
    # connect deadline. Reset to blocking so reads wait for real frames.
    raw.settimeout(None)
    sock = _UnixSocketAdapter(raw)
    # Build a synthetic ws:// URL for the upgrade handshake; websockets parses
    # this for the HTTP path + Host header.
    upgrade_url = urlunparse(("ws", "browserwright", "/", "", query, ""))
    return ws_connect(
        upgrade_url,
        sock=sock,
        server_hostname="browserwright",
        open_timeout=connect_timeout,
        max_size=64 * 1024 * 1024,
        proxy=None,
        compression=None,  # daemon disables permessage-deflate (§6.3)
    )


def _rpc_error_fix(method: str, err: object) -> str:
    """Recovery hint for a JSON-RPC error returned over the wire. A ``-32601``
    ("method not found") almost always means the running daemon is older than
    the installed code, so we surface the restart guidance (naming the method)
    instead of leaking a bare envelope. Empty string for any other error."""
    if isinstance(err, dict) and err.get("code") == -32601:
        from .mode_b_client import ModeBClient  # lazy: avoid import cycle
        return ModeBClient.explain_rpc_error(method, err)
    return ""


class CDPSession:
    """Reader-singleton CDP transport.

    All sends are synchronous: send → block on response with matching id →
    return result. Events arrive on the same socket; the reader thread
    discards them (nothing downstream consumes raw CDP events).
    """

    def __init__(self, ws_url: str, connect_timeout: float = 8.0):
        self.ws_url = ws_url
        # ``proxy=None`` is critical: CDP endpoints are loopback (or a
        # daemon-provided URL the user controls). websockets.sync defaults to
        # ``proxy=True`` which means "respect $ALL_PROXY/$HTTP_PROXY" — agents
        # commonly run inside shells that point those at a SOCKS proxy for
        # their normal browsing, and routing CDP through one would fail in
        # confusing ways. browserwright-daemon-implementer flagged this.
        if ws_url.startswith("ws+unix://"):
            # Mode B: connect to the daemon's unix socket, then upgrade as
            # if it were a ws:// localhost endpoint. We hand websockets a
            # pre-connected socket via ``sock=`` and a stand-in HTTP URL.
            self._ws = _open_unix_websocket(ws_url, connect_timeout=connect_timeout)
        else:
            # ``compression=None`` matches the Mode B daemon contract (which
            # disables permessage-deflate) and is also fine for direct CDP:
            # Chrome's browser-level ws doesn't benefit from deflate on
            # localhost. ``proxy=None`` keeps $ALL_PROXY out of loopback.
            self._ws = ws_connect(
                ws_url,
                open_timeout=connect_timeout,
                max_size=64 * 1024 * 1024,
                proxy=None,
                compression=None,
            )
        self._lock = threading.Lock()
        self._next_id = 1
        self._inflight: dict[int, dict] = {}
        self._inflight_cv = threading.Condition(self._lock)
        self._closed = False
        self._closed_reason: Optional[str] = None
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()
        # Track which target each session is bound to. Attaching to the same
        # target twice in the same process is a programmer error (§D.2.10).
        self._sessions: dict[str, str] = {}  # targetId -> sessionId

    # ---- public --------------------------------------------------------

    def send(self, method: str, *, session: Optional[str] = None, **params) -> dict:
        if self._closed:
            raise CDPError(method=method, params=params,
                           cdp_message=f"ws closed: {self._closed_reason}")
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            msg = {"id": mid, "method": method, "params": params}
            if session:
                msg["sessionId"] = session
            self._inflight[mid] = {}
        payload = json.dumps(msg)
        try:
            self._ws.send(payload)
        except ConnectionClosed as e:
            self._closed, self._closed_reason = True, str(e)
            raise CDPError(method=method, params=params, cdp_message=str(e)) from e
        # Wait for the reply.
        deadline = time.monotonic() + 30.0
        with self._inflight_cv:
            while not self._inflight[mid] and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._inflight.pop(mid, None)
                    raise CDPError(method=method, params=params,
                                   cdp_message="timeout waiting for CDP reply")
                self._inflight_cv.wait(timeout=remaining)
            entry = self._inflight.pop(mid, None)
        if self._closed and not entry:
            raise CDPError(method=method, params=params,
                           cdp_message=f"ws closed: {self._closed_reason}")
        if "error" in entry:
            err = entry["error"]
            raise CDPError(method=method, params=params,
                           cdp_message=err.get("message", str(err)),
                           fix=_rpc_error_fix(method, err))
        return entry.get("result", {})

    def attach(self, target_id: str) -> str:
        """Attach (or reuse attachment) to ``target_id`` and return sessionId."""
        if target_id in self._sessions:
            return self._sessions[target_id]
        res = self.send("Target.attachToTarget", targetId=target_id, flatten=True)
        sid = res["sessionId"]
        self._sessions[target_id] = sid
        # Enable the usual domains so wait_for_load has data.
        for domain in ("Page", "Runtime", "DOM", "Network"):
            try:
                self.send(f"{domain}.enable", session=sid)
            except CDPError:
                pass  # Some domains are noop in some Chrome builds.
        return sid

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass
        with self._inflight_cv:
            self._inflight_cv.notify_all()

    # ---- reader thread -------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                mid = msg.get("id")
                if mid is not None:
                    with self._inflight_cv:
                        if mid in self._inflight:
                            self._inflight[mid] = msg
                            self._inflight_cv.notify_all()
                # Events (no id) are dropped — nothing consumes them.
        except ConnectionClosed as e:
            self._closed_reason = str(e)
        except Exception as e:  # noqa: BLE001
            self._closed_reason = f"reader crash: {e!r}"
        finally:
            self._closed = True
            with self._inflight_cv:
                self._inflight_cv.notify_all()
