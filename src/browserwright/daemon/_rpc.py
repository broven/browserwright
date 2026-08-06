"""One-shot ``BrowserwrightDaemon.*`` JSON-RPC over a transient control socket.

This is the *downstream* client the CLI uses for every non-streaming verb:
open a unix ws to the running daemon, send one request, read the matching
response, close. It knows nothing about argparse, exit codes, or printing —
:mod:`browserwright.daemon.cli` owns those.

Why this isn't ``mode_b_client``: that module (Layer 2) discovers the endpoint
by shelling out to ``browserwright-daemon status --json`` and then hands a
``ws+unix://`` *sentinel URL* to the skill's long-lived ``CDPSession``. It never
sends a JSON-RPC frame itself, and it depends on the CLI rather than the other
way round. Reusing it here would invert the layering (see CONTEXT.md, "Layer 1 /
Layer 2") and re-enter the CLI as a subprocess to talk to a socket we can open
directly. So: two clients, two jobs, both intentional.
"""
from __future__ import annotations

import asyncio
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
    from urllib.parse import quote

    from . import _ipc

    session_q = (
        f"&session={quote(str(browser_session), safe='')}"
        if browser_session else "")

    async def _drain_until_response(ws) -> dict:
        for _ in range(MAX_DRAIN_FRAMES):
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg
        raise DaemonError(
            f"{method} no id=1 response after {MAX_DRAIN_FRAMES} frames")

    path = _ipc.sock_path()
    if not path.exists():
        raise Unavailable("no daemon running")
    async with websockets.unix_connect(
        str(path),
        uri=f"ws://localhost/?client={client_label}{session_q}",
        compression=None,
    ) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": method, "params": params,
        }))
        msg = await _drain_until_response(ws)
    if "error" in msg:
        err = msg["error"] or {}
        raise DaemonError(
            f"{method} failed: {err.get('message', err)} (code={err.get('code')})"
        )
    result = msg.get("result")
    if not isinstance(result, dict):
        raise DaemonError(f"{method} returned non-dict result: {result!r}")
    return result
