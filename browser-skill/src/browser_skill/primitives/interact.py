"""Compositor-level input + JS evaluation primitives.

v0.5.1 (F-4 catch-up) — primitives ported from ``browser-harness``:
``type_text``, ``press_key``, ``scroll``, ``fill_input``, ``dispatch_key``,
``upload_file``, ``wait_for_element``, ``wait_for_network_idle``,
``drain_events``. Same compositor-vs-DOM trade-off semantics; CDP transport
goes through ``current_session().cdp.send(method, session=sid, ...)``.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Iterable, Optional, Union

from ..errors import CDPError, ElementNotFound
from ..session import current_session


def _attached_session() -> str:
    sess = current_session()
    if not sess.current_target_id:
        # Extension backend: do NOT silently steal the user's focused tab
        # (current_page() would call attach_active() and grab it). Raise
        # with named next steps; open_background listed first (default).
        if sess.backend_name == "extension":
            from ..errors import NeedsUserConfirm
            raise NeedsUserConfirm(
                what="no tab attached on extension backend",
                proposal=(
                    "call `open_background(url, group='Agent')` to spawn a "
                    "fresh background tab (does not steal user focus), "
                    "OR `attach_active()` if the task is explicitly "
                    "'drive the user's current tab'. Then re-run."
                ),
            )
        # rdp/env: safe to auto-fallback — isolated Chrome, no user collision.
        from .page import current_page
        current_page()
    return sess.cdp.attach(sess.current_target_id)


def click_at_xy(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    """Compositor-level click — passes through iframes / shadow / cross-origin."""
    sid = _attached_session()
    sess = current_session()
    for _ in range(int(clicks)):
        sess.cdp.send(
            "Input.dispatchMouseEvent", session=sid,
            type="mousePressed", x=float(x), y=float(y),
            button=button, clickCount=1, buttons=1,
        )
        sess.cdp.send(
            "Input.dispatchMouseEvent", session=sid,
            type="mouseReleased", x=float(x), y=float(y),
            button=button, clickCount=1, buttons=0,
        )
        time.sleep(0.05)
    return {"x": x, "y": y, "button": button, "clicks": clicks}


def _has_top_level_return(src: str) -> bool:
    """``True`` iff ``src`` contains a ``return`` keyword at top level.

    Top level means not nested inside any ``()``, ``[]``, ``{}``, string,
    template literal, line/block comment, or regex literal. Used by
    ``js()`` to decide whether to auto-wrap the expression in an IIFE so
    the caller can write ``js("return foo.bar")`` ergonomically without
    misclassifying already-IIFE expressions like
    ``js("(()=>{return arr.map(...)})()")`` (whose ``return`` is nested
    inside parens and must NOT trigger a re-wrap — that was the
    silent-None bug pre-v0.5.5).

    Template-literal interpolations (``${...}``) are treated as opaque:
    we don't scan inside them. Returns *inside* a template's ``${}``
    won't be detected as top-level, which is fine — that pattern is
    vanishingly rare and the user can pass ``raw=True`` if needed.
    """
    n = len(src)
    i = 0
    depth = 0
    in_str: Optional[str] = None  # quote char, or None
    in_line_comment = False
    in_block_comment = False
    # Tracks whether the previous non-space token could plausibly be
    # followed by a regex literal (vs. a division). Crude but enough
    # to skip /.../ regex bodies without false-positives on a/b/c.
    prev_significant = ""

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
                prev_significant = c
                i += 1
                continue
            # Template-literal interpolation: ${ ... }. Skip to the
            # matching '}' — we don't try to scan inside.
            if in_str == "`" and c == "$" and nxt == "{":
                inner_depth = 1
                j = i + 2
                while j < n and inner_depth > 0:
                    if src[j] == "{":
                        inner_depth += 1
                    elif src[j] == "}":
                        inner_depth -= 1
                    j += 1
                i = j
                continue
            i += 1
            continue

        # Comments
        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        # Regex literal: only if the previous significant char allows
        # a regex (not after an identifier, ')', ']', or numeric literal).
        if c == "/" and prev_significant not in (")", "]", "_", "$"
                                                 ) and not (prev_significant.isalnum()):
            # Skip /.../flags
            j = i + 1
            while j < n and src[j] != "/":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "\n":
                    # Newline before closing '/': not a regex after all.
                    break
                j += 1
            else:
                # Hit end-of-string without closing /; bail out of regex.
                j = i
            if j < n and src[j] == "/":
                # Consume trailing flags
                j += 1
                while j < n and src[j].isalpha():
                    j += 1
                i = j
                prev_significant = "/"
                continue

        if c in ('"', "'", "`"):
            in_str = c
            i += 1
            continue
        if c in "({[":
            depth += 1
            prev_significant = c
            i += 1
            continue
        if c in ")}]":
            depth -= 1
            prev_significant = c
            i += 1
            continue
        # Top-level "return" keyword. Reject ``foo.return`` (member access:
        # reserved words are legal property names in JS) by treating ``.``
        # as a keyword-blocker. Same for optional-chain ``?.return``.
        if (depth == 0 and c == "r" and src[i:i + 6] == "return"):
            before_ok = (i == 0) or not (src[i - 1].isalnum()
                                         or src[i - 1] in ("_", "$", "."))
            after_ok = (i + 6 >= n) or not (src[i + 6].isalnum()
                                            or src[i + 6] in ("_", "$"))
            if before_ok and after_ok:
                return True
        if not c.isspace():
            prev_significant = c
        i += 1
    return False


def js(expression: str, target_id: Optional[str] = None, *,
       raw: bool = False) -> Any:
    """Evaluate JS in the page via ``Runtime.evaluate``.

    If ``expression`` contains a *top-level* ``return`` keyword it's
    wrapped in an IIFE so the caller can write ``js("return foo.bar")``
    ergonomically. The scanner skips strings, template literals,
    comments, and any ``return`` nested inside ``()/[]/{}`` — so
    already-IIFE expressions like ``js("(()=>{ return arr.map(...) })()")``
    are NOT re-wrapped (pre-v0.5.5 they were, which silently produced
    ``None`` because the outer wrapper had no return).

    Pass ``raw=True`` to skip auto-wrap entirely (escape hatch for
    expressions where the scanner misfires).

    ``target_id`` lets you target a specific iframe / popup via
    ``iframe_target(url)``.

    Returns the deserialized result, or ``None`` when JS returned
    ``undefined``. Raises ``CDPError`` when the result is
    non-serializable (DOM nodes, Map/Set, circular refs, functions) —
    wrap the relevant fields with ``JSON.stringify()`` or return
    primitive properties instead. Previously such results silently
    became ``None``, which was the second half of the v0.5.4
    silent-None bug.
    """
    sess = current_session()
    sid = sess.cdp.attach(target_id) if target_id else _attached_session()
    code = expression
    if not raw and _has_top_level_return(expression):
        code = f"(function(){{ {expression} }})()"
    try:
        res = sess.cdp.send(
            "Runtime.evaluate", session=sid,
            expression=code, returnByValue=True, awaitPromise=True,
        )
    except CDPError as e:
        # Surface JS errors with their actual text — agents debug from these.
        raise CDPError(method="Runtime.evaluate",
                       params={"expression": expression},
                       cdp_message=e.cdp_message) from e
    if "exceptionDetails" in res:
        det = res["exceptionDetails"]
        text = det.get("exception", {}).get("description") or det.get("text", "JS exception")
        raise CDPError(method="Runtime.evaluate",
                       params={"expression": expression}, cdp_message=text)
    result = res.get("result", {})
    if "value" in result:
        return result["value"]
    # No ``value`` field. CDP omits it for two distinct cases — distinguish:
    #   * ``undefined`` — legitimate "function returned no value", map to None
    #   * everything else (object/function/symbol with no value) —
    #     non-serializable; the silent-None trap pre-v0.5.5.
    ty = result.get("type")
    if ty == "undefined":
        return None
    desc = (result.get("description") or result.get("subtype")
            or ty or "<unknown>")
    raise CDPError(
        method="Runtime.evaluate",
        params={"expression": expression},
        cdp_message=(
            f"non-serializable JS result (type={ty!r}, desc={desc!r}). "
            f"Runtime.evaluate with returnByValue cannot serialize DOM "
            f"nodes, Map/Set, functions, or circular refs. Wrap the "
            f"fields you need with JSON.stringify() or return primitive "
            f"properties (e.g. ``el.id``, ``el.textContent``) instead."
        ),
    )


# ---- keyboard ----------------------------------------------------------


# (key → (windowsVirtualKeyCode, code, text)) — covers the special keys
# whose .keyCode / .code listeners DOM frameworks check. Single-char keys
# fall through to ``ord(key[0])`` + ``key`` for code.
_KEYS: dict[str, tuple[int, str, str]] = {
    "Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", "\t"),
    "Backspace": (8, "Backspace", ""), "Escape": (27, "Escape", ""),
    "Delete": (46, "Delete", ""), " ": (32, "Space", " "),
    "ArrowLeft": (37, "ArrowLeft", ""), "ArrowUp": (38, "ArrowUp", ""),
    "ArrowRight": (39, "ArrowRight", ""), "ArrowDown": (40, "ArrowDown", ""),
    "Home": (36, "Home", ""), "End": (35, "End", ""),
    "PageUp": (33, "PageUp", ""), "PageDown": (34, "PageDown", ""),
}


def type_text(text: str) -> None:
    """Insert ``text`` at the focused element via ``Input.insertText``.

    Bypasses framework event listeners — fast and good for plain inputs.
    Use ``fill_input`` when the site is a framework-controlled input
    (React controlled, Vue v-model, etc.) that needs synthetic
    ``input``/``change`` events to update its bound state.
    """
    sess = current_session()
    sid = _attached_session()
    sess.cdp.send("Input.insertText", session=sid, text=text)


def press_key(key: str, modifiers: int = 0) -> None:
    """Dispatch a real keyDown / (optional char) / keyUp sequence.

    ``modifiers`` bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift. Special
    keys (Enter/Tab/Arrow*/Backspace/etc.) carry their virtual keycodes
    so listeners checking ``e.keyCode`` / ``e.key`` all fire correctly.
    """
    vk, code, text = _KEYS.get(
        key,
        (ord(key[0]) if len(key) == 1 else 0,
         key,
         key if len(key) == 1 else ""),
    )
    base = {
        "key": key, "code": code, "modifiers": modifiers,
        "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
    }
    sess = current_session()
    sid = _attached_session()
    if text:
        sess.cdp.send("Input.dispatchKeyEvent", session=sid,
                      type="keyDown", text=text, **base)
        if len(text) == 1:
            sess.cdp.send("Input.dispatchKeyEvent", session=sid,
                          type="char", text=text, **base)
    else:
        sess.cdp.send("Input.dispatchKeyEvent", session=sid,
                      type="keyDown", **base)
    sess.cdp.send("Input.dispatchKeyEvent", session=sid,
                  type="keyUp", **base)


def scroll(x: float, y: float, dy: float = -300, dx: float = 0) -> None:
    """Wheel scroll at ``(x, y)``. ``dy`` negative = scroll up (consistent
    with browser-harness convention)."""
    sess = current_session()
    sid = _attached_session()
    sess.cdp.send(
        "Input.dispatchMouseEvent", session=sid,
        type="mouseWheel", x=float(x), y=float(y),
        deltaX=float(dx), deltaY=float(dy),
    )


def fill_input(selector: str, text: str, *, clear_first: bool = True,
               timeout: float = 0.0) -> None:
    """Focus the element matched by ``selector``, optionally clear it,
    type ``text`` via real key events, then dispatch synthetic
    ``input``/``change`` events so framework-bound state updates.

    Raises ``ElementNotFound`` if the selector doesn't match. Pass
    ``timeout > 0`` to wait for late-rendered elements (e.g. after a
    route change).
    """
    if timeout > 0:
        if not wait_for_element(selector, timeout=timeout):
            raise ElementNotFound(selector=selector, timeout=timeout)
    focused = js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
        f"if(!e)return false;e.focus();return true;}})()"
    )
    if not focused:
        raise ElementNotFound(selector=selector)
    if clear_first:
        # Select-all via the platform shortcut (Cmd on macOS, Ctrl
        # elsewhere). Done as raw key events because press_key() emits
        # a 'char' for single-char keys, which would type a literal "a"
        # under modifiers instead of triggering select-all.
        mods = 4 if sys.platform == "darwin" else 2
        select_all = {
            "key": "a", "code": "KeyA", "modifiers": mods,
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
        }
        sess = current_session()
        sid = _attached_session()
        sess.cdp.send("Input.dispatchKeyEvent", session=sid,
                      type="rawKeyDown", **select_all)
        sess.cdp.send("Input.dispatchKeyEvent", session=sid,
                      type="keyUp", **select_all)
        press_key("Backspace")
    for ch in text:
        press_key(ch)
    js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
        f"if(!e)return;"
        f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
        f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}})();"
    )


# ---- DOM-level dispatch -----------------------------------------------


_DOM_KC = {
    "Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, " ": 32,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
}


def dispatch_key(selector: str, key: str = "Enter",
                 event: str = "keypress") -> None:
    """Dispatch a synthetic DOM ``KeyboardEvent`` on the matched element.

    Use when a site's listener reacts to DOM events on a specific element
    more reliably than to raw CDP input events fired at compositor level
    (some React/Vue forms behave this way).
    """
    kc = _DOM_KC.get(key, ord(key) if len(key) == 1 else 0)
    js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
        f"if(e){{e.focus();"
        f"e.dispatchEvent(new KeyboardEvent({json.dumps(event)},"
        f"{{key:{json.dumps(key)},code:{json.dumps(key)},"
        f"keyCode:{kc},which:{kc},bubbles:true}}));}}}})()"
    )


# ---- upload -----------------------------------------------------------


def upload_file(selector: str, path: Union[str, Iterable[str]]) -> None:
    """Set files on a ``<input type=file>`` via ``DOM.setFileInputFiles``.

    ``path`` must be an absolute filesystem path (or a list of them for
    multi-file inputs). Raises ``ElementNotFound`` if the selector
    doesn't match.
    """
    sess = current_session()
    sid = _attached_session()
    doc = sess.cdp.send("DOM.getDocument", session=sid, depth=-1)
    res = sess.cdp.send("DOM.querySelector", session=sid,
                        nodeId=doc["root"]["nodeId"], selector=selector)
    nid = res.get("nodeId")
    if not nid:
        raise ElementNotFound(selector=selector)
    files = [path] if isinstance(path, str) else list(path)
    sess.cdp.send("DOM.setFileInputFiles", session=sid,
                  files=files, nodeId=nid)


# ---- waiting + events -------------------------------------------------


def wait_for_element(selector: str, *, timeout: float = 10.0,
                     visible: bool = False) -> bool:
    """Poll until ``document.querySelector(selector)`` matches, or
    timeout. ``visible=True`` additionally requires the element to be
    rendered (uses ``checkVisibility()`` when available, falls back to
    CSS inspection on older Chrome).

    ``wait_for_load`` is not enough for SPAs — ``readyState`` flips to
    ``complete`` before the framework renders. Use this after actions
    that trigger async rendering (route changes, data fetches).
    """
    if visible:
        check = (
            f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
            f"if(!e)return false;"
            f"if(typeof e.checkVisibility==='function')"
            f"return e.checkVisibility({{checkOpacity:true,checkVisibilityCSS:true}});"
            f"const s=getComputedStyle(e);"
            f"return s.display!=='none'&&s.visibility!=='hidden'"
            f"&&s.opacity!=='0'}})()"
        )
    else:
        check = f"!!document.querySelector({json.dumps(selector)})"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if js(check):
                return True
        except CDPError:
            pass
        time.sleep(0.3)
    return False


def drain_events(session: Optional[str] = None) -> list[dict]:
    """Pop accumulated CDP events from the daemon's per-session buffer.

    Returns the list (possibly empty) and clears the buffer. Used by
    ``wait_for_network_idle`` and similar event-watching helpers.
    ``session=None`` drains the currently attached session.
    """
    sess = current_session()
    sid = session or sess.cdp.attach(sess.current_target_id) if sess.current_target_id else None
    return sess.cdp.drain_events(session=sid)


def wait_for_network_idle(*, timeout: float = 10.0,
                          idle_ms: int = 500) -> bool:
    """Wait until no in-flight requests AND no Network.* event has
    arrived for ``idle_ms`` milliseconds.

    Useful after form submits, SPA route transitions, and any action
    that triggers XHR/fetch without a visible DOM change. Filters
    events to the active session so a background tab's polling/SSE
    can't poison the idle window.
    """
    sess = current_session()
    sid = (sess.cdp.attach(sess.current_target_id)
           if sess.current_target_id else None)
    deadline = time.monotonic() + timeout
    last_activity = time.monotonic()
    inflight: set[str] = set()
    while time.monotonic() < deadline:
        for e in sess.cdp.drain_events(session=sid):
            method = e.get("method") or ""
            params = e.get("params") or {}
            if method == "Network.requestWillBeSent":
                rid = params.get("requestId")
                if rid:
                    inflight.add(rid)
                last_activity = time.monotonic()
            elif method in ("Network.loadingFinished",
                            "Network.loadingFailed"):
                rid = params.get("requestId")
                if rid:
                    inflight.discard(rid)
                last_activity = time.monotonic()
            elif method.startswith("Network."):
                last_activity = time.monotonic()
        if not inflight and (time.monotonic() - last_activity) * 1000 >= idle_ms:
            return True
        time.sleep(0.1)
    return False
