"""cdp --attach to a browser across a slow, jittery link.

Field symptom (an attached browser over a tailnet relay, 0.3-1 s RTT): a fixed
`goto -> snapshot -> fill -> click` script intermittently died with
`TargetClosedError: ... Target page, context or browser has been closed`, and
the in-place rebind then failed with `'NoneType' object has no attribute
'new_page'`. The tab stayed alive and the daemon logged nothing.

Two defects, one scenario each:

1. The facade bridge's upstream websocket ran websockets' own keepalive: one
   pong late by 20 s closed it with 1011. A pong travels behind the data, so a
   large reply on a slow link (here a ~1.5 MB aria snapshot at 30 KB/s) delays
   it past that although frames arrive the whole time. Playwright saw its
   browser disconnect.
2. The executor then treated "my connection dropped" as "my tab died": it
   cleared the session's (live) tab from the ledger and re-bound on a context
   that no longer existed, then recycled itself and lost `state`.

The rig, all on this machine: the test Chrome reached through a TCP forwarder
that delays every chunk by a random 100-500 ms and caps throughput, plus a
client on its own direct CDP connection that keeps creating, activating and
closing a background target while the agent script runs (the shape of the
field's probe / popups). Artifact: `cdp-attach-slow-link.json`.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import websockets

from .conftest import TEST_CDP_PORT, published_endpoint, scrubbed_env

_BS_HOME = Path(__file__).resolve().parent / "_bs_home" / "cdp"
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


class _SlowLink:
    """127.0.0.1:<port> -> the test Chrome. Every chunk is delayed by a random
    ``lo``-``hi`` seconds (order kept per direction) and, with ``rate``,
    throughput is capped at that many bytes/s per direction."""

    def __init__(self, lo: float = 0.0, hi: float = 0.0, rate: float = 0.0):
        self.lo, self.hi, self.rate = lo, hi, rate
        self.bytes = 0
        self._conns: list[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

        async def _serve():
            self._srv = await asyncio.start_server(self._forward, "127.0.0.1", 0)
            return self._srv.sockets[0].getsockname()[1]
        self.port = asyncio.run_coroutine_threadsafe(_serve(), self.loop).result(5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def drop_newest(self) -> None:
        """Reset the most recently opened live connection — the facade
        bridge, which the executor opens after the session's own upstream."""
        def _drop():
            live = [c for c in self._conns if not c[0].is_closing()]
            for w in live[-1]:
                w.transport.abort()
        self.loop.call_soon_threadsafe(_drop)

    def close(self) -> None:
        def _close():
            self._srv.close()
            for pair in self._conns:
                for w in pair:
                    w.transport.abort()
        self.loop.call_soon_threadsafe(_close)
        time.sleep(0.2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)

    async def _pipe(self, reader, writer):
        q: asyncio.Queue = asyncio.Queue()

        async def deliver():
            while (item := await q.get()) is not None:
                due, data = item
                await asyncio.sleep(max(0.0, due - time.monotonic()))
                writer.write(data)
                await writer.drain()

        sender = asyncio.create_task(deliver())
        due = 0.0
        try:
            while data := await reader.read(16384):
                self.bytes += len(data)
                # Monotone due times keep each direction's bytes in order.
                due = max(due, time.monotonic() + random.uniform(self.lo, self.hi))
                if self.rate:
                    due += len(data) / self.rate
                q.put_nowait((due, data))
        except (ConnectionError, OSError):
            pass
        finally:
            q.put_nowait(None)
            try:
                await sender
            except (ConnectionError, OSError):
                pass
            writer.close()

    async def _forward(self, reader, writer):
        ur, uw = await asyncio.open_connection("127.0.0.1", TEST_CDP_PORT)
        self._conns.append((writer, uw))
        await asyncio.gather(self._pipe(reader, uw), self._pipe(ur, writer),
                             return_exceptions=True)


