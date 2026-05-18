"""active-tab subcommand — H8 / US1 Mode A path.

Spec §5.4 + §6.4.1: Mode A has no persistent RPC channel, so every call spawns
a fresh ws, runs `Target.getTargets`, picks the page target with the most-recent
`lastAccessed` field, and exits. The accuracy field is hard-coded
`"heuristic-recent-activate"` in v0.1 — spec acknowledges this loses user-driven
tab clicks (Chrome UI clicks don't fire CDP `Target.activateTarget`), and that
limit is documented for the Skill.

Caller-visible side effect: this opens a ws. On `autoconnect`, that means a
popup, every call. The Skill is supposed to route around this via long-lived
REPL daemon (see browser-skill design §A.5). This CLI is the fallback.
"""
from __future__ import annotations

import time
from typing import Any

from cdp_use.client import CDPClient

from .config import Config
from .errors import Unavailable
from .resolver import resolve


# DevTools target list contains entries with `type` in this set besides actual
# pages — we treat anything not type=="page" as ineligible to be "the user's
# tab," per playwriter cdp-relay's restricted-target filter (§A附录).
_REAL_PAGE_TYPE = "page"
_INTERNAL_URL_PREFIXES = (
    "chrome://", "chrome-untrusted://", "devtools://", "edge://",
    "chrome-extension://", "about:", "view-source:",
)


async def active_tab(cfg: Config) -> dict[str, Any] | None:
    """Return the active-tab dict, or None when no eligible page exists.

    Shape (spec §5.4 --json):
        {targetId, url, title, accuracy, since_seconds}
    """
    rr = await resolve(cfg)
    targets = await _fetch_targets(rr.ws_url, cfg.timeout)
    eligible = [
        t for t in targets
        if isinstance(t, dict)
        and t.get("type") == _REAL_PAGE_TYPE
        and isinstance(t.get("url"), str)
        and not t["url"].startswith(_INTERNAL_URL_PREFIXES)
    ]
    if not eligible:
        return None

    # Pick by `lastAccessed` (CDP field, milliseconds since epoch) when present.
    # Some older Chrome builds omit it — fall back to attached==True or the
    # registry-order first one. The accuracy stays "heuristic-recent-activate"
    # either way; the field doc says so.
    now_ms = time.time() * 1000.0

    def sort_key(t):
        # Higher = more recent. Missing field sorts to the bottom.
        return t.get("lastAccessed") or 0

    eligible.sort(key=sort_key, reverse=True)
    pick = eligible[0]
    last_accessed = pick.get("lastAccessed")
    since_seconds = (
        (now_ms - float(last_accessed)) / 1000.0
        if isinstance(last_accessed, (int, float)) and last_accessed > 0
        else None
    )
    return {
        "targetId": pick.get("targetId"),
        "url": pick.get("url"),
        "title": pick.get("title", ""),
        "accuracy": "heuristic-recent-activate",
        "since_seconds": since_seconds,
    }


async def _fetch_targets(ws_url: str, timeout: float) -> list[dict]:
    """Open a ws, run Target.getTargets, close. Single roundtrip."""
    import asyncio

    client = CDPClient(ws_url)
    try:
        # CDPClient.start() establishes the ws connection. Wrap it in a timeout
        # so a hung Chrome (e.g. waiting for the user's Allow popup forever)
        # doesn't pin this subprocess.
        with _localhost_bypass_proxy(ws_url):
            await asyncio.wait_for(client.start(), timeout=timeout)
            try:
                resp = await asyncio.wait_for(
                    client.send_raw("Target.getTargets"),
                    timeout=timeout,
                )
            finally:
                await _silent_stop(client)
    except (TimeoutError, OSError) as e:
        raise Unavailable(
            f"active-tab: failed to fetch targets via {ws_url}: {e}",
            attempts={"active-tab": f"{type(e).__name__}: {e}"},
        ) from e
    # cdp-use's send_raw returns the full {"id":N,"result":{...}} structure.
    # Tolerate both shapes (`result` wrapped or already unwrapped) so we don't
    # break on a future library version.
    if isinstance(resp, dict) and "result" in resp and isinstance(resp["result"], dict):
        infos = resp["result"].get("targetInfos", [])
    elif isinstance(resp, dict):
        infos = resp.get("targetInfos", [])
    else:
        infos = []
    return infos if isinstance(infos, list) else []


async def _silent_stop(client: CDPClient) -> None:
    try:
        await client.stop()
    except Exception:
        # Closing a ws can race with Chrome closing first — never fatal.
        pass


import contextlib
import os
from urllib.parse import urlparse


@contextlib.contextmanager
def _localhost_bypass_proxy(ws_url: str):
    """Temporarily extend NO_PROXY so the user's HTTP_PROXY / ALL_PROXY env vars
    don't force this loopback ws through an outside SOCKS server. cdp-use doesn't
    expose a `proxy=None` knob, but websockets v15 honors urllib.request.proxy_bypass
    which honors NO_PROXY. Only mutates env when the target is a localhost URL.
    """
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
    # urllib caches proxy decisions; clear to make sure NO_PROXY takes effect.
    try:
        yield
    finally:
        if prev:
            os.environ["NO_PROXY"] = prev
        else:
            os.environ.pop("NO_PROXY", None)
