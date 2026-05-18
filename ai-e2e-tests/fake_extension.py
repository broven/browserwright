"""Fake Chrome extension client for US-Ext.

Speaks the daemon's relay protocol (see
`browser-daemon/src/browser_daemon/server/relay.py` for the wire shape)
and back-proxies all real work to the isolated Chrome on
`127.0.0.1:9444`. Effectively: instead of `chrome.debugger.sendCommand`,
we open a per-tab CDP ws to Chrome and forward raw CDP frames.

This is the "mock end-to-end" path (option A from the F-4e review
finding). It verifies:
  - daemon serve --backend extension binds the relay correctly
  - daemon's __status__ counts our connection
  - daemon's wire protocol translation between Mode B clients and the
    extension protocol (attach / command / response / event) works
  - skill via Mode B → daemon → fake extension → real Chrome CDP
    chain functions transparently for the agent

What it does NOT verify (path B's territory):
  - `chrome-extension/background.js` code (we replace it)
  - `chrome.debugger` API behavior
  - MV3 service-worker lifecycle
  - manifest.json correctness

Standalone usage:

    FAKE_EXT_RELAY_PORT=19989 FAKE_EXT_UPSTREAM_PORT=9444 \\
        python fake_extension.py

The harness launches this as a subprocess and waits for the daemon's
`__status__` endpoint to report `extensions >= 1`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect


# Module-level state.
RELAY_HOST = "127.0.0.1"
RELAY_PORT = 19989
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 9444
INSTALL_ID = "ai-e2e-fake-ext"


# In a real extension, tabId is Chrome's numeric tab ID. We assign our own
# monotonic IDs and keep a map back to Chrome's targetId / per-tab ws.
class FakeTab:
    __slots__ = ("tab_id", "target_id", "ws_url", "title", "url", "cdp_ws")

    def __init__(self, tab_id: int, target: dict[str, Any]) -> None:
        self.tab_id = tab_id
        self.target_id = target["id"]
        self.ws_url = target["webSocketDebuggerUrl"]
        self.title = target.get("title", "")
        self.url = target.get("url", "")
        self.cdp_ws: Any | None = None  # opened lazily on first attach


# Tab table is module-level so all the async handlers can share it.
_tabs: dict[int, FakeTab] = {}
_target_to_tab: dict[str, int] = {}
_next_tab_id = 1
_relay_ws: Any | None = None  # set in main()


def _httpx_kw() -> dict[str, Any]:
    """Bypass any user-installed proxies for 127.0.0.1 calls."""
    return {"trust_env": False, "mounts": {}}


async def _refresh_targets() -> list[FakeTab]:
    """Fetch Chrome's current page list; sync our tab table to it.
    Returns the current FakeTab list."""
    global _next_tab_id
    async with httpx.AsyncClient(timeout=2.0, **_httpx_kw()) as c:
        r = await c.get(f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/json")
    seen_now: set[str] = set()
    for t in r.json():
        if t.get("type") != "page":
            continue
        tid = t.get("id")
        if not tid:
            continue
        seen_now.add(tid)
        if tid not in _target_to_tab:
            ftab = FakeTab(_next_tab_id, t)
            _tabs[_next_tab_id] = ftab
            _target_to_tab[tid] = _next_tab_id
            _next_tab_id += 1
    # Purge tabs that Chrome no longer has.
    for tid in list(_target_to_tab.keys()):
        if tid not in seen_now:
            tab_id = _target_to_tab.pop(tid)
            tab = _tabs.pop(tab_id, None)
            if tab and tab.cdp_ws is not None:
                try:
                    await tab.cdp_ws.close()
                except Exception:
                    pass
    return list(_tabs.values())


async def _pick_active_tab() -> FakeTab | None:
    """Best-guess "user's active tab": Chrome's /json activation order
    has the most-recently-focused page first. Refresh first to keep
    pace with new_tab calls etc."""
    tabs = await _refresh_targets()
    return tabs[0] if tabs else None


async def _send(msg: dict[str, Any]) -> None:
    """Send a JSON frame to the relay. No-op if relay isn't connected."""
    if _relay_ws is None:
        return
    await _relay_ws.send(json.dumps(msg))


async def _ensure_tab_cdp(tab: FakeTab) -> Any:
    """Open the per-tab CDP ws if it isn't open yet. Start a background
    pump that forwards Chrome events back to the relay as 'event'
    messages."""
    if tab.cdp_ws is not None:
        return tab.cdp_ws
    tab.cdp_ws = await ws_connect(tab.ws_url, max_size=None)
    asyncio.create_task(_pump_events_from_chrome(tab))
    return tab.cdp_ws


