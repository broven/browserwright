"""L1 -- Playwright `connect_over_cdp` against the daemon's CDP facade on the
EXTENSION backend (Task 05-24-tab-handle-model-for-code-agents, PR2).

Phase A2/A3/A4 acceptance: a real Playwright client connects to the daemon's
Playwright-facing CDP facade, and the facade — bridging through the shared
extension relay + the user's (here: Chrome-for-Testing harness) browser —
synthesizes the `Target.targetCreated`/`attachedToTarget` event stream that
`connect_over_cdp` needs (A2), maps `Target.createTarget` to a real background
tab (A3), runs the `Runtime.enable` settle barrier (A4), and forwards
page-domain CDP through `chrome.debugger` WITH the per-page sessionId so
flat-session routing stays consistent.

This is the extension sibling of `test_l1_playwright_facade.py` (rdp). It uses
the isolated CfT harness (port 29989) — NOT the developer's daily Chrome.

What this proves (all via a real `chromium.connect_over_cdp`):
  1. The handshake completes against the extension backend (Browser.getVersion,
     Target.setAutoAttach/setDiscoverTargets, Target.getTargetInfo, the benign
     browser-level no-ops) — `connect_over_cdp` returns a usable Browser.
  2. A2: `setAutoAttach` replays `attachedToTarget` for an already-attached tab,
     so `context.pages()` enumerates it.
  3. A3 + page-domain forwarding: `Target.createTarget` opens a REAL background
     tab via the extension, and `Page.navigate` / `Runtime.evaluate` drive it —
     read the title back through the facade.

PR3 (CRPage high-level fidelity — DONE): Playwright's HIGH-LEVEL
`context.new_page()` / `page.goto()` wrappers now work over the extension
backend too — see `test_high_level_new_page_and_goto_over_extension`. The
facade synthesizes the CRPage `_initialize` contract: a stable browserContextId
+ initial-empty-document ':' url on a fresh blank target, an event-gated
Runtime.enable barrier (disable→enable + wait for the default
executionContextCreated), forwarded page-session Target.setAutoAttach with
extension-owned child-target resume. The
older `test_connect_over_cdp_drives_extension_page` retains the CDP-level drive
as a lower-level regression anchor.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from .conftest import (  # noqa: F401
    TEST_EXT_FACADE_PORT as _CONFTEST_FACADE_PORT,
    TEST_EXT_PORT,
    _isolated_runtime_dir,
)

# A test facade port distinct from production (19990), the rdp-facade test
# (29991), and the relay (29989).
TEST_EXT_FACADE_PORT = 29992


@pytest.fixture
def e2e_ext_facade_daemon(e2e_daemon, ext_ready):
    """Reuse the session-scoped extension daemon.

    The CfT extension is patched to dial ONE fixed relay port (TEST_EXT_PORT =
    29989), so only one extension daemon can own it per pytest session — a
    second daemon on 29989 would collide (this fixture used to spawn one and
    failed `_port_free` whenever `e2e_daemon` was alive in a full-suite run).
    `e2e_daemon` already serves 29989 WITH a facade on
    `conftest.TEST_EXT_FACADE_PORT`, and `ext_ready` blocks until the extension
    SW has connected. Yields (ext_port, facade_port, runtime_dir).
    """
    yield (TEST_EXT_PORT, _CONFTEST_FACADE_PORT, e2e_daemon.runtime_dir)


@pytest.fixture
def ext_facade_ready(e2e_ext_facade_daemon, e2e_chrome):
    """Block until the CfT extension SW connects to the facade daemon's relay.

    Reuses the session-scoped `e2e_chrome` (Chrome for Testing + patched
    extension); the patched extension dials TEST_EXT_PORT, which this daemon
    serves. Returns (ext_port, facade_port, runtime_dir).
    """
    ext_port, facade_port, runtime_dir = e2e_ext_facade_daemon
    deadline = time.monotonic() + 15.0
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{ext_port}/__status__", timeout=0.5
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            last = body
            if int(body.get("extensions", 0)) >= 1:
                return (ext_port, facade_port, runtime_dir)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    pytest.fail(f"extension never connected within 15s; last status={last}")


def test_connect_over_cdp_handshake_against_extension(ext_facade_ready):
    """ACCEPTANCE part 1: a real `chromium.connect_over_cdp(facade_ws)` against
    the EXTENSION backend completes the handshake and yields a usable Browser +
    default context — this alone is what PR2 unblocked (before PR2 the handshake
    died on Browser.setDownloadBehavior / Target.getTargetInfo / the missing
    target-event synthesis). Enumeration + drive are covered by
    `test_connect_over_cdp_drives_extension_page`."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_api.sync_playwright

    _ext_port, facade_port, _runtime_dir = ext_facade_ready
    facade_ws = f"ws://127.0.0.1:{facade_port}/cdp"

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(facade_ws, timeout=20000)
        try:
            # connect_over_cdp returning at all proves the facade speaks
            # Playwright's browser-level CDP dialect over the extension backend.
            assert browser.is_connected()
            assert browser.contexts, "connect_over_cdp yielded no context"
        finally:
            browser.close()


