"""L2 -- background tabs must render/advance as if the user is viewing them.

The extension attaches `chrome.debugger` to off-screen tabs (open_background
uses `active:false`). Without focus/visibility emulation Chrome reports such a
tab as hidden+blurred and throttles requestAnimationFrame, so pages that only
render or poll while visible stall. `keepTabRendered()` in the extension turns
on `Emulation.setFocusEmulationEnabled` + `Page.setWebLifecycleState:active`
right after attach; these tests pin the observable result of that.
"""
from __future__ import annotations

import json

from .helpers import run_skill


def _last_json(stdout: str) -> dict:
    line = next(
        ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{")
    )
    return json.loads(line)


def test_background_tab_reports_visible_and_focused(ext_ready):
    """A backgrounded tab we never switch to should still look focused+visible."""
    script = (
        "import json\n"
        "open_background('about:blank')\n"
        "wait_for_load()\n"
        "state = js('''return {\n"
        "  visibility: document.visibilityState,\n"
        "  hidden: document.hidden,\n"
        "  hasFocus: document.hasFocus(),\n"
        "}''')\n"
        "print(json.dumps(state))\n"
    )
    result = run_skill(script=script, backend="extension")
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    state = _last_json(result.stdout)
    assert state["visibility"] == "visible", state
    assert state["hidden"] is False, state
    assert state["hasFocus"] is True, state


def test_background_tab_raf_keeps_advancing(ext_ready):
    """Anti-overfit: prove rAF actually runs off-screen, not just that the
    visibility flags read nicely. A page increments a counter on every
    requestAnimationFrame frame and writes it to the DOM; a backgrounded tab
    with throttled rAF would barely move (hidden tabs get ~1fps or 0). We read
    the counter, wait, read again, and require real progress."""
    # rAF loop writing frame count into #n. data: URL keeps the test hermetic.
    html = (
        "<body><span id=n>0</span>"
        "<script>let n=0;"
        'function f(){n++;document.getElementById("n").textContent=n;'
        "requestAnimationFrame(f)}requestAnimationFrame(f)</script></body>"
    )
    script = (
        "import json, time\n"
        f"open_background('data:text/html,{html}')\n"
        "wait_for_load()\n"
        "first = int(js(\"document.getElementById('n').textContent\"))\n"
        "time.sleep(1.0)\n"
        "second = int(js(\"document.getElementById('n').textContent\"))\n"
        "print(json.dumps({'first': first, 'second': second}))\n"
    )
    result = run_skill(script=script, backend="extension", timeout=60)
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    payload = _last_json(result.stdout)
    # ~60fps for 1s would be ~60 frames; a throttled hidden tab yields ~0-2.
    # Require clearly-more-than-throttled progress to avoid a flaky threshold.
    advanced = payload["second"] - payload["first"]
    assert advanced >= 10, (
        f"rAF barely advanced off-screen (Δ={advanced}); focus emulation "
        f"likely not applied: {payload}"
    )
