"""S8 / B3 gate: set-of-mark annotated screenshots.

``capture_screenshot(annotate=True)`` overlays numbered ``[N]`` marks on the
page's interactive elements and returns a *legend* mapping each ``[N]`` to the
element's role/name and its center ``(x, y)`` — so the agent clicks via the
existing ``click_at_xy(x, y)`` with NO ref store. Coordinate-keyed, not ref-keyed.

The contract under test:
  - ``annotate=False`` (default) still returns a bare PNG path string
    (backward compatible).
  - ``annotate=True`` returns ``{"path": <png path>, "legend": [ {n, role, name,
    x, y}, ... ]}``.
  - The legend's entries correspond 1:1 to ``snapshot()``'s interactive nodes
    (same count), each ``[n]`` is sequential from 0, and each carries numeric
    ``(x, y)``.

Behaviour layer needs a Chromium binary (Playwright auto-discovers); it skips
cleanly when none is found. We reuse test_perception's live_session harness so
we exercise the real ``js()`` -> ``Runtime.evaluate`` round-trip, and inject a
controlled DOM with a known number of interactive elements (no real site /
selector / class strings — pure injection, so the assertion generalizes).
"""
from __future__ import annotations

import pytest

# Reuse the live Chromium harness + injected-DOM fixtures from the S1 gate.
from test_perception import injected, live_session  # noqa: F401


# A controlled DOM with a KNOWN set of interactive elements: 3 buttons, 1 link,
# 1 text input = 5 interactive nodes. No site/class strings — pure injection so
# the gate asserts the general "legend == interactive set" contract.
_INJECT_INTERACTIVE = r"""
return (function(){
  document.documentElement.innerHTML = '<head></head><body></body>';
  var b = document.body;
  b.style.margin = '0';
  function mk(tag, props, css){
    var e = document.createElement(tag);
    for (var k in props) { if (k === 'text') e.textContent = props[k];
                           else e.setAttribute(k, props[k]); }
    e.style.cssText = css;
    b.appendChild(e); return e;
  }
  mk('button', {text:'Alpha'},  'position:fixed;top:40px;left:30px;width:80px;height:30px');
  mk('button', {text:'Beta'},   'position:fixed;top:90px;left:30px;width:80px;height:30px');
  mk('button', {text:'Gamma'},  'position:fixed;top:140px;left:30px;width:80px;height:30px');
  mk('a',      {text:'Docs', href:'https://example.org/d'},
                                'position:fixed;top:190px;left:30px;width:80px;height:24px');
  mk('input',  {type:'text', placeholder:'q'},
                                'position:fixed;top:230px;left:30px;width:160px;height:24px');
  return true;
})();
"""


@pytest.fixture
def interactive_page(injected):  # noqa: F811 (injected fixture from test_perception)
    from browser_skill import js

    js(_INJECT_INTERACTIVE)
    return injected


def test_annotate_false_returns_bare_path(interactive_page):
    """Default behaviour is unchanged: a string path, no legend wrapper."""
    from browser_skill import capture_screenshot

    out = capture_screenshot()
    assert isinstance(out, str)
    assert out.endswith(".png")


def test_annotate_returns_legend_matching_snapshot(interactive_page):
    from browser_skill import capture_screenshot, snapshot

    snap = snapshot()
    interactive = snap["nodes"]
    assert interactive, "fixture should inject interactive nodes"

    out = capture_screenshot(annotate=True)

    # Shape: dict with a png path + a legend list.
    assert isinstance(out, dict), "annotate=True must return a dict, not a str"
    assert isinstance(out.get("path"), str) and out["path"].endswith(".png")
    legend = out.get("legend")
    assert isinstance(legend, list)

    # 1:1 with snapshot()'s interactive set.
    assert len(legend) == len(interactive), (
        f"legend has {len(legend)} marks but snapshot() found "
        f"{len(interactive)} interactive nodes — they must match"
    )

    # Each entry: sequential [n] from 0, numeric (x, y), a role, and a name key.
    for i, entry in enumerate(legend):
        assert entry["n"] == i, "marks must be sequential from 0"
        assert isinstance(entry["x"], (int, float))
        assert isinstance(entry["y"], (int, float))
        assert "role" in entry
        assert "name" in entry

    # Coordinates are keyed to the SAME centers snapshot() reports (the agent
    # feeds them straight into click_at_xy). Compare as sets of (x, y).
    legend_xy = sorted((e["x"], e["y"]) for e in legend)
    snap_xy = sorted((n["x"], n["y"]) for n in interactive)
    assert legend_xy == snap_xy, "legend centers must equal snapshot() centers"


def test_annotate_count_scales_with_interactive_count(injected):  # noqa: F811
    """Generalization guard: a DIFFERENT interactive count yields a matching
    legend count — the feature isn't pinned to one fixture size."""
    from browser_skill import capture_screenshot, js, snapshot

    # Only two interactive nodes this time.
    js(
        "document.documentElement.innerHTML='<head></head><body></body>';"
        "var b=document.body;b.style.margin='0';"
        "var x=document.createElement('button');x.textContent='One';"
        "x.style.cssText='position:fixed;top:20px;left:20px;width:60px;height:24px';"
        "b.appendChild(x);"
        "var y=document.createElement('button');y.textContent='Two';"
        "y.style.cssText='position:fixed;top:60px;left:20px;width:60px;height:24px';"
        "b.appendChild(y);return true;"
    )
    n = len(snapshot()["nodes"])
    out = capture_screenshot(annotate=True)
    assert len(out["legend"]) == n
