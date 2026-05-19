"""Primitives that don't require a live browser:

- ``current_session()`` returns a Session
- ``host_stem`` / lookup paths
- ``memory_read`` returns sensible defaults when no current tab
- ``bootstrap_site`` primitive works through the api re-export
"""
import pytest


def test_session_lazy(tmp_bs_home, fresh_modules):
    from browser_skill.session import current_session

    s = current_session()
    assert s is not None
    assert s.current_target_id is None


def test_bootstrap_site_via_api(tmp_bs_home, fresh_modules):
    import browser_skill

    out = browser_skill.bootstrap_site("https://example.com/page")
    assert "example" in out


def test_memory_read_no_current_tab(tmp_bs_home, fresh_modules):
    import browser_skill

    m = browser_skill.memory_read()
    assert "global" in m


def test_propose_solidify_through_api(tmp_bs_home, fresh_modules):
    import browser_skill

    # Bug 3 (v0.3.1): no history → returns a dict with ready=False +
    # diagnostic warnings, not None.
    out = browser_skill.propose_solidify()
    assert isinstance(out, dict)
    assert out["ready"] is False
    assert out["readiness_score"] == 0.0
    assert any("history" in w.lower() for w in out["warnings"])


def test_redaction_blocks_remember(tmp_bs_home, fresh_modules, capsys):
    import browser_skill

    out = browser_skill.remember("github.com",
                                 "Bearer abcdefghijklmnopqrstuvwxyz1234567890")
    assert out == ""
    err = capsys.readouterr().err
    assert "redaction" in err.lower()


# ---- extension-backend 0-tab guards ---------------------------------------


class _StubCDP:
    """Minimal CDPSession stand-in: send() returns whatever we set."""
    def __init__(self, payload):
        self.payload = payload
        # Per-instance — class-level would let one test's mutation leak
        # into another (Session.cdp's "_closed" guard reads this).
        self._closed = False
    def send(self, method, **_):
        return self.payload


def _stub_session(monkeypatch, *, backend: str, targets: list, current_target=None):
    """Replace the singleton with a session whose cdp/daemon are stubs."""
    from browser_skill.session import Session
    import browser_skill.session as session_mod
    sess = Session(daemon=object())
    sess._cdp = _StubCDP({"targetInfos": targets})
    sess._backend_name_cache = backend  # bypass daemon round-trip
    sess.current_target_id = current_target
    monkeypatch.setattr(session_mod, "_singleton", sess)
    return sess


def test_list_tabs_raises_on_extension_with_zero_ghosts(tmp_bs_home, monkeypatch):
    """Extension backend with no attached tabs is an actionable state, not
    an empty Chrome. The agent needs to call attach_active or
    open_background, so list_tabs raises NeedsUserConfirm pointing there
    instead of returning [] silently."""
    from browser_skill.errors import NeedsUserConfirm
    from browser_skill.primitives import list_tabs
    _stub_session(monkeypatch, backend="extension", targets=[])
    with pytest.raises(NeedsUserConfirm) as exc_info:
        list_tabs()
    assert "extension" in str(exc_info.value).lower()
    assert "attach_active" in (exc_info.value.proposal or "")


def test_list_tabs_returns_empty_on_other_backend(tmp_bs_home, monkeypatch):
    """On rdp/env an empty getTargets is legitimate. No raise."""
    from browser_skill.primitives import list_tabs
    _stub_session(monkeypatch, backend="rdp", targets=[])
    assert list_tabs() == []


def test_list_tabs_chrome_only_ghost_on_extension_does_not_raise(
        tmp_bs_home, monkeypatch):
    """H-1 regression: a ghost target IS attached even when it's on
    ``chrome://newtab/``. Filtering with ``include_chrome=False`` must
    return ``[]`` (legitimate "no real pages"), NOT raise NeedsUserConfirm
    (which would tell the agent to attach_active() and silently clear the
    cached current_target_id in current_page())."""
    from browser_skill.primitives import list_tabs
    # One page-type ghost target, but its URL is chrome-internal.
    ghost = {
        "targetId": "ext-tab-7",
        "type": "page",
        "url": "chrome://newtab/",
        "title": "New Tab",
        "attached": True,
    }
    _stub_session(monkeypatch, backend="extension", targets=[ghost])
    # include_chrome=False filters the ghost out — but raw_pages != [] so
    # the raise must NOT fire.
    assert list_tabs(include_chrome=False) == []
    # And the unfiltered call returns the ghost (sanity check that the
    # session was wired correctly).
    assert len(list_tabs(include_chrome=True)) == 1


def test_current_tab_raises_on_extension_without_attachment(tmp_bs_home, monkeypatch):
    """Extension backend + no current_target_id → raise (the agent's next
    move is attach_active). Mode A keeps returning None for the same shape."""
    from browser_skill.errors import NeedsUserConfirm
    from browser_skill.primitives import current_tab
    _stub_session(monkeypatch, backend="extension", targets=[],
                  current_target=None)
    with pytest.raises(NeedsUserConfirm):
        current_tab()


def test_current_tab_returns_none_on_other_backend_without_attachment(
        tmp_bs_home, monkeypatch):
    from browser_skill.primitives import current_tab
    _stub_session(monkeypatch, backend="rdp", targets=[], current_target=None)
    assert current_tab() is None


# ---- v0.5.5: switch_tab error when handle is stale ------------------------


class _AttachFailingCDP:
    """CDPSession stub where ``attach()`` raises CDPError — simulates a
    stale tab handle (tab closed since the targetId was issued)."""
    def __init__(self):
        self._closed = False
        from browser_skill.errors import CDPError
        self._CDPError = CDPError

    def attach(self, target_id):
        raise self._CDPError(
            method="Target.attachToTarget",
            params={"targetId": target_id},
            cdp_message="No target with given id found",
        )

    def send(self, method, **_):
        return {}


def test_switch_tab_stale_handle_raises_actionable(tmp_bs_home, monkeypatch):
    """v0.5.5: passing a stale targetId (tab closed) to switch_tab must
    raise with text that names the cause and the fix, not a bare CDP
    error. Heredoc agents need this — they hand-off targetIds across
    process boundaries and a closed tab is the common failure."""
    from browser_skill.session import Session
    import browser_skill.session as session_mod
    sess = Session(daemon=object())
    sess._cdp = _AttachFailingCDP()
    sess._backend_name_cache = "rdp"
    monkeypatch.setattr(session_mod, "_singleton", sess)

    from browser_skill.errors import CDPError
    from browser_skill.primitives import switch_tab
    with pytest.raises(CDPError) as exc:
        switch_tab("ghost-tab-deadbeef")
    msg = str(exc.value)
    assert "no longer exists" in msg
    assert "attach_active" in msg or "new_tab" in msg
    assert "ghost-tab-deadbeef" in msg
