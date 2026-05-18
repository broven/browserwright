"""Compositor-level input + JS evaluation primitives.

v0.5.1 (F-4 catch-up) — primitives ported from ``browser-harness``:
``type_text``, ``press_key``, ``scroll``, ``fill_input``, ``dispatch_key``,
``upload_file``, ``wait_for_element``, ``wait_for_network_idle``,
``drain_events``. Same compositor-vs-DOM trade-off semantics; CDP transport
goes through ``current_session().cdp.send(method, session=sid, ...)``.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Iterable, Optional, Union

from ..errors import CDPError, ElementNotFound
from ..session import current_session


def _attached_session() -> str:
    sess = current_session()
    if not sess.current_target_id:
        # Attempt to attach to *some* real page so the agent gets a clear
        # error instead of an opaque CDP "no session" message.
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


_HAS_RETURN = re.compile(r"\breturn\b")


def js(expression: str, target_id: Optional[str] = None) -> Any:
    """Evaluate JS in the page. If ``expression`` contains a ``return`` keyword
    it's wrapped in an IIFE so the caller doesn't have to. ``target_id`` lets
    you target a specific iframe / popup (use ``iframe_target(url)`` once
    that helper lands)."""
    sess = current_session()
    sid = sess.cdp.attach(target_id) if target_id else _attached_session()
    code = expression
    if _HAS_RETURN.search(expression):
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
    value = res.get("result", {}).get("value")
    return value


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
