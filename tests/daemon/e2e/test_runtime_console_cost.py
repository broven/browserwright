"""Real-Chrome console overhead and execution-context lifecycle evidence.

Run ``uv run pytest tests/daemon/e2e/test_runtime_console_cost.py -v``.
The JSON artifact compares table serialization with the Runtime subscription
off/on, and records main-world evaluation after navigation and executor reset.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import uuid

import pytest

from .helpers import bs_home, drop_ledger_session, run_skill, seed_ledger_session


@pytest.fixture
def e2e_chrome(extension_only_chrome):
    return extension_only_chrome


@pytest.fixture
def context_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            title = "Context fixture" if self.path == "/" else "Next context"
            child = '<iframe srcdoc="<p>Child context</p>"></iframe>' if self.path == "/frame" else ""
            body = f"<title>{title}</title><p>Changed</p>{child}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
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


def test_console_subscription_is_opt_in_and_contexts_survive_navigation(
    e2e_daemon, ext_ready, e2e_artifacts_dir, context_site,
):
    pytest.importorskip("playwright.sync_api")
    sid = f"e2e-console-{uuid.uuid4().hex}"
    seed_ledger_session(bs_home("extension"), sid, backend="extension", name="console")
    artifact = e2e_artifacts_dir / "runtime-console-cost.json"
    artifact.unlink(missing_ok=True)
    script = f"artifact_path = {str(artifact)!r}\nsite = {context_site!r}\n" + '''
import json
from pathlib import Path
page.goto(site)
page.evaluate('window.fixtureValue = 41')
assert page.evaluate('fixtureValue + 1') == 42
measure = """() => {
    const rows = Array.from({length: 30000}, (_, i) => ({a:i, b:i+1, c:i+2, d:i+3}));
    const samples = [];
    for (let i=0; i<3; i++) {
        const start = performance.now();
        console.table(rows);
        samples.push(performance.now() - start);
    }
    return samples.sort((a,b) => a-b)[1];
}"""
result = {'default_ms': page.evaluate(measure)}
cdp = context.new_cdp_session(page)
cdp.send('Runtime.disable')
result['disabled_ms'] = page.evaluate(measure)
cdp.send('Runtime.enable')
result['enabled_ms'] = page.evaluate(measure)
cdp.send('Runtime.disable')
cdp.detach()
Path(artifact_path).write_text(json.dumps(result, indent=2) + '\\n')
page.goto(site + '/next')
result['after_navigation'] = page.evaluate('({title:document.title, previous:typeof fixtureValue})')
page.set_content('<p>setcontent works</p>')
result['set_content'] = page.locator('body').inner_text()
assert result['set_content'] == 'setcontent works', result
page.goto(site + '/frame')
frame = page.frames[1]
frame.wait_for_load_state()
result['child_context'] = frame.evaluate('document.body.innerText')
assert result['child_context'] == 'Child context'
assert page.get_by_text('Changed', exact=True).count() == 1
Path(artifact_path).write_text(json.dumps(result, indent=2) + '\\n')
print('CONSOLE_CONTEXTS_OK')
'''
    try:
        execution = run_skill(script, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                              extra_env={"BD_SESSION": sid}, timeout=90)
        evidence = {"returncode": execution.returncode, "stdout": execution.stdout,
                    "stderr": execution.stderr}
        if artifact.exists():
            evidence["observations"] = json.loads(artifact.read_text())
        artifact.write_text(json.dumps(evidence, indent=2) + "\n")
        assert execution.returncode == 0, evidence
        result = evidence["observations"]
        assert result["after_navigation"] == {"title": "Next context", "previous": "undefined"}
        assert result["default_ms"] <= result["disabled_ms"] * 3 + 12, result
        assert result["enabled_ms"] > result["disabled_ms"] + 5, result
        reset_result = run_skill("reset()", backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                                 extra_env={"BD_SESSION": sid}, timeout=30)
        assert reset_result.returncode == 0, reset_result.stderr
        rebound = run_skill("assert page.frames[1].evaluate('document.body.innerText') == 'Child context'\n"
                            "assert page.evaluate('typeof fixtureValue') == 'undefined'\nprint('REBOUND_OK')",
                            backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                            extra_env={"BD_SESSION": sid}, timeout=45)
        evidence["rebind"] = {"returncode": rebound.returncode, "stdout": rebound.stdout,
                              "stderr": rebound.stderr}
        artifact.write_text(json.dumps(evidence, indent=2) + "\n")
        assert rebound.returncode == 0, evidence
    finally:
        drop_ledger_session(bs_home("extension"), sid)
