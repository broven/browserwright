from __future__ import annotations

import base64
import sys
import threading
import types
from collections import deque
from pathlib import Path

import pytest

from browserwright.errors import CDPError, ElementNotFound, NeedsUserConfirm


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lT6k"
    "9QAAAABJRU5ErkJggg=="
)


class _FakeCDP:
    _closed = False

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.attached: list[str] = []
        self._sessions: dict[str, str] = {}
        self._events: dict[str | None, deque] = {}

    def attach(self, target_id: str) -> str:
        self.attached.append(target_id)
        sid = self._sessions.setdefault(target_id, f"sid-{target_id}")
        self._events.setdefault(sid, deque(maxlen=8))
        return sid

    def attach_readonly(self, target_id: str) -> str:
        return f"readonly-{target_id}"

    def send(self, method: str, *, session: str | None = None, **params):
        self.calls.append((method, {"session": session, **params}))
        response = self.responses.get(method, {})
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(method=method, session=session, **params)
        return response

    def drain_events(self, *, session: str | None = None):
        buf = self._events.get(session)
        if not buf:
            return []
        out = list(buf)
        buf.clear()
        return out


class _StubDaemon:
    def resolve_ws_url(self):
        raise AssertionError("daemon should not be touched")

    def invalidate(self):
        pass


@pytest.fixture
def fake_session(fresh_modules):
    from browserwright.session import Session, with_session

    sess = Session(daemon=_StubDaemon())
    sess.current_target_id = "target-1"
    sess._cdp = _FakeCDP()  # type: ignore[attr-defined]
    with with_session(sess):
        yield sess, sess._cdp


def test_capture_screenshot_annotates_writes_and_always_clears(
    fake_session, monkeypatch, tmp_path
):
    from browserwright.primitives import inspect as inspect_mod

    _, fake = fake_session
    fake.responses["Page.captureScreenshot"] = {
        "data": base64.b64encode(_ONE_BY_ONE_PNG).decode()
    }
    cleared = []
    legend = [{"n": 0, "role": "button", "name": "Save", "x": 10, "y": 20}]
    monkeypatch.setattr(inspect_mod, "_draw_set_of_mark", lambda: (legend, "paint failed"))
    monkeypatch.setattr(inspect_mod, "_clear_set_of_mark", lambda: cleared.append(True))

    out = inspect_mod.capture_screenshot(
        str(tmp_path / "shot.png"), full=True, annotate=True
    )

    assert out == {
        "path": str(tmp_path / "shot.png"),
        "legend": legend,
        "mark_error": "paint failed",
    }
    assert Path(out["path"]).read_bytes() == _ONE_BY_ONE_PNG
    assert cleared == [True]
    assert fake.calls[-1] == (
        "Page.captureScreenshot",
        {"session": "sid-target-1", "format": "png", "captureBeyondViewport": True},
    )
