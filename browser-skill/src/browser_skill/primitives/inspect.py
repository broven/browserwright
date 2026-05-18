"""Page inspection: page_info / capture_screenshot / raw cdp."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Any, Optional

from ..session import current_session


def cdp(method: str, session_id: Optional[str] = None, **params) -> dict:
    """Pass-through to the underlying CDP transport."""
    sess = current_session()
    if session_id is None and sess.current_target_id:
        session_id = sess.cdp.attach(sess.current_target_id)
    return sess.cdp.send(method, session=session_id, **params)


def page_info() -> dict:
    """Snapshot of the current page state. Mirrors browser-harness shape."""
    from .interact import js  # avoid import cycle

    return js("""
        return {
          url: location.href,
          title: document.title,
          w: window.innerWidth,
          h: window.innerHeight,
          sx: window.scrollX,
          sy: window.scrollY,
          pw: document.documentElement.scrollWidth,
          ph: document.documentElement.scrollHeight,
          ready: document.readyState
        }
    """)


def capture_screenshot(path: Optional[str] = None, *, full: bool = False,
                       max_dim: Optional[int] = None) -> str:
    """Capture a PNG screenshot. Writes to ``path`` (or /tmp/screenshot-N.png)
    and returns the absolute path. Set ``full=True`` for a full-page capture."""
    sess = current_session()
    sid = sess.cdp.attach(sess.current_target_id) if sess.current_target_id else None
    if sid is None:
        from .page import current_page
        current_page()
        sid = sess.cdp.attach(sess.current_target_id)
    params: dict[str, Any] = {"format": "png"}
    if full:
        params["captureBeyondViewport"] = True
    res = sess.cdp.send("Page.captureScreenshot", session=sid, **params)
    raw = base64.b64decode(res["data"])
    if max_dim:
        raw = _downscale_png(raw, max_dim=max_dim)
    if not path:
        # Pick a /tmp file that doesn't collide if the agent runs many shots.
        i = 0
        while True:
            cand = Path("/tmp") / f"browser-skill-shot-{os.getpid()}-{i}.png"
            if not cand.exists():
                path = str(cand)
                break
            i += 1
    Path(path).write_bytes(raw)
    return str(Path(path).resolve())


def _downscale_png(data: bytes, *, max_dim: int) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return data
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale >= 1.0:
        return data
    new = im.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    new.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
