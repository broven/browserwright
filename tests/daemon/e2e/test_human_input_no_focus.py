"""Human input on a background tab must never activate a tab or focus a window.

The extension backend drives the user's own Chrome. Activating the session's
tab or raising its window steals the desktop's active window from the user.
This test puts the user somewhere else first — a newly focused window, plus a
newer active tab in the session tab's own window — then runs executor
click + fill + scroll-into-view on the (now background) session tab.

It asserts (a) the page saw the actions as trusted input and they took effect,
and (b) Chrome's own tab/window state is unchanged: the same active tab in
every window, the same focused window, and no `tabs.onActivated` /
`windows.onFocusChanged` event for the session tab or its window at any point
during the actions (so a transient activate-then-restore is caught too).
Chrome state is read from the extension's service worker over the harness's
CDP port, independently of the daemon.

Repeat with ``bash tests/daemon/e2e/run.sh
tests/daemon/e2e/test_human_input_no_focus.py -v``. The artifact is
``tests/daemon/e2e/_artifacts/human-input-no-focus.json``. Headful by default
(the meaningful case); ``BW_E2E_HEADLESS=1`` still exercises the contract.
"""
from __future__ import annotations

import json
import textwrap
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browserwright.cdp import CDPSession

from .helpers import bs_home, drop_ledger_session, run_skill, seed_ledger_session
from .test_l2_multisession import _extension_id_from_path, _extension_worker_target_id

_SESSION_HTML = """<!doctype html><meta charset="utf-8"><title>Session page</title>
<style>body{margin:24px;font:16px sans-serif} input,button{display:block;margin:8px;
width:200px;height:36px} .space{height:2400px}</style>
<label>Name<input id="name" value="old value"></label>
<button id="near" onclick="clicks.push('near')">Near</button>
<div class="space"></div>
<button id="far" onclick="clicks.push('far')">Far</button>
<script>
window.events = []; window.clicks = [];
for (const type of ['pointermove','mousedown','mouseup','click','keydown','keyup',
                    'input','wheel','scroll'])
  document.addEventListener(type, e => events.push({type, target: e.target.id || null,
    trusted: e.isTrusted, key: e.key || null}), true);
</script>"""

_USER_HTML = """<!doctype html><meta charset="utf-8"><title>User page</title>
<p>The user is reading this tab.</p>"""

_WATCH_JS = """(() => {
  globalThis.__bwFocusLog = [];
  const log = (kind) => (...args) => __bwFocusLog.push({kind, args, t: Date.now()});
  globalThis.__bwFocusListeners = [
    [chrome.tabs.onActivated, log('tabs.onActivated')],
    [chrome.tabs.onHighlighted, log('tabs.onHighlighted')],
    [chrome.windows.onFocusChanged, log('windows.onFocusChanged')],
  ];
  for (const [event, fn] of __bwFocusListeners) event.addListener(fn);
  return true;
})()"""

_STATE_JS = """(async () => {
  const windows = await chrome.windows.getAll({populate: true});
  let lastFocused = null;
  try { lastFocused = (await chrome.windows.getLastFocused()).id; } catch (e) {}
  return {
    lastFocusedWindow: lastFocused,
    windows: windows.map(w => ({id: w.id, focused: w.focused,
      activeTab: (w.tabs.find(t => t.active) || {}).id ?? null,
      tabs: w.tabs.map(t => ({id: t.id, active: t.active, url: t.url}))})),
  };
})()"""


