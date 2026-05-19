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
    _closed = False
    def __init__(self, payload):
        self.payload = payload
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
    """On rdp/autoconnect/env an empty getTargets is legitimate. No raise."""
    from browser_skill.primitives import list_tabs
    _stub_session(monkeypatch, backend="rdp", targets=[])
    assert list_tabs() == []


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
