"""S8 / D2 gate: ``attach_active()`` recovery when the user's focused tab is an
internal / extension page Chrome's debugger cannot attach to.

Session-1 failure mode: the agent's active tab was a ``chrome-extension://`` page,
``attach_active()`` raised, and the agent reacted by spawning FIVE brand-new
sessions instead of recovering. The fix: when the active tab is a non-attachable
internal URL, ``attach_active()`` auto-falls back to ``open_background()`` (a
fresh background tab) rather than bubbling the raise.

These are SEAM/MOCK tests — they patch ``sess.cdp.send`` and ``open_background``
to assert the *fallback path is taken*, with no live browser. The condition is
kept generic: any non-attachable internal URL (chrome://, chrome-extension://,
devtools://, …) triggers the fallback, not one hardcoded scheme.
"""
from __future__ import annotations

import pytest

from browser_skill.errors import CDPError


class _FakeCDP:
    """Minimal stand-in for ``sess.cdp`` that raises the daemon's
    'cannot attach to internal page' error on ``attachActiveTab``."""

    def __init__(self, attach_error_msg: str):
        self._attach_error_msg = attach_error_msg
        self.calls: list[str] = []
        self._sessions = {}
        self._events = {}

    def send(self, method, *args, **kwargs):
        self.calls.append(method)
        if method == "BrowserDaemon.attachActiveTab":
            raise CDPError(method=method, cdp_message=self._attach_error_msg)
        return {}


class _FakeSession:
    def __init__(self, cdp):
        self.cdp = cdp
        self.current_target_id = None
        self.backend_name = "extension"


@pytest.fixture
def patched(monkeypatch):
    """Bind a fake session into ``current_session()`` and stub
    ``open_background`` so we can observe the fallback without a browser."""
    from browser_skill.primitives import page as page_mod

    state = {"open_background_called_with": None}

    def fake_open_background(url, *, group=None):
        state["open_background_called_with"] = {"url": url, "group": group}
        return {"targetId": "bg-tab-1", "tabId": 7, "url": url,
                "title": "", "groupId": 1}

    monkeypatch.setattr(page_mod, "open_background", fake_open_background)

    def _install(attach_error_msg):
        cdp = _FakeCDP(attach_error_msg)
        sess = _FakeSession(cdp)
        monkeypatch.setattr(page_mod, "current_session", lambda: sess)
        return sess, cdp, state

    return _install


# A non-attachable internal active tab must trigger the open_background fallback,
# for EVERY internal scheme — not one hardcoded string. Parametrize the variants.
@pytest.mark.parametrize("err_msg", [
    "Cannot access a chrome-extension:// URL",
    "Cannot access a chrome:// URL",
    "Cannot attach to this target (devtools://devtools/...)",
    "Cannot access contents of url \"chrome-extension://abc/popup.html\"",
])
def test_attach_active_falls_back_to_open_background_on_internal_url(patched, err_msg):
    from browser_skill.primitives.page import attach_active

    sess, cdp, state = patched(err_msg)

    result = attach_active()

    # Fallback path was taken: open_background was called, no raise escaped.
    assert state["open_background_called_with"] is not None, (
        "expected attach_active() to fall back to open_background() on a "
        "non-attachable internal active tab, not re-raise"
    )
    # And the result is the open_background tab handle (so callers chain on it).
    assert result.get("targetId") == "bg-tab-1"
    # It really did try attach first (we recovered, not skipped).
    assert "BrowserDaemon.attachActiveTab" in cdp.calls


def test_attach_active_reraises_on_unrelated_cdp_error(patched):
    """A genuine failure (daemon down, wrong backend) must STILL raise — the
    fallback is scoped to the non-attachable-internal-URL condition, it doesn't
    swallow every CDPError."""
    from browser_skill.primitives.page import attach_active

    sess, cdp, state = patched(
        "attach_active() requires the extension backend; start the daemon "
        "with `browser-daemon serve --backend extension`"
    )

    with pytest.raises(CDPError):
        attach_active()

    assert state["open_background_called_with"] is None, (
        "unrelated CDPError must not silently trigger the open_background "
        "fallback"
    )
