"""Issue #21 unit coverage: the current-target-changed hook + ledger hygiene.

The agent-facing repro (switch_tab + close old tab in one call) is an
executor/e2e behavior (see tests/daemon/e2e/test_l2_issue21_page_binding.py);
these tests pin the session_runtime mechanics the executor's rebind hook
depends on:

  - ``bind_target`` (the switch_tab equivalent) fires the target-changed hook
    and repoints ``runtime.current_target_id``;
  - ``close_session_tab`` of the CURRENT tab clears + persists the binding and
    fires the hook (close of a non-current tab does neither);
  - ``register_recovered`` (open_session_tab / recovery) fires the hook.

A hook callback is registered for the duration of each test and unregistered
afterwards so a leaked registration never crosses into another test.
"""
from __future__ import annotations

import pytest



class _StubDaemon:
    def resolve_ws_url(self):
        raise AssertionError("daemon should not be touched")

    def invalidate(self):
        pass


class _FakeCDP:
    _closed = False

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.attached: list[str] = []
        self._sessions: dict[str, str] = {}

    def attach(self, target_id: str) -> str:
        self.attached.append(target_id)
        return self._sessions.setdefault(target_id, f"sid-{target_id}")

    def send(self, method: str, *, session: str | None = None, **params):
        self.calls.append((method, {"session": session, **params}))
        response = self.responses.get(method, {})
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(method=method, session=session, **params)
        return response


@pytest.fixture
def hooked_session(fresh_modules, tmp_bs_home):
    """A session with a real ledger record (so persist_target writes through)
    plus a fake CDP, and an installed target-changed hook that records calls."""
    from browserwright import session_registry as reg
    from browserwright.session import Session, with_session

    sid = reg.allocate(backend="rdp", owner="attach", name="issue21")
    rec = reg.get(sid)
    sess = Session(record=rec)
    sess.current_target_id = "ext-tab-A"
    fake = _FakeCDP(
        {
            "Target.getTargets": {
                "targetInfos": [
                    {"type": "page", "targetId": "ext-tab-A",
                     "url": "about:blank", "title": "", "attached": True},
                    {"type": "page", "targetId": "ext-tab-B",
                     "url": "https://b.test/", "title": "B", "attached": True},
                    {"type": "page", "targetId": "ext-tab-C",
                     "url": "https://c.test/", "title": "C", "attached": True},
                ]
            },
            "BrowserwrightDaemon.closeTab": {"ok": True, "tabId": 3},
        }
    )
    sess._cdp = fake  # type: ignore[attr-defined]
    fake._sessions["ext-tab-A"] = "sid-A"
    fake._sessions["ext-tab-B"] = "sid-B"
    fake._sessions["ext-tab-C"] = "sid-C"

    from browserwright import session_runtime as rt

    fired: list = []
    rt.set_target_changed_hook(lambda s: fired.append(s))
    with with_session(sess):
        yield sess, fake, fired, sid
    rt.set_target_changed_hook(None)


def test_bind_target_fires_hook_and_repoints_current_target(hooked_session):
    from browserwright import session_registry as reg
    from browserwright import session_runtime as rt

    sess, fake, fired, sid = hooked_session
    reg.update(sid, runtime={
        "current_target_id": "ext-tab-A", "updated_at": 0.0,
    })

    assert rt.bind_target(sess, "ext-tab-C") == {"targetId": "ext-tab-C"}
    assert sess.current_target_id == "ext-tab-C"
    # The hook fired with this session (executor rebinds its live page).
    assert fired == [sess], f"hook not fired by bind_target: {fired}"
    rec = reg.get(sid)
    assert (rec.get("runtime") or {}).get("current_target_id") == "ext-tab-C"


def test_close_of_current_tab_clears_persists_and_fires_hook(hooked_session):
    from browserwright import session_registry as reg
    from browserwright import session_runtime as rt

    sess, fake, fired, sid = hooked_session
    reg.update(sid, runtime={
        "current_target_id": "ext-tab-A", "updated_at": 0.0,
    })
    fired.clear()

    # Closing the CURRENT tab clears the binding, persists the clearing (so a
    # later process never fast-paths onto a dead target), and fires the hook.
    assert rt.close_session_tab(sess, target_id="ext-tab-A") == {
        "ok": True, "tabId": 3}
    assert sess.current_target_id is None
    assert fired == [sess], f"hook not fired on close-of-current: {fired}"
    rec = reg.get(sid)
    runtime = rec.get("runtime") or {}
    assert runtime.get("current_target_id") is None
    # The durable group anchor is NOT wiped by the clearing.


def test_close_of_non_current_tab_touches_nothing(hooked_session):
    from browserwright import session_registry as reg
    from browserwright import session_runtime as rt

    sess, fake, fired, sid = hooked_session
    reg.update(sid, runtime={
        "current_target_id": "ext-tab-C", "updated_at": 0.0,
    })
    sess.current_target_id = "ext-tab-C"  # in-process mirror of the ledger
    fired.clear()
    # no ledger write, no hook (the executor's page stays on the current tab).
    assert rt.close_session_tab(sess, target_id="ext-tab-A") == {
        "ok": True, "tabId": 3}
    assert sess.current_target_id == "ext-tab-C"
    assert fired == [], f"hook fired on close of non-current tab: {fired}"
    rec = reg.get(sid)
    assert (rec.get("runtime") or {}).get("current_target_id") == "ext-tab-C"


def test_register_recovered_fires_hook(hooked_session):
    from browserwright import session_runtime as rt

    sess, fake, fired, _sid = hooked_session
    fired.clear()

    payload = {
        "targetId": "ext-tab-D", "sessionId": "sid-D",
        "tabId": 4, "url": "https://d.test/", "groupId": 9,
    }
    out = rt.register_recovered(sess, payload)
    assert out == "ext-tab-D"
    assert sess.current_target_id == "ext-tab-D"
    assert fired == [sess], f"hook not fired by register_recovered: {fired}"


def test_hook_failure_never_breaks_tab_op(hooked_session):
    from browserwright import session_runtime as rt

    sess, fake, fired, _sid = hooked_session

    def boom(_sess):
        raise RuntimeError("rebind failed")

    rt.set_target_changed_hook(boom)
    try:
        # bind_target must still succeed even though the hook raised.
        assert rt.bind_target(sess, "ext-tab-B") == {"targetId": "ext-tab-B"}
    finally:
        rt.set_target_changed_hook(None)


def test_persist_target_none_clears_the_current_target(hooked_session):
    from browserwright import session_registry as reg
    from browserwright import session_runtime as rt

    sess, _fake, _fired, sid = hooked_session
    reg.update(sid, runtime={
        "current_target_id": "ext-tab-A", "updated_at": 0.0,
    })

    rt.persist_target(None, sess=sess)
    rec = reg.get(sid)
    runtime = rec.get("runtime") or {}
    assert runtime.get("current_target_id") is None
