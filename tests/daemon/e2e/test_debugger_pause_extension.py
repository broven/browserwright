"""Extension-only Chrome remains usable on pages containing debugger statements.

Repeat with ``uv run pytest tests/daemon/e2e/test_debugger_pause_extension.py -v``.
Chrome has no remote debugging port; the agent talks only to chrome.debugger
through the extension. ``_artifacts/debugger-pause-extension.json`` records the
navigation, evaluation, real form input, default console text and executor rebind.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import uuid

import pytest

from .conftest import published_endpoint
from .helpers import bs_home, drop_ledger_session, run_skill, seed_ledger_session


_HTML = """<!doctype html><meta charset="utf-8"><title>Debugger fixture</title>
<label>Name<input id="name"></label><button id="submit">Submit</button>
<p id="result">Ready</p><iframe srcdoc="<p>Child context</p>"></iframe><script>
window.fixtureTicks = 0;
console.log('fixture startup', 42);
console.warn('fixture startup warning', 1);
console.error('fixture startup error', 2);
debugger;
setInterval(() => { debugger; fixtureTicks++; }, 16);
document.querySelector('#submit').addEventListener('click', () => {
  debugger;
  document.querySelector('#result').textContent =
    'Submitted: ' + document.querySelector('#name').value;
  console.log('fixture submitted', document.querySelector('#name').value);
});
</script>"""


@pytest.fixture
def debugger_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def e2e_chrome(extension_only_chrome):
    """Override the usual harness: no Chrome CDP socket or DevTools connection."""
    return extension_only_chrome


def test_debugger_statements_do_not_pause_agent_calls_or_loaded_page_rebind(
    e2e_daemon, e2e_chrome, ext_ready, e2e_artifacts_dir, debugger_site,
):
    pytest.importorskip("playwright.sync_api")
    assert not (e2e_chrome.profile_path / "DevToolsActivePort").exists()
    sid = f"e2e-debugger-{uuid.uuid4().hex}"
    seed_ledger_session(bs_home("extension"), sid, backend="extension", name="debugger")
    artifact = e2e_artifacts_dir / "debugger-pause-extension.json"
    progress = artifact.with_suffix(".progress.json")
    progress.unlink(missing_ok=True)
    events = []
    observer_ready = threading.Event()
    observer_stop = threading.Event()

    def observe():
        # Subscribe to the same session's synthesized target announcements.
        # No page domain or debugger command is sent by this observer.
        from websockets.sync.client import connect
        endpoint = published_endpoint(e2e_daemon.runtime_dir)
        with connect(endpoint.replace("http://", "ws://") + f"/cdp?session={sid}") as ws:
            ws.send(json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {
                "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}}))
            while not observer_stop.is_set():
                try:
                    event = json.loads(ws.recv(timeout=0.2))
                except TimeoutError:
                    continue
                if event.get("id") == 1:
                    observer_ready.set()
                if event.get("method") in {"Debugger.paused", "Debugger.resumed", "Page.frameNavigated"}:
                    events.append(event)

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    assert observer_ready.wait(5), "passive extension event observer did not connect"
    evidence = {"remote_debugging_port": False, "stages": [], "native_events": events}
    script = f"site = {debugger_site!r}\nprogress_path = {str(progress)!r}\n" + '''
import json
from pathlib import Path
progress = []
def note(stage, **observations):
    progress.append({'stage': stage, **observations})
    Path(progress_path).write_text(json.dumps(progress, indent=2) + '\\n')
note('bound')
messages = []
page.on('console', lambda message: messages.append({'level': message.type, 'text': message.text}))
context_events = []
context_observer = context.new_cdp_session(page)
context_observer.on('Runtime.executionContextCreated',
                    lambda event: context_events.append({'type':'created', **event}))
context_observer.on('Runtime.executionContextDestroyed',
                    lambda event: context_events.append({'type':'destroyed', **event}))
page.set_default_timeout(8000)
page.goto(site, timeout=8000)
note('navigated')
print('NAVIGATED', flush=True)
assert page.evaluate('() => { debugger; return 42; }') == 42
print('EVALUATED', flush=True)
note('evaluated')
benchmark = """() => {
    const iterations = 100000;
    let start = performance.now();
    for (let i=0; i<iterations; i++) { void 0; }
    const noopMs = performance.now() - start;
    start = performance.now();
    for (let i=0; i<iterations; i++) { debugger; }
    return {iterations, noop_ms:noopMs, debugger_ms:performance.now() - start};
}"""
for url in [site, site.replace('127.0.0.1', 'localhost'), site]:
    if page.url != url + '/':
        note('navigating-loop', url=url)
        page.goto(url, timeout=8000)
        note('navigated-loop', url=page.url)
    cost = page.evaluate(benchmark)
    note('debugger-cost', url=page.url, contexts=list(context_events), **cost)
    print('DEBUGGER_COST ' + json.dumps(cost), flush=True)
    assert cost['debugger_ms'] <= max(50, cost['noop_ms'] * 20), cost
for expected in [{'level':'log', 'text':'fixture startup 42'},
                 {'level':'warning', 'text':'fixture startup warning 1'},
                 {'level':'error', 'text':'fixture startup error 2'}]:
    assert messages.count(expected) >= 3, (expected, messages)
note('startup-console-across-navigation', messages=messages)
child = page.frames[1]
note('scriptless-child-bound', url=child.url, contexts=list(context_events))
child.wait_for_load_state()
note('scriptless-child-loaded')
assert child.evaluate('document.body.innerText') == 'Child context'
note('scriptless-child-evaluated')
assert child.locator('body').inner_text() == 'Child context'
note('scriptless-child-context')
assert 'Name' in snapshot()
page.get_by_role('textbox', name='Name').fill('Ada Lovelace')
page.get_by_role('button', name='Submit').click()
page.get_by_text('Submitted: Ada Lovelace', exact=True).wait_for()
assert 'Submitted: Ada Lovelace' in read_markdown()
page.evaluate("console.log('plain text', 42)")
page.evaluate("console.warn('warning text', 1); console.error('error text', 2)")
page.wait_for_timeout(100)
assert {'level':'log', 'text':'plain text 42'} in messages, messages
assert {'level':'warning', 'text':'warning text 1'} in messages, messages
assert {'level':'error', 'text':'error text 2'} in messages, messages
before = page.evaluate('fixtureTicks')
page.wait_for_timeout(80)
assert page.evaluate('fixtureTicks') > before
print('OBSERVATIONS ' + json.dumps({'messages':messages, 'ticks':page.evaluate('fixtureTicks')}), flush=True)
note('input-console-heartbeat', messages=messages, ticks=page.evaluate('fixtureTicks'))
context_observer.detach()
'''

    def record(script, label):
        try:
            result = run_skill(script, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                               extra_env={"BD_SESSION": sid}, timeout=45)
            stage = {"label": label, "returncode": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr}
        except subprocess.TimeoutExpired as exc:
            def as_text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value
            stage = {"label": label, "timeout": exc.timeout,
                     "stdout": as_text(exc.stdout), "stderr": as_text(exc.stderr)}
        evidence["stages"].append(stage)
        if progress.exists():
            evidence["progress"] = json.loads(progress.read_text())
        artifact.write_text(json.dumps(evidence, indent=2) + "\n")
        assert stage.get("returncode") == 0, evidence

    try:
        record(script, "navigation-evaluation-input-console")
        record("reset()", "reset-loaded-page")
        record("messages = []\npage.on('console', lambda message: messages.append({'level':message.type,'text':message.text}))\n"
               "assert page.evaluate('() => { debugger; return 42; }') == 42\n"
               "assert page.get_by_text('Submitted: Ada Lovelace', exact=True).count() == 1\n"
               "before = page.evaluate('fixtureTicks')\npage.wait_for_timeout(80)\n"
               "assert page.evaluate('fixtureTicks') > before\npage.reload()\n"
               "page.wait_for_timeout(100)\n"
               "assert {'level':'log','text':'fixture startup 42'} in messages, messages\n"
               "assert {'level':'warning','text':'fixture startup warning 1'} in messages, messages\n"
               "assert {'level':'error','text':'fixture startup error 2'} in messages, messages\n"
               "print('REBOUND_OK', messages)",
               "rebind-loaded-page")
        observed_urls = {event.get("params", {}).get("frame", {}).get("url")
                         for event in events if event["method"] == "Page.frameNavigated"}
        assert debugger_site + "/" in observed_urls, evidence
        assert debugger_site.replace("127.0.0.1", "localhost") + "/" in observed_urls, evidence
        assert not [event for event in events if event["method"] == "Debugger.paused"], evidence
    finally:
        observer_stop.set()
        observer.join(timeout=2)
        artifact.write_text(json.dumps(evidence, indent=2) + "\n")
        drop_ledger_session(bs_home("extension"), sid)
