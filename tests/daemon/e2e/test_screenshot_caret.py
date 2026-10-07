"""Screenshot defaults preserve page caret styling, observed by page code.

Repeat with ``uv run pytest tests/daemon/e2e/test_screenshot_caret.py -v``.
The JSON artifact records shared-DOM mutations and the PNG/JPEG files verify
the native screenshot return value, path handling, and clipping behavior.
"""
from __future__ import annotations

import json
import textwrap
import uuid

import pytest

from .helpers import bs_home, drop_ledger_session, run_skill, seed_ledger_session


@pytest.fixture
def e2e_chrome(extension_only_chrome):
    return extension_only_chrome


@pytest.mark.parametrize("extension_only_chrome", ["headless"], indirect=True)
def test_screenshot_defaults_preserve_caret_and_explicit_options(
    e2e_daemon, e2e_chrome, ext_ready, e2e_artifacts_dir,
):
    pytest.importorskip("playwright.sync_api")
    assert not (e2e_chrome.profile_path / "DevToolsActivePort").exists()
    sid = f"e2e-caret-{uuid.uuid4().hex}"
    seed_ledger_session(bs_home("extension"), sid, backend="extension", name="caret")
    artifact = e2e_artifacts_dir / "screenshot-caret.json"
    artifact.unlink(missing_ok=True)
    script = f"artifact_dir = {str(e2e_artifacts_dir)!r}\n" + textwrap.dedent('''
        import json
        import struct
        from pathlib import Path
        page.set_content("""
            <style>body {margin:20px} input,textarea,[contenteditable] {display:block; margin:12px}</style>
            <input id="field" value="Caret fixture" style="caret-color:rgb(123, 45, 67)">
            <textarea style="caret-color:rgb(4, 56, 78)">Textarea</textarea>
            <div contenteditable style="caret-color:rgb(9, 87, 65)">Editable</div>
            <div id="shadow"></div>
            <script>
            const root = document.querySelector('#shadow').attachShadow({mode:'open'});
            root.innerHTML = '<input value="Shadow input" style="caret-color:rgb(10, 20, 30)">';
            window.styleMutations = [];
            for (const target of [document.documentElement, root]) {
              new MutationObserver(records => {
                for (const record of records) {
                  styleMutations.push({tag:record.target.tagName,
                    before:record.oldValue, after:record.target.getAttribute('style')});
                }
              }).observe(target, {attributes:true, subtree:true,
                                  attributeFilter:['style'], attributeOldValue:true});
            }
            window.readCaretStyles = () => [...document.querySelectorAll('input,textarea,[contenteditable]'),
                                            ...root.querySelectorAll('input')]
              .map(el => ({inline:el.getAttribute('style'), computed:getComputedStyle(el).caretColor}));
            </script>
        """)
        initial = page.evaluate('readCaretStyles()')
        result = {'initial': initial, 'cases': {}}
        def capture(name, take):
            page.evaluate('styleMutations.splice(0)')
            data = take()
            page.wait_for_timeout(50)
            result['cases'][name] = {
                'styles': page.evaluate('readCaretStyles()'),
                'mutations': page.evaluate('styleMutations.splice(0)'),
                'bytes': len(data), 'png': data.startswith(b'\\x89PNG\\r\\n\\x1a\\n'),
            }
            return data
        directory = Path(artifact_dir)
        page_path = directory / 'caret-page.png'
        data = capture('page_default', lambda: page.screenshot(path=page_path))
        result['page_file_equals_return'] = page_path.read_bytes() == data
        capture('page_none', lambda: page.screenshot(caret=None))
        capture('locator_default', lambda: page.locator('#field').screenshot())
        handle = page.locator('#field').element_handle()
        capture('handle_default', lambda: handle.screenshot())
        handle.dispose()
        clip = capture('page_clip_initial', lambda: page.screenshot(
            caret='initial', clip={'x':0, 'y':0, 'width':160, 'height':100}, scale='css'))
        result['clip_dimensions'] = list(struct.unpack('>II', clip[16:24]))
        jpeg_path = directory / 'caret-page.jpg'
        jpeg = capture('page_jpeg_initial', lambda: page.screenshot(
            path=jpeg_path, caret='initial', quality=65))
        result['jpeg_magic'] = jpeg.startswith(b'\\xff\\xd8\\xff')
        result['jpeg_file_equals_return'] = jpeg_path.read_bytes() == jpeg
        capture('page_explicit_hide', lambda: page.screenshot(caret='hide'))
        capture('locator_explicit_hide', lambda: page.locator('#field').screenshot(caret='hide'))
        (directory / 'screenshot-caret.json').write_text(json.dumps(result, indent=2) + '\\n')
        print('SCREENSHOT_CARET_OK')
    ''')
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
        for name in ("page_default", "page_none", "locator_default", "handle_default",
                     "page_clip_initial", "page_jpeg_initial"):
            case = result["cases"][name]
            assert case["mutations"] == [], (name, case)
            assert case["styles"] == result["initial"], (name, case)
            assert case["bytes"] > 100, (name, case)
            if name != "page_jpeg_initial":
                assert case["png"] is True, (name, case)
        for name in ("page_explicit_hide", "locator_explicit_hide"):
            case = result["cases"][name]
            assert any("transparent" in (mutation["before"] or "")
                       for mutation in case["mutations"]), (name, case)
            # Native CSSOM restoration normalizes inline declaration spacing.
            # Its explicit hiding must restore the original computed colors.
            assert [style['computed'] for style in case['styles']] == [
                style['computed'] for style in result['initial']], (name, case)
        assert result["page_file_equals_return"] is True, result
        assert result["jpeg_file_equals_return"] is True, result
        assert result["jpeg_magic"] is True, result
        assert result["clip_dimensions"] == [160, 100], result
    finally:
        drop_ledger_session(bs_home("extension"), sid)
