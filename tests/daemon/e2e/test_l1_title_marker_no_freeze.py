"""Extension attachment must leave page title/accessors intact and responsive.

The former title marker rewrote the DOM and could spin an empty-title renderer.
The browser UI indicator now belongs to the extension, so test the actual
extension-backed path rather than executing the retired marker snippets.
"""
from __future__ import annotations

import json
import time

from .helpers import run_skill

RENDERER_BUDGET_S = 45.0


def _last_json(stdout: str) -> dict:
    line = next(
        ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{")
    )
    return json.loads(line)


def test_empty_title_tab_answers_a_renderer_command(ext_ready, e2e_daemon, tmp_path):
    """A real extension attach preserves native title behavior, including empty
    titles and titles that legitimately start with the former marker glyph.
    Save the observed page state so the renderer proof can be inspected.
    """
    script = (
        "import json, time\n"
        "from browserwright.session import current_session\n"
        "from browserwright.session_runtime import (\n"
        "    eval_js, open_session_tab, wait_for_ready,\n"
        ")\n"
        "sess = current_session()\n"
        "open_session_tab(sess, 'about:blank')\n"
        "wait_for_ready(sess)\n"
        "t0 = time.monotonic()\n"
        "value = eval_js(sess, '1 + 1')\n"
        "probe = \"(() => { const samples = ['', 'Example', '👀 Example'].map(title => { document.title = title; return {expected: title, title: document.title, raw: document.querySelector('title').textContent}; }); const descriptor = Object.getOwnPropertyDescriptor(Document.prototype, 'title'); return {samples, ownTitle: Object.hasOwn(document, 'title'), nativeGetter: descriptor.get.toString().includes('[native code]'), nativeSetter: descriptor.set.toString().includes('[native code]'), markerGlobal: Object.hasOwn(window, '__bdTitleMarker')}; })()\"\n"
        "integrity = eval_js(sess, probe)\n"
        "print(json.dumps({'value': value, 'eval_s': time.monotonic() - t0, 'integrity': integrity}))\n"
    )
    started = time.monotonic()
    result = run_skill(script=script, backend="extension", timeout=90,
                       runtime_dir=e2e_daemon.runtime_dir)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, (
        f"skill exited {result.returncode} after {elapsed:.1f}s; "
        f"stderr={result.stderr!r}"
    )
    payload = _last_json(result.stdout)
    (tmp_path / "extension-page-integrity.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    assert payload["value"] == 2, payload
    assert elapsed < RENDERER_BUDGET_S, payload
    integrity = payload["integrity"]
    assert not integrity["ownTitle"], integrity
    assert integrity["nativeGetter"] and integrity["nativeSetter"], integrity
    assert not integrity["markerGlobal"], integrity
    for sample in integrity["samples"]:
        assert sample["title"] == sample["raw"] == sample["expected"], sample