class _TargetChurn:
    """create(background) -> activate -> close, in a loop, on a direct CDP ws."""

    def __init__(self, browser_ws: str):
        self.browser_ws = browser_ws
        self.cycles = 0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()),
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(15)

    async def _run(self):
        async with websockets.connect(self.browser_ws, max_size=None) as ws:
            n = 0

            async def call(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n:
                        if "error" in msg:
                            raise RuntimeError(f"{method}: {msg['error']}")
                        return msg.get("result", {})

            while not self._stop.is_set():
                try:
                    tid = (await call("Target.createTarget", url="about:blank",
                                      background=True))["targetId"]
                    await call("Target.activateTarget", targetId=tid)
                    await asyncio.sleep(0.2)
                    await call("Target.closeTarget", targetId=tid)
                    self.cycles += 1
                except Exception as e:  # noqa: BLE001 - recorded, keep churning
                    self.errors.append(repr(e)[:200])
                await asyncio.sleep(0.3)


def _cli(args: list[str], runtime_dir: str, stdin: str | None = None,
         timeout: float = 300.0):
    env = {k: v for k, v in scrubbed_env().items() if k not in _PROXY_VARS}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BW_DAEMON_URL"] = published_endpoint(runtime_dir) or ""
    env["BS_HOME"] = str(_BS_HOME)
    assert env["BW_DAEMON_URL"], "test cdp daemon did not publish its endpoint"
    binary = Path(sys.executable).with_name("browserwright")
    return subprocess.run([str(binary), *args], input=stdin, capture_output=True,
                          text=True, env=env, timeout=timeout)


def _run(sid: str, runtime_dir: str, code: str, timeout: int = 240):
    return _cli(["-s", sid, "--timeout", str(timeout), "--code-stdin"],
                runtime_dir, stdin=code, timeout=timeout + 60)


def _record(artifacts: Path, key: str, value: dict) -> None:
    path = artifacts / "cdp-attach-slow-link.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    report[key] = value
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _summary(proc) -> dict:
    return {"exit_code": proc.returncode, "stdout_tail": proc.stdout[-800:],
            "stderr_tail": proc.stderr[-1500:]}


_FORM = ("data:text/html,<title>slow-link</title>"
         "<label>Name <input id=n></label><button onclick=\"document.title="
         "'clicked-'+document.getElementById('n').value\">Go</button>")

#: ~1.5 MB of aria snapshot on the wire: at 30 KB/s that is ~50 s, so at
#: least one 20 s keepalive ping is answered more than 20 s late.
_BIG_DOM_ITEMS = 12000

# `human=False`: human input costs many sequential round trips, which on this
# link alone outlasts the 30 s action timeout; that is not what is under test.
_HEAVY_SCRIPT = f"""
import time
page.goto({_FORM!r})
page.evaluate('''n => {{
  const ul = document.createElement('ul');
  for (let i = 0; i < n; i++) {{
    const li = document.createElement('li');
    li.innerHTML = '<a href=#' + i + '>item ' + i + ' lorem ipsum dolor sit amet</a>';
    ul.appendChild(li);
  }}
  document.body.appendChild(ul);
}}''', {_BIG_DOM_ITEMS})
t = time.monotonic()
tree = page.aria_snapshot(mode="ai", timeout=180_000)
print("SNAPSHOT", len(tree), "chars in", round(time.monotonic() - t), "s")
page.get_by_role("textbox", name="Name").fill("ok", human=False)
page.get_by_role("button", name="Go").click(human=False)
print("TITLE", page.title())
"""


def test_large_reply_on_a_slow_link_keeps_the_browser_connected(
        e2e_cdp_daemon, e2e_chrome_cdp, e2e_artifacts_dir):
    link = _SlowLink(lo=0.1, hi=0.5, rate=30_000)
    try:
        new = _cli(["session", "new", "--backend=cdp", f"--attach={link.url}",
                    "--name=slow-link"], e2e_cdp_daemon)
        assert new.returncode == 0, new.stderr
        sid = new.stdout.strip().splitlines()[-1]
        try:
            with _TargetChurn(e2e_chrome_cdp.ws_url) as churn:
                t0 = time.monotonic()
                ran = _run(sid, e2e_cdp_daemon, _HEAVY_SCRIPT)
                elapsed = round(time.monotonic() - t0, 1)
        finally:
            _cli(["session", "end", sid], e2e_cdp_daemon)
    finally:
        link.close()
    _record(e2e_artifacts_dir, "large_reply_on_slow_link", {
        "link": {"jitter_s": [link.lo, link.hi], "bytes_per_s": link.rate},
        "bytes_forwarded": link.bytes, "elapsed_s": elapsed,
        "churn_cycles": churn.cycles, "churn_errors": churn.errors[:5],
        **_summary(ran)})
    assert churn.cycles > 0
    assert ran.returncode == 0, ran.stderr
    assert "TITLE clicked-ok" in ran.stdout, ran.stdout


def test_dropped_bridge_reconnects_to_the_same_tab_with_state(
        e2e_cdp_daemon, e2e_chrome_cdp, e2e_artifacts_dir):
    link = _SlowLink()
    try:
        new = _cli(["session", "new", "--backend=cdp", f"--attach={link.url}",
                    "--name=dropped-bridge"], e2e_cdp_daemon)
        assert new.returncode == 0, new.stderr
        sid = new.stdout.strip().splitlines()[-1]
        try:
            first = _run(sid, e2e_cdp_daemon, (
                f"page.goto({_FORM!r})\n"
                "state['marker'] = 'kept'\n"
                "page.evaluate('window.__tab_marker = 42')\n"
                "print('BOUND', page.title())\n"
                "print('TABS', len(context.pages))\n"))
            assert first.returncode == 0, first.stderr

            # Drop the bridge while a call is running on it; the session's own
            # upstream (an older connection) stays up, as in the field.
            timer = threading.Timer(3.0, link.drop_newest)
            timer.start()
            try:
                dropped = _run(sid, e2e_cdp_daemon, (
                    "import time\n"
                    "end = time.monotonic() + 15\n"
                    "while time.monotonic() < end:\n"
                    "    page.title()\n"
                    "    time.sleep(0.1)\n"))
            finally:
                timer.cancel()

            after = _run(sid, e2e_cdp_daemon, (
                "print('STATE', state.get('marker'))\n"
                "print('TAB', page.evaluate('window.__tab_marker'))\n"
                "print('TABS', len(context.pages))\n"))
        finally:
            _cli(["session", "end", sid], e2e_cdp_daemon)
    finally:
        link.close()
    _record(e2e_artifacts_dir, "dropped_bridge", {
        "first": _summary(first), "dropped": _summary(dropped),
        "after": _summary(after)})

    # The drop surfaces as the connection, not as a dead tab ...
    assert dropped.returncode != 0, dropped.stdout
    assert "connection to the browser dropped" in dropped.stderr, dropped.stderr
    assert "new_page" not in dropped.stderr, dropped.stderr
    # ... and the next call is back on the SAME tab, with `state` kept.
    assert after.returncode == 0, after.stderr
    assert "STATE kept" in after.stdout, after.stdout
    assert "TAB 42" in after.stdout, after.stdout
    # No replacement tab was opened.
    tabs = next(line for line in first.stdout.splitlines()
                if line.startswith("TABS"))
    assert tabs in after.stdout, (first.stdout, after.stdout)
