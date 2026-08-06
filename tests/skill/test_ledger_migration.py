"""Retired-backend ledger rows (#38).

Without a sweep these rows are immortal, and the failure is a closed loop: the
daemon refuses to route an unrecognised backend, so `session end` cannot
confirm teardown and keeps the row "for retry" — and the retry takes the same
path to the same refusal. Auto-prune skips it for the same reason.
"""
from __future__ import annotations

import pytest

from browserwright import session_registry as reg


def test_rdp_rows_are_migrated_in_place(tmp_bs_home):
    """Identical semantics, so a silent rename is the honest move."""
    sid = reg.allocate(backend="rdp", owner="create",
                       workspace={"port": 9444}, name="job")

    swept = reg.migrate_legacy_backends()

    assert swept["migrated"] == [sid]
    assert swept["evicted"] == []
    row = reg.get(sid)
    assert row["backend"] == "cdp"
    # Everything else survives untouched — this is a rename, not a rebuild.
    assert row["workspace"] == {"port": 9444}
    assert row["owner"] == "create"
    assert row["name"] == "job"


def test_env_rows_are_evicted_and_reported(tmp_bs_home):
    """There is nothing to migrate an env row *to*.

    Its endpoint lived in its daemon's environment, never in the record, so a
    converted row would point nowhere. Eviction is returned rather than silent
    so the caller can say which session vanished and why.
    """
    sid = reg.allocate(backend="env", owner="attach", name="cloak")

    swept = reg.migrate_legacy_backends()

    assert swept["migrated"] == []
    assert [r["id"] for r in swept["evicted"]] == [sid]
    assert swept["evicted"][0]["name"] == "cloak"  # enough to name it in a log
    assert reg.get(sid) is None


def test_current_rows_are_left_alone(tmp_bs_home):
    ext = reg.allocate(backend="extension", owner="attach", name="daily")
    cdp = reg.allocate(backend="cdp", owner="attach",
                       workspace={"url": "ws://box/cdp"}, name="cloud")

    swept = reg.migrate_legacy_backends()

    assert swept == {"migrated": [], "evicted": []}
    assert reg.get(ext)["backend"] == "extension"
    assert reg.get(cdp)["workspace"] == {"url": "ws://box/cdp"}


def test_sweep_is_idempotent(tmp_bs_home):
    reg.allocate(backend="rdp", owner="attach", name="a")
    reg.migrate_legacy_backends()

    assert reg.migrate_legacy_backends() == {"migrated": [], "evicted": []}


def test_update_still_refuses_a_backend_change(tmp_bs_home):
    """Locks the reason the sweep goes through `_locked` directly.

    `update()` rejecting a backend change is correct for every caller but the
    migration; if that guard were relaxed to make the migration convenient,
    the immutability invariant would be gone for everyone.
    """
    sid = reg.allocate(backend="rdp", owner="attach", name="a")

    with pytest.raises(ValueError, match="backend is immutable"):
        reg.update(sid, backend="cdp")


def test_session_end_clears_a_legacy_row_without_calling_the_daemon(
    tmp_bs_home, monkeypatch,
):
    """The loop-breaker: no RPC, because no answer to it exists."""
    from browserwright import session_create

    sid = reg.allocate(backend="env", owner="attach", name="cloak")
    calls = []
    monkeypatch.setattr(session_create, "_run",
                        lambda cmd, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(session_create, "_ensure_daemon_running",
                        lambda: pytest.fail("must not spawn a daemon"))

    message = session_create.end(reg.get(sid))

    assert calls == []
    assert reg.get(sid) is None
    assert "no longer exists" in message


def test_session_new_sweeps_legacy_rows(tmp_bs_home, monkeypatch):
    """Upgrading and running any command clears them — not only a restart."""
    from browserwright import session_create

    stale = reg.allocate(backend="env", owner="attach", name="old")
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)

    session_create.new(backend="extension", name="fresh")

    assert reg.get(stale) is None