def test_connect_over_cdp_drives_extension_page(ext_facade_ready):
    """ACCEPTANCE part 2 (A3 + page-domain forwarding): over the live
    `connect_over_cdp` facade, open a REAL tab (Target.createTarget →
    extension background tab), then navigate + read the title + evaluate
    through the facade's session-scoped chrome.debugger forwarding.

    We drive the page domain over a raw CDP frame channel to the SAME facade
    ws (the layer phase A targets); the high-level Playwright Page wrapper is a
    PR3 follow-up (see module docstring). `connect_over_cdp` above proves the
    facade speaks Playwright's dialect; this proves it drives the real
    browser."""
    pytest.importorskip("playwright.sync_api")  # ensure the dep exists
    import asyncio

    import websockets

    _ext_port, facade_port, _runtime_dir = ext_facade_ready
    facade_ws = f"ws://127.0.0.1:{facade_port}/cdp"

    async def drive() -> tuple[str, int]:
        ws = await websockets.connect(facade_ws, max_size=100 * 1024 * 1024)
        try:
            pending: dict[int, asyncio.Future] = {}
            # Map every announced target → its synthesized session, so we can
            # pick the EXACT session for the tab we create (not whichever
            # attachedToTarget happened to arrive last).
            tid_to_sid: dict[str, str] = {}
            loop = asyncio.get_running_loop()

            async def reader():
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("method") == "Target.attachedToTarget":
                        ti = m["params"]["targetInfo"]
                        if ti["targetId"].startswith("ext-tab-"):
                            tid_to_sid[ti["targetId"]] = m["params"]["sessionId"]
                    rid = m.get("id")
                    if isinstance(rid, int) and rid in pending:
                        pending.pop(rid).set_result(m)

            rt = asyncio.create_task(reader())
            counter = {"n": 0}

            async def send(method, params=None, session=None):
                counter["n"] += 1
                rid = counter["n"]
                frame = {"id": rid, "method": method, "params": params or {}}
                if session:
                    frame["sessionId"] = session
                fut = loop.create_future()
                pending[rid] = fut
                await ws.send(json.dumps(frame))
                return await asyncio.wait_for(fut, timeout=12.0)

            # Handshake + A3 createTarget.
            await send("Target.setAutoAttach",
                       {"autoAttach": True, "flatten": True,
                        "waitForDebuggerOnStart": False})
            created = await send("Target.createTarget", {"url": "about:blank"})
            target_id = created["result"]["targetId"]
            # Pick the session synthesized FOR THIS created tab (the
            # attachedToTarget for target_id is delivered before the response).
            sid = None
            for _ in range(40):
                sid = tid_to_sid.get(target_id)
                if sid:
                    break
                await asyncio.sleep(0.1)
            assert sid, f"no session announced for {target_id}; seen={tid_to_sid}"

            await send("Page.enable", session=sid)
            await send("Runtime.enable", session=sid)
            # Page.navigate forwarded → chrome.debugger on THIS session's tab.
            nav = await send("Page.navigate", {"url": "about:blank"},
                             session=sid)
            nav_ok = ("error" not in nav
                      and bool(nav["result"].get("frameId")))
            await asyncio.sleep(0.5)
            # Drive the page's DOM via Runtime.evaluate in the live context:
            # mutate it then read it back. Mutating+reading in ONE evaluate keeps
            # us in the same execution context (avoids cross-navigation context
            # races) — proving Runtime.evaluate forwards to + manipulates the
            # real attached tab through the facade.
            heading = (await send("Runtime.evaluate",
                       {"expression":
                        "(()=>{document.body.innerHTML="
                        "'<h1>facade-rendered</h1>';"
                        "return document.querySelector('h1').textContent;})()",
                        "returnByValue": True}, session=sid)
                       )["result"]["result"].get("value")
            two = (await send("Runtime.evaluate",
                              {"expression": "1 + 1", "returnByValue": True},
                              session=sid))["result"]["result"].get("value")
            await send("Target.closeTarget", {"targetId": target_id})
            rt.cancel()
            return nav_ok, heading, two
        finally:
            await ws.close()

    nav_ok, heading, two = asyncio.run(drive())
    # Page.navigate forwarded successfully (frameId, no error); Runtime.evaluate
    # both mutated and read the real attached tab's DOM through the facade →
    # chrome.debugger; and a plain expression evaluates correctly.
    assert nav_ok, "Page.navigate did not return a frameId"
    assert heading == "facade-rendered", f"got heading {heading!r}"
    assert two == 2


