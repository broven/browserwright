from __future__ import annotations

import json
import http.server
import shutil
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager

from .conftest import TEST_EXT_PORT, TEST_NAME, scrubbed_env
from .helpers import run_skill


def _enable_user_scripts_toggle(e2e_chrome) -> None:
    import asyncio
    import json as _json
    import urllib.request

    import websockets

    async def run() -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{e2e_chrome.port}/json/new?chrome://extensions/",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            tab = _json.loads(resp.read().decode("utf-8"))
        async with websockets.connect(tab["webSocketDebuggerUrl"], compression=None) as ws:
            next_id = 1

            async def send(method: str, params: dict | None = None) -> dict:
                nonlocal next_id
                req_id = next_id
                next_id += 1
                await ws.send(_json.dumps({"id": req_id, "method": method, "params": params or {}}))
                while True:
                    msg = _json.loads(await ws.recv())
                    if msg.get("id") == req_id:
                        if "error" in msg:
                            raise AssertionError(msg["error"])
                        return msg.get("result") or {}

            await send("Runtime.enable")
            await send("Page.enable")

            async def eval_js(expr: str) -> object:
                res = await send("Runtime.evaluate", {
                    "expression": expr,
                    "awaitPromise": True,
                    "returnByValue": True,
                })
                if "exceptionDetails" in res:
                    raise AssertionError(res["exceptionDetails"])
                return (res.get("result") or {}).get("value")

            ext_id = await eval_js("""new Promise((resolve) => {
              const deadline = Date.now() + 5000;
              function tick() {
                const manager = document.querySelector('extensions-manager');
                const list = manager && manager.shadowRoot && manager.shadowRoot.querySelector('extensions-item-list');
                const listRoot = list && list.shadowRoot;
                const item = listRoot && listRoot.querySelector('extensions-item');
                if (item && item.id) { resolve(item.id); return; }
                if (Date.now() > deadline) { resolve(null); return; }
                setTimeout(tick, 100);
              }
              tick();
            })""")
            assert ext_id, "could not find loaded extension id"
            await send("Page.navigate", {"url": "chrome://extensions/?id=" + ext_id})
            state = await eval_js("""new Promise((resolve) => {
              const deadline = Date.now() + 5000;
              function tick() {
                const manager = document.querySelector('extensions-manager');
                const detail = manager && manager.shadowRoot && manager.shadowRoot.querySelector('extensions-detail-view');
                const root = detail && detail.shadowRoot;
                const row = root && root.querySelector('#allow-user-scripts');
                if (row) {
                  const toggle = row.shadowRoot ? row.shadowRoot.querySelector('#crToggle') : row.querySelector('cr-toggle');
                  const checked = !!(toggle && (toggle.checked || toggle.getAttribute('aria-pressed') === 'true'));
                  if (!checked) (toggle || row).click();
                  resolve(checked ? 'already' : 'clicked');
                  return;
                }
                if (Date.now() > deadline) { resolve('missing'); return; }
                setTimeout(tick, 100);
              }
              tick();
            })""")
            assert state in {"already", "clicked"}, state

    asyncio.run(run())


class _E2EPageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<body>userscript e2e</body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@contextmanager
def _local_page_server():
    with socketserver.TCPServer(("127.0.0.1", 0), _E2EPageHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _last_json(stdout: str) -> dict:
    line = next(
        ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{")
    )
    return json.loads(line)


def _daemon_userscript(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    daemon_bin = shutil.which("browser-daemon") or "browser-daemon"
    env = scrubbed_env()
    env["BD_NAME"] = TEST_NAME
    env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return subprocess.run(
        [daemon_bin, "userscript", *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def test_userscript_install_inject_toggle_logs_remove(ext_ready, e2e_chrome, tmp_path):
    _enable_user_scripts_toggle(e2e_chrome)

    userjs = tmp_path / "e2e.user.js"
    userjs.write_text(
        "// ==UserScript==\n"
        "// @name E2E Sentinel\n"
        "// @namespace bd.e2e\n"
        "// @match http://127.0.0.1/*\n"
        "// @run-at document-idle\n"
        "// ==/UserScript==\n"
        "function set(){ document.documentElement.setAttribute('data-us-e2e', 'ok'); }\n"
        "if (document.documentElement) set();\n"
        "else document.addEventListener('DOMContentLoaded', set, {once:true});\n",
        encoding="utf-8",
    )

    pushed = _daemon_userscript(["push", str(userjs)])
    assert pushed.returncode == 0, pushed.stderr
    pushed_payload = json.loads(pushed.stdout)
    assert pushed_payload.get("sync", {}).get("ok") is True, pushed_payload
    assert pushed_payload.get("sync", {}).get("registered") == 1, pushed_payload
    script_id = pushed_payload["id"]
    identity = pushed_payload["identity"]

    with _local_page_server() as url:
        probe = run_skill(
            script=(
                "import json\n"
                f"open_background({url!r})\n"
                "wait_for_load()\n"
                'print(json.dumps({"sentinel": js("document.documentElement && document.documentElement.getAttribute(\'data-us-e2e\')")}))\n'
            ),
            backend="extension",
            timeout=60,
        )
        assert probe.returncode == 0, probe.stderr
        assert _last_json(probe.stdout)["sentinel"] == "ok"

        # Injection on the matching page must have appended an audit-log entry.
        deadline = time.monotonic() + 5.0
        while True:
            logs = _daemon_userscript(["logs", "--id", script_id, "--limit=20"])
            assert logs.returncode == 0, logs.stderr
            if any(entry.get("id") == script_id for entry in json.loads(logs.stdout)["logs"]):
                break
            if time.monotonic() > deadline:
                all_logs = _daemon_userscript(["logs", "--limit=50"])
                raise AssertionError(f"missing audit log for {script_id}: filtered={logs.stdout} all={all_logs.stdout}")
            time.sleep(0.2)

        toggled = _daemon_userscript(["toggle", identity, "--enabled=false"])
        assert toggled.returncode == 0, toggled.stderr
        disabled_probe = run_skill(
            script=(
                "import json\n"
                f"open_background({url!r})\n"
                "wait_for_load()\n"
                'print(json.dumps({"sentinel": js("document.documentElement && document.documentElement.getAttribute(\'data-us-e2e\')")}))\n'
            ),
            backend="extension",
            timeout=60,
        )
        assert disabled_probe.returncode == 0, disabled_probe.stderr
        assert _last_json(disabled_probe.stdout)["sentinel"] is None

    removed = _daemon_userscript(["remove", identity])
    assert removed.returncode == 0, removed.stderr
    listed = _daemon_userscript(["list", "--site=http://127.0.0.1/e2e"])
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["scripts"] == []
