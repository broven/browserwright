"""Session-decision memory + session_create.choose() (P7)."""
import json

import pytest


def test_record_then_lookup_roundtrips(tmp_bs_home):
    from browserwright.memory import session_decisions as sd

    assert sd.lookup("github.com deep research") is None
    sd.record("github.com deep research", {"backend": "extension", "mode": "attach"})
    assert sd.lookup("github.com deep research") == {"backend": "extension", "mode": "attach"}


def test_record_overwrites(tmp_bs_home):
    from browserwright.memory import session_decisions as sd

    sd.record("sit", {"backend": "rdp", "mode": "create"})
    sd.record("sit", {"backend": "rdp", "mode": "attach", "target": 9222})
    assert sd.lookup("sit") == {"backend": "rdp", "mode": "attach", "target": 9222}


def test_choose_hit_returns_decision(tmp_bs_home):
    from browserwright import session_create
    from browserwright.memory import session_decisions as sd

    sd.record("scrape with fingerprint", {"backend": "rdp", "mode": "attach", "target": 9222})
    assert session_create.choose("scrape with fingerprint") == {
        "backend": "rdp", "mode": "attach", "target": 9222}


def test_choose_miss_raises_needs_confirm_naming_three_modes(tmp_bs_home):
    from browserwright import session_create
    from browserwright.errors import NeedsUserConfirm

    with pytest.raises(NeedsUserConfirm) as ei:
        session_create.choose("a brand new situation")
    blob = (str(ei.value) + json.dumps(ei.value.proposal or {})).lower()
    assert "extension" in blob
    assert "rdp" in blob
    assert "create" in blob and "attach" in blob
