"""Tests for ``_has_top_level_return`` and the v0.5.5 ``js()`` fixes.

Pre-v0.5.5 ``js()`` used ``re.compile(r"\\breturn\\b")`` to detect when to
auto-wrap an expression in ``(function(){ ... })()``. The regex matched
``return`` *anywhere*, including inside nested arrow functions, so:

    js("(()=>{ return arr.map(x=>x.id) })()")

got wrapped to ``(function(){ (()=>{return arr.map(...)})() })()`` — the
outer wrapper had no return, the outer IIFE evaluated to ``undefined``,
``returnByValue`` returned a result with no ``value`` key, and ``js()``
silently returned ``None``. This file pins the new behavior.
"""
from __future__ import annotations

import pytest


# ---- pure scanner tests ---------------------------------------------------


def test_scanner_detects_bare_return():
    from browserwright.primitives.interact import _has_top_level_return
    assert _has_top_level_return("return foo")
    assert _has_top_level_return("const x = 1; return x")
    assert _has_top_level_return("  return  42 ;")


def test_scanner_ignores_return_inside_parens():
    """The pre-v0.5.5 silent-None case: returns nested in arrow IIFEs."""
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("(()=>{ return arr.map(x=>x.id) })()")
    assert not _has_top_level_return("(function(){ return 1 })()")


def test_scanner_ignores_return_inside_braces():
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("foo.map(function(x){ return x*2 })")


def test_scanner_ignores_return_inside_brackets():
    from browserwright.primitives.interact import _has_top_level_return
    # Pathological but valid: subscript with a comma-expression
    assert not _has_top_level_return("arr[(function(){return 0})()]")


def test_scanner_ignores_return_inside_string():
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("'return foo'")
    assert not _has_top_level_return('"return foo"')
    assert not _has_top_level_return("`return foo`")
    # Mixed: top-level return AFTER a string containing "return"
    assert _has_top_level_return("const s = 'return'; return s")


def test_scanner_ignores_return_inside_template_interpolation():
    """Template literal ${} expressions are treated as opaque — we don't
    scan inside them. The whole template literal is skipped as a string."""
    from browserwright.primitives.interact import _has_top_level_return
    # `${(()=>{return 1})()}` — return is inside ${} of template, opaque.
    assert not _has_top_level_return("`${(()=>{return 1})()}`")


def test_scanner_ignores_return_inside_line_comment():
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("// return foo")
    assert not _has_top_level_return("x = 1 // return bar")
    # Comment ends at newline; subsequent return at top level counts.
    assert _has_top_level_return("// not\nreturn 1")


def test_scanner_ignores_return_inside_block_comment():
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("/* return foo */")
    assert not _has_top_level_return("x /* return */ = 1")
    # Block comment ends; later return counts.
    assert _has_top_level_return("/* return */ return 2")


def test_scanner_ignores_escaped_quote_in_string():
    """Escape sequences inside strings must not prematurely close them."""
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("'it\\'s return'")
    assert not _has_top_level_return('"a \\"return\\" b"')


def test_scanner_rejects_returnish_identifiers():
    """``returns`` / ``_return`` / ``$return`` are not the keyword."""
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("returns")
    assert not _has_top_level_return("_return")
    assert not _has_top_level_return("$return")
    assert not _has_top_level_return("returnX = 1")
    assert not _has_top_level_return("foo.return")


def test_scanner_handles_empty_and_whitespace():
    from browserwright.primitives.interact import _has_top_level_return
    assert not _has_top_level_return("")
    assert not _has_top_level_return("   ")
    assert not _has_top_level_return("\n\t\n")


# ---- integration: js() with mocked CDP -----------------------------------


class _RecordingCDP:
    """Stub CDP that records the last Runtime.evaluate expression and
    returns a configurable Runtime.evaluate response. ``Target.attachToTarget``
    yields a stable sessionId; everything else returns ``{}``.
    """
    def __init__(self, eval_result):
        self._eval_result = eval_result
        self.last_expression = None
        self._sessions: dict = {}
        self._closed = False

    def send(self, method, **kwargs):
        if method == "Target.attachToTarget":
            return {"sessionId": "stub-sid"}
        if method == "Runtime.evaluate":
            self.last_expression = kwargs.get("expression")
            return self._eval_result
        return {}

    def attach(self, target_id):
        return "stub-sid"


