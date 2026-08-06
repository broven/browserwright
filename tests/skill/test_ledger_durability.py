"""Ledger write durability and permissions.

The ledger is about to hold CDP endpoints for external browsers (#38), and
those routinely carry a reusable token in the userinfo or query string. That
changes the file from "a list of session ids" to "a credential store", which is
what justifies the atomic write and the mode bits locked down here.
"""
from __future__ import annotations

import json
import stat

import pytest

from browserwright import session_registry


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_ledger_file_is_owner_only(tmp_bs_home):
    session_registry.allocate(backend="extension", owner="attach", name="s")
    assert _mode(session_registry._ledger_path()) == 0o600


def test_sessions_dir_is_owner_only(tmp_bs_home):
    session_registry.allocate(backend="extension", owner="attach", name="s")
    assert _mode(session_registry._dir()) == 0o700


def test_dir_permissions_are_repaired_for_a_legacy_loose_directory(tmp_bs_home):
    """A ledger dir created by an older version must not stay world-readable."""
    d = session_registry._dir()
    d.chmod(0o755)
    session_registry.allocate(backend="extension", owner="attach", name="s")
    assert _mode(session_registry._dir()) == 0o700


def test_write_leaves_no_temp_file_behind(tmp_bs_home):
    session_registry.allocate(backend="extension", owner="attach", name="s")
    leftovers = list(session_registry._dir().glob("*.tmp"))
    assert leftovers == []


def test_raising_inside_the_lock_leaves_the_ledger_byte_identical(tmp_bs_home):
    """The abort contract every guard in session_registry depends on.

    The write sits after the `yield` precisely so that a guard raising from the
    caller's body never reaches the file. If someone "helpfully" moves it into
    a `finally`, a rejected allocation would start persisting its own partial
    mutation — and this test is what catches that.
    """
    session_registry.allocate(backend="extension", owner="attach", name="keep")
    before = session_registry._ledger_path().read_bytes()

    with pytest.raises(RuntimeError):
        with session_registry._locked() as data:
            data["sessions"]["999"] = {"id": "999", "backend": "bogus"}
            data["next_id"] = 4242
            raise RuntimeError("guard rejected this write")

    assert session_registry._ledger_path().read_bytes() == before
    assert session_registry.get("999") is None


def test_rejected_allocation_leaves_the_ledger_untouched(tmp_bs_home):
    """Same contract, exercised through a real guard rather than a raw raise."""
    session_registry.allocate(
        backend="extension", owner="attach", name="taken", unique_name=True)
    before = session_registry._ledger_path().read_bytes()

    with pytest.raises(ValueError, match="already taken"):
        session_registry.allocate(
            backend="extension", owner="attach", name="taken", unique_name=True)

    assert session_registry._ledger_path().read_bytes() == before


def test_a_stale_temp_file_does_not_leak_loose_permissions(tmp_bs_home):
    """`os.open`'s mode applies only on creation, so a leftover tmp is repaired.

    Simulates a crash between write and rename that left a world-readable tmp.
    """
    session_registry.allocate(backend="extension", owner="attach", name="s")
    p = session_registry._ledger_path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("{}")
    tmp.chmod(0o644)

    session_registry.allocate(backend="extension", owner="attach", name="s2")

    assert _mode(p) == 0o600
    assert not tmp.exists()


def test_ledger_content_survives_the_atomic_write(tmp_bs_home):
    a = session_registry.allocate(backend="extension", owner="attach", name="a")
    b = session_registry.allocate(backend="rdp", owner="create",
                                  workspace={"port": 9333}, name="b")
    raw = json.loads(session_registry._ledger_path().read_text())
    assert set(raw["sessions"]) == {a, b}
    assert raw["sessions"][b]["workspace"] == {"port": 9333}
    assert raw["next_id"] == 3