async def _pump_events_from_chrome(tab: FakeTab) -> None:
    """Forward CDP events from Chrome → relay as
    `{"type":"event","tabId":T,...}`. CDP responses (frames with 'id')
    are handled inline by the command handler, not here."""
    try:
        async for raw in tab.cdp_ws:
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            if "id" in frame:
                # Belongs to a pending command — handled there. Stash by id.
                fut = _pending_cdp.pop(frame["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(frame)
                continue
            # Event frame — wrap and forward.
            await _send({
                "type": "event",
                "tabId": tab.tab_id,
                "method": frame.get("method"),
                "params": frame.get("params") or {},
            })
    except websockets.ConnectionClosed:
        pass


_pending_cdp: dict[int, asyncio.Future] = {}
_next_cdp_id = 1_000_000   # offset from the relay's id space to avoid collisions


async def _send_cdp(tab: FakeTab, method: str, params: dict) -> dict:
    """Send a raw CDP command to the per-tab ws; await its response."""
    global _next_cdp_id
    cdp_ws = await _ensure_tab_cdp(tab)
    cid = _next_cdp_id
    _next_cdp_id += 1
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_cdp[cid] = fut
    await cdp_ws.send(json.dumps({"id": cid, "method": method, "params": params}))
    return await asyncio.wait_for(fut, timeout=15.0)


# ---- relay-protocol message handlers ---------------------------------------


async def handle_query_active_tab(msg: dict) -> None:
    req_id = msg["id"]
    tab = await _pick_active_tab()
    if tab is None:
        # Empty world — let relay handle the None case.
        await _send({"type": "activeTab", "id": req_id, "tabId": -1,
                     "url": "", "title": ""})
        return
    await _send({
        "type": "activeTab",
        "id": req_id,
        "tabId": tab.tab_id,
        "url": tab.url,
        "title": tab.title,
    })


async def handle_attach(msg: dict) -> None:
    req_id = msg["id"]
    tab_id = msg["tabId"]
    await _refresh_targets()  # in case Chrome opened/closed tabs
    tab = _tabs.get(tab_id)
    if tab is None:
        await _send({
            "type": "response",
            "id": req_id,
            "error": {"code": -32000, "message": f"no such tab {tab_id}"},
        })
        return
    try:
        # Open the per-tab CDP ws now so we know Chrome accepts the
        # debugger session before reporting attached.
        await _ensure_tab_cdp(tab)
    except Exception as e:
        await _send({
            "type": "response",
            "id": req_id,
            "error": {"code": -32000, "message": f"attach failed: {e!r}"},
        })
        return
    await _send({
        "type": "response",
        "id": req_id,
        "result": {"targetInfo": {
            "url": tab.url,
            "title": tab.title,
        }},
    })
    # Also emit the "attached" notification the relay listens for.
    await _send({
        "type": "attached",
        "tabId": tab_id,
        "targetInfo": {"url": tab.url, "title": tab.title},
    })


async def handle_detach(msg: dict) -> None:
    req_id = msg["id"]
    tab_id = msg["tabId"]
    tab = _tabs.get(tab_id)
    if tab is not None and tab.cdp_ws is not None:
        try:
            await tab.cdp_ws.close()
        except Exception:
            pass
        tab.cdp_ws = None
    await _send({"type": "response", "id": req_id, "result": {}})
    await _send({"type": "detached", "tabId": tab_id})


async def handle_command(msg: dict) -> None:
    """Forward a CDP command from the daemon to Chrome via the per-tab ws."""
    req_id = msg["id"]
    tab_id = msg["tabId"]
    method = msg["method"]
    params = msg.get("params") or {}
    tab = _tabs.get(tab_id)
    if tab is None:
        await _send({
            "type": "response",
            "id": req_id,
            "error": {"code": -32000, "message": f"no such tab {tab_id}"},
        })
        return
    try:
        cdp_resp = await _send_cdp(tab, method, params)
    except Exception as e:
        await _send({
            "type": "response",
            "id": req_id,
            "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"},
        })
        return
    if "error" in cdp_resp:
        await _send({"type": "response", "id": req_id,
                     "error": cdp_resp["error"]})
    else:
        await _send({"type": "response", "id": req_id,
                     "result": cdp_resp.get("result", {})})


# Dispatch table — keep keys exactly matching the relay's vocabulary.
HANDLERS = {
    "queryActiveTab": handle_query_active_tab,
    "attach": handle_attach,
    "detach": handle_detach,
    "command": handle_command,
}


async def _main(relay_url: str) -> None:
    global _relay_ws
    async with ws_connect(relay_url, max_size=None) as ws:
        _relay_ws = ws
        # Identify ourselves to the relay.
        await ws.send(json.dumps({
            "type": "hello",
            "installId": INSTALL_ID,
            "browser": "chrome",
            "version": "148.0.7778.168-ai-e2e-fake",
        }))
        # Tell the harness we're ready.
        print(
            f"[fake-ext] hello sent; relay={relay_url}, "
            f"upstream={UPSTREAM_HOST}:{UPSTREAM_PORT}",
            file=sys.stderr, flush=True,
        )
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            handler = HANDLERS.get(mtype)
            if handler is None:
                continue
            asyncio.create_task(handler(msg))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay-port", type=int,
                    default=int(os.environ.get("FAKE_EXT_RELAY_PORT", RELAY_PORT)))
    ap.add_argument("--upstream-port", type=int,
                    default=int(os.environ.get("FAKE_EXT_UPSTREAM_PORT", UPSTREAM_PORT)))
    args = ap.parse_args()
    RELAY_PORT = args.relay_port
    UPSTREAM_PORT = args.upstream_port
    url = f"ws://{RELAY_HOST}:{RELAY_PORT}/"
    try:
        asyncio.run(_main(url))
    except KeyboardInterrupt:
        pass
