"""L2 -- real extension service-worker idle recovery.

This uses the real e2e harness: Chrome for Testing launches with the patched
unpacked extension and an isolated daemon relay port. The test drives the
extension service worker over Chrome's CDP to simulate the stale-OPEN state
that caused GitHub #8, then verifies the real ``maintainLoop`` recovery path
opens a fresh websocket to the daemon.
"""
from __future__ import annotations

import json
import time
import urllib.request

from browserwright.cdp import CDPSession

from ._real_browser import extension_id_from_path


def _extension_worker_target_id(cdp: CDPSession, extension_id: str) -> str:
    prefix = f"chrome-extension://{extension_id}/"
    cdp.send("Target.setDiscoverTargets", discover=True)
    deadline = time.monotonic() + 10.0
    last: dict | None = None
    while time.monotonic() < deadline:
        targets = cdp.send("Target.getTargets")
        last = targets
        for info in targets.get("targetInfos", []):
            if (info.get("type") == "service_worker"
                    and info.get("url", "").startswith(prefix)):
                return info["targetId"]
        time.sleep(0.2)
    raise AssertionError(f"extension service worker not found: {last!r}")


def _status(port: int) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/__status__", timeout=0.5,
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _eval_sw(cdp: CDPSession, session_id: str, expression: str) -> object:
    result = cdp.send(
        "Runtime.evaluate",
        session=session_id,
        expression=expression,
        returnByValue=True,
        awaitPromise=True,
    )
    if "exceptionDetails" in result:
        raise AssertionError(f"service worker eval failed: {result!r}")
    return result.get("result", {}).get("value")


def test_extension_maintain_loop_reconnects_stale_open_websocket(
    ext_ready, e2e_daemon, e2e_chrome, patched_ext_dir,
):
    """A stale OPEN websocket is no longer trusted by the real extension.

    We cannot ask Chrome to fabricate a ghost network-process websocket, so the
    test sets the SW health timestamps stale and lets the running maintainLoop
    wake up and handle it. That exercises the production ``background.js``
    recovery branch in a real loaded extension, not a JS unit stub.
    """
    before = _status(e2e_daemon.ext_port)
    install_ids = before.get("install_ids") or []
    assert install_ids, before
    install_id = install_ids[0]

    cdp = CDPSession(e2e_chrome.ws_url)
    try:
        worker = _extension_worker_target_id(
            cdp, extension_id_from_path(patched_ext_dir))
        session_id = cdp.attach(worker)

        value = _eval_sw(
            cdp,
            session_id,
            """
            (() => {
              globalThis.__zer110OldWs = ws;
              lastPongTs = Date.now() - 60000;
              lastInboundFrameTs = Date.now() - 60000;
              if (ws) ws.onmessage = null;
              return {hadWs: !!globalThis.__zer110OldWs};
            })()
            """,
        )
        assert isinstance(value, dict)
        assert value.get("hadWs") is True

        deadline = time.monotonic() + 10.0
        last: dict | None = None
        while time.monotonic() < deadline:
            last = _status(e2e_daemon.ext_port)
            if install_id in (last.get("install_ids") or []):
                state = _eval_sw(
                    cdp,
                    session_id,
                    """
                    (() => ({
                      sameWs: ws === globalThis.__zer110OldWs,
                      readyState: ws ? ws.readyState : -1,
                      open: ws && ws.readyState === WebSocket.OPEN,
                    }))()
                    """,
                )
                if isinstance(state, dict) and state.get("open") and not state.get("sameWs"):
                    break
            time.sleep(0.2)
        else:
            raise AssertionError(f"extension did not reconnect; last status={last!r}")
    finally:
        cdp.close()