def _install_stub(monkeypatch, eval_result):
    from browserwright.session import Session
    import browserwright.session as session_mod
    sess = Session(daemon=object())
    cdp = _RecordingCDP(eval_result)
    sess._cdp = cdp
    sess._backend_name_cache = "rdp"
    sess.current_target_id = "stub-target"
    monkeypatch.setattr(session_mod, "_singleton", sess)
    return cdp


def test_js_iife_not_rewrapped(monkeypatch):
    """The headline pre-v0.5.5 bug: IIFE wrapping the IIFE killed the
    return value. Now ``js()`` must pass the IIFE through unchanged."""
    cdp = _install_stub(monkeypatch, {"result": {"type": "object",
                                                 "value": [1, 2, 3]}})
    from browserwright.primitives.interact import js
    out = js("(()=>{ return [1,2,3] })()")
    assert out == [1, 2, 3]
    # Critical: the expression sent to Runtime.evaluate is the ORIGINAL,
    # NOT wrapped in another (function(){ ... })().
    assert cdp.last_expression == "(()=>{ return [1,2,3] })()"


def test_js_bare_return_still_wraps(monkeypatch):
    """Back-compat: existing ``js("return foo.bar")`` callers must keep
    working (the auto-wrap is the whole reason agents like this helper)."""
    cdp = _install_stub(monkeypatch, {"result": {"type": "number",
                                                 "value": 42}})
    from browserwright.primitives.interact import js
    out = js("return 6 * 7")
    assert out == 42
    # Must be wrapped — Runtime.evaluate can't evaluate ``return`` at
    # top level without a function context.
    assert cdp.last_expression == "(function(){ return 6 * 7 })()"


def test_js_raw_true_skips_wrap(monkeypatch):
    """``raw=True`` is the escape hatch for cases where the scanner
    misfires (e.g. a regex literal it can't reliably distinguish)."""
    cdp = _install_stub(monkeypatch, {"result": {"type": "string",
                                                 "value": "ok"}})
    from browserwright.primitives.interact import js
    js("return 1", raw=True)
    # Even though ``return`` is at top level, raw=True skips the wrap.
    assert cdp.last_expression == "return 1"


def test_js_undefined_returns_none(monkeypatch):
    """JS ``undefined`` is the legitimate "no return value" case and
    must map to Python ``None``, not raise. CDP omits ``value`` for
    undefined — distinguishing it from non-serializable is the point
    of the v0.5.5 result-shape handling."""
    _install_stub(monkeypatch, {"result": {"type": "undefined"}})
    from browserwright.primitives.interact import js
    assert js("void 0") is None


def test_js_non_serializable_raises(monkeypatch):
    """The OTHER silent-None case pre-v0.5.5: ``returnByValue`` returns
    no ``value`` field for DOM nodes / Map / Set / circular refs.
    ``js()`` used to silently return None; now it raises with text
    that names the cause and the fix."""
    _install_stub(monkeypatch, {
        "result": {"type": "object", "subtype": "node",
                   "className": "HTMLDivElement",
                   "description": "div#main"},
    })
    from browserwright.errors import CDPError
    from browserwright.primitives.interact import js
    with pytest.raises(CDPError) as exc:
        js("return document.querySelector('#main')")
    msg = str(exc.value)
    assert "non-serializable" in msg
    assert "JSON.stringify" in msg


def test_js_null_returns_none(monkeypatch):
    """JS ``null`` deserializes to Python None via ``value`` field
    (present, value=None). Distinct from ``undefined`` (no value key)."""
    _install_stub(monkeypatch, {"result": {"type": "object",
                                           "subtype": "null",
                                           "value": None}})
    from browserwright.primitives.interact import js
    assert js("return null") is None


def test_js_exception_passes_through(monkeypatch):
    """JS exceptions (TypeError, etc.) still raise CDPError carrying the
    error text — same behavior as pre-v0.5.5, must not regress."""
    _install_stub(monkeypatch, {
        "exceptionDetails": {
            "text": "Uncaught",
            "exception": {
                "description": "TypeError: foo is not a function",
            },
        },
        "result": {"type": "object", "subtype": "error"},
    })
    from browserwright.errors import CDPError
    from browserwright.primitives.interact import js
    with pytest.raises(CDPError) as exc:
        js("foo()")
    assert "TypeError" in str(exc.value)