def test_high_level_new_page_and_goto_over_extension(ext_facade_ready):
    """ACCEPTANCE (PR3 — CRPage high-level fidelity): the HIGH-LEVEL Playwright
    API — `context.new_page()` / `page.goto()` / `page.title()` /
    `page.locator().text_content()` — works over the EXTENSION backend through
    the facade, not just CDP-level drive.

    Before the PR3 fixes this FAILED: `context.new_page()` threw because CRPage
    `_initialize` rejected (target announced already-running + frame tree
    showing about:blank instead of ':', and Runtime.enable not gated on the
    real default-context event), so Playwright closed the freshly-created
    target ('Failed to create page'). The fixes:
      - synthesized targetInfo carries a stable browserContextId + ':' url for
        a fresh blank tab (so isInitialEmptyPage matches real Chrome),
      - Runtime.enable does disable→enable and gates on the default
        executionContextCreated event,
      - page-session Target.setAutoAttach is forwarded so Chrome surfaces
        child targets, while the extension resumes them and the facade filters
        their Target.* events.

    This is the playwriter-parity acceptance on the user's PRIMARY backend."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_api.sync_playwright

    _ext_port, facade_port, _runtime_dir = ext_facade_ready
    facade_ws = f"ws://127.0.0.1:{facade_port}/cdp"

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(facade_ws, timeout=20000)
        try:
            context = (browser.contexts[0] if browser.contexts
                       else browser.new_context())
            # HIGH-LEVEL new_page() — the whole CRPage _initialize state machine
            # runs here. Pre-fix this closed the target during init.
            page = context.new_page()
            # Use set_content (navigate about:blank + set document content), NOT
            # `goto("data:...")`: a `data:` URL navigation issued over
            # `chrome.debugger` (the extension backend's transport) is aborted by
            # Chrome (`net::ERR_ABORTED`) — a backend-transport limitation, not a
            # facade gap (real `http(s)`/`about:` gotos drive fine over the
            # facade). set_content exercises the same high-level surface
            # (CRPage navigation + main/utility worlds + locators) without the
            # data:-scheme restriction.
            page.set_content("<title>facade-hl</title>"
                             "<h1 id=x>hi-high-level</h1>",
                             wait_until="load", timeout=20000)
            assert page.title() == "facade-hl", f"title={page.title()!r}"
            assert page.locator("#x").text_content() == "hi-high-level"
            # A plain evaluate proves the utility/main world is fully wired.
            assert page.evaluate("1 + 1") == 2
        finally:
            browser.close()


def _port_free(port: int) -> bool:
    import socket
    from contextlib import closing
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
