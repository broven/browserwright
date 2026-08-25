"""One-shot ``BrowserwrightDaemon.*`` JSON-RPC over the control surface.

This is the *downstream* client the CLI uses for every non-streaming verb: open
a ws to the running daemon's ``/control`` surface (ADR-0011), send one request,
read the matching response, close. It knows nothing about argparse, exit codes,
or printing — :mod:`browserwright.daemon.cli` owns those.

Why this isn't ``mode_b_client``: that module (Layer 2) owns a *long-lived*
``CDPSession`` for the skill and never sends a JSON-RPC frame itself. Reusing it
here would invert the layering (see CONTEXT.md, "Layer 1 / Layer 2"). So: two
clients, two jobs, both intentional. They do now agree on the address, because
both resolve it through :mod:`browserwright.daemon_url`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from .errors import DaemonError, Unavailable


#: Lifecycle events (``upstreamConnecting`` / ``upstreamReady``) can arrive
#: ahead of our response, especially when the RPC is what triggered a lazy
#: upstream open. Drain at most this many frames looking for ours before giving
#: up — a bound, not a guess: nothing legitimately emits 20 events in the window
#: of a single one-shot verb.
MAX_DRAIN_FRAMES = 20


async def call(cfg, method: str, params: dict,
               *, client_label: str, timeout: float = 10.0,
               browser_session: str | None = None) -> dict:
    """Send one ``BrowserwrightDaemon.*`` RPC and return its ``result`` dict.

    Raises :class:`Unavailable` when no daemon socket exists, and
    :class:`DaemonError` for a daemon-side error response, a non-dict result, or
    a response that never arrives within :data:`MAX_DRAIN_FRAMES`.

    ``browser_session`` goes on the ws query string as ``?session=<id>`` — the
    daemon's dispatcher routes on *that*, not on ``client_label``, so a verb that
    must reach a specific session's upstream has to pass it (omitting it lands
    on the shared context, or gets rejected outright by the daemon's
    ``_require_browser_session`` boundary check).
    """
    import websockets

    from ..daemon_url import daemon_endpoint

    async def _drain_until_response(ws) -> dict:
        for _ in range(MAX_DRAIN_FRAMES):
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg
        raise DaemonError(
            f"{method} no id=1 response after {MAX_DRAIN_FRAMES} frames")

    ep = daemon_endpoint()
    url = ep.ws("/control", client=client_label, session=browser_session)
    ws_cm = websockets.connect(
        url, compression=None, proxy=None, open_timeout=timeout,
        # CDP replies on the control surface carry screenshots; keep the limit
        # the unix listener used rather than the websockets 1 MiB default.
        max_size=100 * 1024 * 1024,
    )
    try:
        ws = await ws_cm.__aenter__()
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
        # A refused connect is "no daemon" — the same condition the socket-file
        # existence check used to stand in for, now observed directly.
        raise Unavailable(f"no daemon answered at {ep.url}: {e}") from e
    try:
        await ws.send(json.dumps({
            "id": 1, "method": method, "params": params,
        }))
        msg = await _drain_until_response(ws)
    finally:
        with contextlib.suppress(Exception):
            await ws_cm.__aexit__(None, None, None)
    if "error" in msg:
        err = msg["error"] or {}
        raise DaemonError(
            f"{method} failed: {err.get('message', err)} (code={err.get('code')})"
        )
    result = msg.get("result")
    if not isinstance(result, dict):
        raise DaemonError(f"{method} returned non-dict result: {result!r}")
    return result