class _Worker:
    """Evaluate in the extension's service worker over the browser CDP port."""

    def __init__(self, chrome, extension_id: str):
        self._cdp = CDPSession(chrome.ws_url)
        self._sid = self._cdp.attach(_extension_worker_target_id(self._cdp, extension_id))

    def eval(self, expression: str):
        result = self._cdp.send("Runtime.evaluate", session=self._sid,
                                expression=expression, returnByValue=True,
                                awaitPromise=True)
        if "exceptionDetails" in result:
            raise AssertionError(f"service-worker evaluation failed: {result!r}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self._cdp.close()


@pytest.fixture
def pages_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (_USER_HTML if self.path.startswith("/user") else _SESSION_HTML).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _wait_loaded(worker: _Worker, tab_id: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.eval(f"chrome.tabs.get({tab_id}).then(t => t.status)") == "complete":
            return
        time.sleep(0.1)
    raise AssertionError(f"tab {tab_id} did not finish loading")


def test_background_tab_human_input_never_steals_focus(
    e2e_daemon, e2e_chrome, ext_ready, patched_ext_dir, e2e_artifacts_dir, pages_site,
):
    pytest.importorskip("playwright.sync_api")
    artifact = e2e_artifacts_dir / "human-input-no-focus.json"
    artifact.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    session_url = f"{pages_site}/session?{token}"
    sid = f"e2e-nofocus-{token}"
    seed_ledger_session(bs_home("extension"), sid, backend="extension", name="nofocus")
    env = {"BD_SESSION": sid}
    worker = _Worker(e2e_chrome, _extension_id_from_path(patched_ext_dir))
    record: dict = {"session_url": session_url}
    try:
        # 1. Bind the session tab to the fixture page.
        setup = run_skill(f"page.goto({session_url!r}, wait_until='load')\n",
                          backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                          extra_env=env, timeout=60)
        assert setup.returncode == 0, (setup.stdout, setup.stderr)
        tabs = worker.eval(f"chrome.tabs.query({{}}).then(ts => ts.filter("
                           f"t => (t.url || '').includes({token!r})).map(t => t.id))")
        assert len(tabs) == 1, tabs
        session_tab = tabs[0]
        session_window = worker.eval(f"chrome.tabs.get({session_tab}).then(t => t.windowId)")

        # 2. The user moves on: a newer active tab in the session tab's own
        #    window, then a separate window that takes focus.
        user_tab = worker.eval(
            f"chrome.tabs.create({{windowId: {session_window}, "
            f"url: {pages_site + '/user-tab'!r}, active: true}}).then(t => t.id)")
        user_window = worker.eval(
            f"chrome.windows.create({{url: {pages_site + '/user-window'!r}, "
            f"focused: true}}).then(w => w.id)")
        user_window_tab = worker.eval(
            f"chrome.tabs.query({{windowId: {user_window}}}).then(ts => ts[0].id)")
        for tab in (user_tab, user_window_tab):
            _wait_loaded(worker, tab)
        time.sleep(0.5)  # let window-manager focus settle before the baseline
        before = worker.eval(_STATE_JS)
        record["before"] = before
        session_win_state = next(w for w in before["windows"] if w["id"] == session_window)
        assert session_win_state["activeTab"] == user_tab, before
        assert not any(t["active"] for t in session_win_state["tabs"]
                       if t["id"] == session_tab), before
        assert worker.eval(_WATCH_JS) is True

        # 3. Human input on the background session tab.
        script = textwrap.dedent("""
            import json, time
            out = {'visibility': page.evaluate('document.visibilityState')}
            t0 = time.monotonic()
            page.get_by_role('button', name='Near').click()
            out['click_s'] = time.monotonic() - t0
            t0 = time.monotonic()
            page.get_by_role('textbox', name='Name').fill('New Name!')
            out['fill_s'] = time.monotonic() - t0
            t0 = time.monotonic()
            page.get_by_role('button', name='Far').click()
            out['scroll_click_s'] = time.monotonic() - t0
            out['value'] = page.locator('#name').input_value()
            out['clicks'] = page.evaluate('clicks')
            out['scrollY'] = page.evaluate('scrollY')
            out['events'] = page.evaluate('events')
            from pathlib import Path
            Path(RESULT_PATH).write_text(json.dumps(out))
        """)
        result_path = e2e_artifacts_dir / f"human-input-no-focus-{token}.tmp.json"
        script = f"RESULT_PATH = {str(result_path)!r}\n" + script
        run = run_skill(script, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                        extra_env=env, timeout=90)
        record["run"] = {"returncode": run.returncode, "stdout": run.stdout,
                         "stderr": run.stderr}
        focus_log = worker.eval("globalThis.__bwFocusLog ?? null")
        after = worker.eval(_STATE_JS)
        record.update(focus_log=focus_log, after=after)
        assert run.returncode == 0, record["run"]
        result = json.loads(result_path.read_text())
        result_path.unlink()
        record["result"] = result

        # (a) The actions happened, through trusted browser input.
        assert result["clicks"] == ["near", "far"], result["clicks"]
        assert result["value"] == "New Name!", result["value"]
        assert result["scrollY"] > 0, result["scrollY"]
        events = result["events"]
        for kind in ("pointermove", "mousedown", "click", "keydown", "input", "wheel"):
            seen = [e for e in events if e["type"] == kind]
            assert seen and all(e["trusted"] for e in seen), (kind, seen)
        typed = "".join(e["key"] for e in events
                        if e["type"] == "keydown" and len(e["key"] or "") == 1)
        assert typed.endswith("New Name!"), typed

        # (b) Chrome's focus state is exactly what the user left it as.
        assert focus_log is not None, "service worker restarted; focus log lost"

        def activations(state):
            return {w["id"]: w["activeTab"] for w in state["windows"]}

        assert activations(after) == activations(before), (before, after)
        assert after["lastFocusedWindow"] == before["lastFocusedWindow"], (before, after)
        assert ([w["focused"] for w in after["windows"]]
                == [w["focused"] for w in before["windows"]]), (before, after)
        assert not any(e["kind"] == "tabs.onActivated"
                       and e["args"][0].get("tabId") == session_tab
                       for e in focus_log), focus_log
        assert not any(e["kind"] == "windows.onFocusChanged"
                       and e["args"][0] == session_window
                       for e in focus_log), focus_log
        assert not any(e["kind"] == "tabs.onHighlighted"
                       and session_tab in (e["args"][0].get("tabIds") or [])
                       for e in focus_log), focus_log
    finally:
        artifact.write_text(json.dumps(record, indent=2) + "\n")
        try:
            worker.eval("""(() => { for (const [e, f] of globalThis.__bwFocusListeners || [])
                e.removeListener(f); return true; })()""")
        finally:
            worker.close()
        drop_ledger_session(bs_home("extension"), sid)
