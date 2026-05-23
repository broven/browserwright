"""Transparent reconnect-recovery (session_runtime.ensure_session_target +
persist_target). Uses a stub CDP — no live daemon."""
from __future__ import annotations

import pytest


def _cdp_error(**kw):
    # Import lazily so we raise the SAME CDPError class the production code
    # currently has imported — other tests delete browserwright.* from
    # sys.modules, which can otherwise mint a second, non-matching class.
    from browserwright.errors import CDPError
    return CDPError(**kw)


class _StubCDP:
    """Records send() calls; attach() behaviour is configurable."""

    def __init__(self, *, attach_raises: bool = False,
                 recover_response: dict | None = None,
                 recover_raises: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self._attach_raises = attach_raises
        self._recover_response = recover_response or {}
        self._recover_raises = recover_raises
        self._sessions: dict[str, str] = {}
        self._events: dict[str, object] = {}

    def attach(self, target_id: str) -> str:
        self.calls.append(("attach", {"targetId": target_id}))
        if self._attach_raises:
            raise _cdp_error(method="Target.attachToTarget",
                             params={"targetId": target_id},
                             cdp_message="no such target")
        return self._sessions.setdefault(target_id, "sid-cached")

    def send(self, method: str, *, session: str | None = None, **params) -> dict:
        self.calls.append((method, {"session": session, **params}))
        if method == "BrowserwrightDaemon.recoverSession":
            if self._recover_raises:
                raise _cdp_error(method=method, params=params,
                                 cdp_message="no matching group")
            return self._recover_response
        return {}


class _StubSession:
    def __init__(self, cdp, *, backend="extension", record=None):
        self.cdp = cdp
        self.current_target_id = None
        self.session_record = record
        self._backend_name_cache = backend

    @property
    def backend_name(self) -> str:
        return self._backend_name_cache


def _ledger_session(name="cf-bots", *, runtime=None):
    from browserwright import session_registry as reg
    sid = reg.allocate(backend="extension",
                       owner="attach", name=name)
    if runtime is not None:
        reg.update(sid, runtime=runtime)
    return sid, reg.get(sid)


def test_fast_path_uses_runtime_cache_no_recover(tmp_bs_home):
    """current_target_id=None, ledger.runtime.current_target_id set, attach
    succeeds → ensure_session_target returns it WITHOUT sending recoverSession."""
    from browserwright.session_runtime import ensure_session_target

    sid, rec = _ledger_session(runtime={"current_target_id": "ext-tab-7"})
    cdp = _StubCDP()
    sess = _StubSession(cdp, record=rec)

    tid = ensure_session_target(sess)
    assert tid == "ext-tab-7"
    assert sess.current_target_id == "ext-tab-7"
    # Fast path: attached the cached tab, never queried the group.
    assert ("attach", {"targetId": "ext-tab-7"}) in cdp.calls
    assert not any(m == "BrowserwrightDaemon.recoverSession" for m, _ in cdp.calls)


def test_fallback_recovers_by_group_and_writes_runtime(tmp_bs_home):
    """Persisted runtime.group_id (no current_target_id) → sends
    recoverSession(groupId=...), registers the binding, writes ledger.runtime,
    sets current_target_id. Recovery keys on the durable groupId, not the title
    (names are no longer unique)."""
    from browserwright import session_registry as reg
    from browserwright.session_runtime import ensure_session_target

    sid, rec = _ledger_session(name="cf-bots", runtime={"group_id": 4})
    cdp = _StubCDP(recover_response={
        "sessionId": "ws-sid-9", "targetId": "ext-tab-9", "tabId": 9,
        "url": "https://x.test", "title": "X", "groupId": 4,
    })
    sess = _StubSession(cdp, record=rec)

    tid = ensure_session_target(sess)
    assert tid == "ext-tab-9"
    assert sess.current_target_id == "ext-tab-9"

    recover = [(m, p) for m, p in cdp.calls if m == "BrowserwrightDaemon.recoverSession"]
    assert len(recover) == 1
    assert recover[0][1]["groupId"] == 4
    assert recover[0][1]["bsSession"] == sid
    # binding registered locally + written back to the ledger cache
    assert cdp._sessions["ext-tab-9"] == "ws-sid-9"
    assert reg.get(sid)["runtime"]["current_target_id"] == "ext-tab-9"


def test_fallback_after_stale_runtime_attach_failure(tmp_bs_home):
    """Stale runtime tab (attach raises) → falls through to recoverSession."""
    from browserwright.session_runtime import ensure_session_target

    sid, rec = _ledger_session(
        runtime={"current_target_id": "ext-tab-stale", "group_id": 4})
    cdp = _StubCDP(attach_raises=True, recover_response={
        "sessionId": "ws-sid-2", "targetId": "ext-tab-2",
    })
    sess = _StubSession(cdp, record=rec)

    tid = ensure_session_target(sess)
    assert tid == "ext-tab-2"
    assert any(m == "BrowserwrightDaemon.recoverSession" for m, _ in cdp.calls)


def test_empty_group_returns_none(tmp_bs_home):
    """Daemon error on recoverSession (no/empty group) → returns None."""
    from browserwright.session_runtime import ensure_session_target

    sid, rec = _ledger_session(name="ghost")
    cdp = _StubCDP(recover_raises=True)
    sess = _StubSession(cdp, record=rec)

    assert ensure_session_target(sess) is None
    assert sess.current_target_id is None


def test_entrypoint_opens_fresh_tab_when_recovery_fails(tmp_bs_home, monkeypatch):
    """De-branched (docs §Tier B): when recovery yields nothing, the entrypoint
    no longer raises NeedsUserConfirm on the extension backend. It falls through
    to current_page() → open() (a NEW working tab, never adopt), uniform across
    backends. Here the stub returns no openBackgroundTab payload, so open()
    surfaces a CDPError — proving the path now goes through open(), NOT the old
    extension-only NeedsUserConfirm refusal."""
    from browserwright import session as session_mod
    from browserwright.errors import CDPError, NeedsUserConfirm
    from browserwright.primitives.interact import _attached_session

    sid, rec = _ledger_session(name="ghost2")
    cdp = _StubCDP(recover_raises=True)
    sess = _StubSession(cdp, record=rec)
    monkeypatch.setattr(session_mod, "_singleton", sess)

    with pytest.raises(CDPError) as exc_info:
        _attached_session()
    # It went through open()'s unified verb, not the removed refusal.
    assert not isinstance(exc_info.value, NeedsUserConfirm)
    assert "openBackgroundTab" in str(exc_info.value)


def test_persist_target_writes_runtime_on_open_background(tmp_bs_home, monkeypatch):
    """After open_background, reg.get(sid)['runtime'] is populated."""
    from browserwright import session as session_mod
    from browserwright import session_registry as reg
    from browserwright.primitives.page import open_background

    sid, rec = _ledger_session(name="ob")
    monkeypatch.setenv("BD_SESSION", sid)
    cdp = _StubCDP(recover_response={})
    sess = _StubSession(cdp, record=rec)
    # open_background uses sess.cdp.send for openBackgroundTab; give it a payload
    monkeypatch.setattr(session_mod, "_singleton", sess)

    def _send(method, *, session=None, **params):
        cdp.calls.append((method, {"session": session, **params}))
        if method == "BrowserwrightDaemon.openBackgroundTab":
            return {"sessionId": "ws-sid-ob", "targetId": "ext-tab-ob",
                    "tabId": 1, "url": params.get("url"), "title": "",
                    "groupId": 5}
        return {}

    cdp.send = _send  # type: ignore[assignment]
    open_background("https://ob.test")

    runtime = reg.get(sid)["runtime"]
    assert runtime["current_target_id"] == "ext-tab-ob"
    assert runtime["group_id"] == 5
