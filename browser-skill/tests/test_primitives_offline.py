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
