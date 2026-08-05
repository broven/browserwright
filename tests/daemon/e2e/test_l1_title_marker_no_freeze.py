"""L1 -- the 👀 tab marker must reach a fixpoint, not spin the renderer.

The reported symptom was a *random* freeze: an agent command goes out and
nothing ever comes back (`relay send failed: TimeoutError()`). The cause was
not in the daemon at all -- it was in the extension's cosmetic tab marker.

`MARKER_INSTALL_SCRIPT` puts a `MutationObserver` on `document.head` whose
callback re-asserts the `"👀 "` prefix on `document.title`. The prefix carries a
trailing space, and the HTML spec's `document.title` getter *strips* trailing
ASCII whitespace -- so on a page whose title is empty the script writes `"👀 "`,
reads back `"👀"`, decides the title is still wrong, and writes again. Each
write mutates `<head>`, which re-fires the observer as a microtask, which
writes again: an unbounded microtask loop that pins the renderer's main thread.
A pinned renderer never answers `chrome.debugger.sendCommand`, so the extension
never sends its `{type:"response"}`, so the relay's `asyncio.wait_for(fut, 10)`
expires. Only *empty-titled* pages trip it -- `about:blank`, `data:` URLs, pages
caught before their title is set -- which is exactly what made it look random.

Two tests, deliberately at different levels:

1. `test_marker_script_reaches_a_fixpoint` runs the **real** script text pulled
   out of `chrome-extension/background.js` in a real renderer, with no daemon
   and no extension in the way. It is the sharp one: it fails in seconds, it
   names the fixpoint invariant directly, and it cannot pass by accident.
2. `test_empty_title_tab_answers_a_renderer_command` drives the whole stack the
   way the user did -- session tab on `about:blank`, then a renderer-scoped CDP
   command -- and gives it a wall-clock budget, in the spirit of
   `tests/daemon/test_hang_budget.py`: *a result or an error, either is fine,
   but within N seconds.*
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from .helpers import run_skill

#: The marker converges in one or two writes. Anything past this is the loop.
FIXPOINT_BUDGET_S = 10.0

#: A renderer-scoped eval on a freshly-attached blank tab. The relay's own
#: per-command deadline is 10s; the budget covers tab creation + attach + eval
#: with room to spare, while still being far below "hung".
RENDERER_BUDGET_S = 45.0

EXT_SOURCE_DIR = Path(__file__).resolve().parents[3] / "chrome-extension"

#: The marker section of background.js is pure JS -- constants and template
#: literals, no `chrome.*` -- so it can be evaluated verbatim in a page. Slicing
#: it by these anchors keeps the test pinned to the shipped source instead of a
#: copy that can silently drift. If either anchor moves, this test fails loudly
#: rather than testing a stale duplicate.
_REGION_START = "const TITLE_PREFIX"
_REGION_END = "const markedTabs"


def _marker_region() -> str:
    src = (EXT_SOURCE_DIR / "background.js").read_text(encoding="utf-8")
    try:
        start = src.index(_REGION_START)
        end = src.index(_REGION_END)
    except ValueError as e:  # pragma: no cover - structural drift
        raise AssertionError(
            f"could not slice the marker section out of background.js "
            f"({_REGION_START!r} .. {_REGION_END!r}); the anchors moved"
        ) from e
    assert start < end
    region = src[start:end]
    assert "MARKER_INSTALL_SCRIPT" in region
    # Comments in this region legitimately *discuss* chrome.debugger; only real
    # code would stop it evaluating standalone.
    code = "\n".join(ln for ln in region.splitlines()
                     if not ln.strip().startswith("//"))
    assert "chrome." not in code, (
        "the marker region gained a chrome.* reference; it can no longer be "
        "evaluated standalone in a page"
    )
    return region


async def _fixpoint_probe(browser_binary: Path) -> dict:
    """Install the real marker script on pages with hostile titles.

    Returns a report per scenario. Every step is wrapped in `asyncio.wait_for`
    because the failure being pinned is a *wedged renderer*: without a deadline
    `page.evaluate` simply never returns and the test hangs instead of failing.

    Driven with Chrome for Testing (the binary this suite already requires)
    rather than Playwright's own bundled build, so the test needs no extra
    download and exercises the same engine the rest of the e2e suite does. No
    extension and no daemon are involved — only the script text.
    """
    from playwright.async_api import async_playwright

    report: dict[str, dict] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=str(browser_binary), headless=True)
        try:
            page = await browser.new_page()
            await page.goto("about:blank")
            scripts = await page.evaluate(
                "(region) => new Function(region + '\\n"
                "return {install: MARKER_INSTALL_SCRIPT,"
                " remove: MARKER_REMOVE_SCRIPT};')()",
                _marker_region(),
            )

            scenarios = [
                # (label, page title before the marker is installed, expected
                #  title the page itself should still observe afterwards)
                ("empty", "", ""),
                ("normal", "Foo", "Foo"),
            ]
            for label, initial, expected_visible in scenarios:
                page = await browser.new_page()
                await page.goto("about:blank")
                if initial:
                    await asyncio.wait_for(
                        page.evaluate("t => { document.title = t; }", initial),
                        FIXPOINT_BUDGET_S,
                    )
                # THE step that hangs today when `initial` is empty.
                await asyncio.wait_for(
                    page.evaluate(scripts["install"]), FIXPOINT_BUDGET_S)
                # The renderer must still be answering at all.
                alive = await asyncio.wait_for(
                    page.evaluate("1 + 1"), FIXPOINT_BUDGET_S)
                # Let any pending observer callbacks drain, then confirm the
                # title has actually settled rather than merely being slow.
                await asyncio.sleep(0.25)
                first = await asyncio.wait_for(
                    page.evaluate(
                        "document.querySelector('title')"
                        " ? document.querySelector('title').textContent : null"),
                    FIXPOINT_BUDGET_S)
                await asyncio.sleep(0.25)
                second = await asyncio.wait_for(
                    page.evaluate(
                        "document.querySelector('title')"
                        " ? document.querySelector('title').textContent : null"),
                    FIXPOINT_BUDGET_S)
                visible = await asyncio.wait_for(
                    page.evaluate("document.title"), FIXPOINT_BUDGET_S)
                # The page setting its own empty title must not restart the loop.
                await asyncio.wait_for(
                    page.evaluate("document.title = ''"), FIXPOINT_BUDGET_S)
                await asyncio.sleep(0.25)
                after_self_clear = await asyncio.wait_for(
                    page.evaluate("1 + 1"), FIXPOINT_BUDGET_S)
                report[label] = {
                    "alive": alive,
                    "raw_first": first,
                    "raw_second": second,
                    "visible": visible,
                    "expected_visible": expected_visible,
                    "after_self_clear": after_self_clear,
                }
                await page.close()
        finally:
            await browser.close()
    return report


def test_marker_script_reaches_a_fixpoint(cft_binary):
    """The marker must be a fixpoint under the DOM's own title normalization.

    Write the marked title, read it back through the native getter, and the two
    must agree -- otherwise the observer rewrites forever. Asserted on the real
    script text from `chrome-extension/background.js`, in a real renderer.
    """
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("playwright not installed")

    try:
        report = asyncio.run(_fixpoint_probe(cft_binary))
    except asyncio.TimeoutError as e:  # pragma: no cover - this IS the bug
        raise AssertionError(
            "the renderer stopped answering after the tab marker was "
            "installed — this is the title-rewrite loop, i.e. the freeze"
        ) from e

    for label, r in report.items():
        assert r["alive"] == 2, f"[{label}] renderer not responsive: {r}"
        assert r["after_self_clear"] == 2, (
            f"[{label}] renderer wedged after the page set an empty title "
            f"through the marker's own setter: {r}"
        )
        assert r["raw_first"] == r["raw_second"], (
            f"[{label}] the raw <title> is still changing 250ms apart "
            f"({r['raw_first']!r} -> {r['raw_second']!r}) — the marker never "
            f"reached a fixpoint"
        )
        assert r["raw_first"] is not None and "\U0001F440" in r["raw_first"], (
            f"[{label}] the marker is gone; the tab strip no longer shows the "
            f"agent is driving this tab: {r}"
        )
        assert r["raw_first"] == r["raw_first"].strip(), (
            f"[{label}] the marked title {r['raw_first']!r} carries padding "
            f"whitespace the DOM getter will strip back off — that asymmetry "
            f"is precisely what loops"
        )
        assert r["visible"] == r["expected_visible"], (
            f"[{label}] the page's own document.title should read as if "
            f"unmarked: {r}"
        )

    # The marker still reads the way a user expects on a page with a title.
    assert report["normal"]["raw_first"] == "\U0001F440 Foo", report["normal"]


def _last_json(stdout: str) -> dict:
    line = next(
        ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{")
    )
    return json.loads(line)


def test_empty_title_tab_answers_a_renderer_command(ext_ready, e2e_daemon):
    """End-to-end shape of the user's report: attach a blank tab, then ask the
    renderer something. It must answer, and within a budget.

    `about:blank` is the trigger because its title is empty. Before the fix this
    fails as `relay send failed: TimeoutError()` roughly 10s in -- the renderer
    is spinning, so `chrome.debugger.sendCommand` never resolves.
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
        "print(json.dumps({'value': value, 'eval_s': time.monotonic() - t0}))\n"
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
    assert payload["value"] == 2, payload
    assert elapsed < RENDERER_BUDGET_S, (
        f"blank-tab attach + one renderer eval took {elapsed:.1f}s "
        f"(budget {RENDERER_BUDGET_S}s) — this is the shape of a hang, not a "
        f"slow test"
    )
