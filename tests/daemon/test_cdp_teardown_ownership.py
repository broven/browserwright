"""Teardown must never close a browser we did not launch (#38).

Before the merge, `env` sessions were kept safe *structurally*: they had no
per-session context, so `_end_raw_session` found nothing to tear down and
returned early. That accident is gone — every cdp session has a context now, so
attach-owned sessions run the full teardown path for the first time.

Which means the ownership rule is no longer protected by "we never get there".
It rests on one data dependency: `_launch_cdp_chrome` is the only writer of
`cdp_pid`, it runs only when `cdp_owns_browser` is true, and every kill path is
gated on `cdp_pid is not None`. These tests pin that chain, because a browser
closed out from under a user is not a failure they can undo.
"""
from __future__ import annotations

import os


import pytest

from browserwright.daemon.config import Config
from browserwright.daemon.server.daemon import Daemon
from browserwright.daemon.server.listener import make_context


@pytest.fixture
def no_signals(monkeypatch):
    """Any signal sent to any pid fails the test, loudly and immediately."""
    def _boom(pid, sig):
        pytest.fail(
            f"teardown sent signal {sig} to pid {pid} — an attached browser "
            "belongs to someone else and must be left running")
    monkeypatch.setattr(os, "kill", _boom)


def _daemon_with_session(record: dict, session_id: str = "s1") -> Daemon:
    shared = make_context(backend="extension", cfg=Config(backend="extension"))
    daemon = Daemon(cfg=Config(backend="extension"), shared_context=shared,
                    make_context=make_context)
    ctx = daemon._ensure_cdp_context(session_id, record)
    assert daemon.contexts[session_id] is ctx
    return daemon


@pytest.mark.asyncio
async def test_attach_teardown_sends_no_signal_and_drops_the_context(no_signals):
    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"url": "ws://cloud.example/cdp"},
    })
    holder = daemon.contexts["s1"].holder

    assert holder.cdp_owns_browser is False
    assert holder.cdp_pid is None

    assert await daemon.teardown_cdp_context("s1") is True
    assert "s1" not in daemon.contexts


@pytest.mark.asyncio
async def test_attach_by_port_is_equally_untouched(no_signals):
    """A local port is still someone else's browser when we only attached."""
    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })

    assert await daemon.teardown_cdp_context("s1") is True
    assert "s1" not in daemon.contexts


@pytest.mark.asyncio
async def test_trigger_close_on_an_attach_holder_sends_no_signal(no_signals):
    """`trigger_close` runs on every close path — idle, shutdown, chrome_exit.

    It kills unconditionally *except* for the `cdp_pid is not None` gate, so it
    is the widest place the ownership rule could leak.
    """
    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"url": "ws://cloud.example/cdp"},
    })

    await daemon.contexts["s1"].holder.trigger_close("idle_close")


@pytest.mark.asyncio
async def test_create_owned_teardown_does_kill_its_own_chrome(monkeypatch):
    """The other half: a browser we launched must not be leaked."""
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "create",
        "workspace": {"port": 9444},
    })
    holder = daemon.contexts["s1"].holder
    assert holder.cdp_owns_browser is True
    # Stand in for a launch: `_launch_cdp_chrome` is the only writer of this.
    holder.cdp_pid = 424242

    await daemon.teardown_cdp_context("s1")

    assert [pid for pid, _ in killed] == [424242]


@pytest.mark.asyncio
async def test_ownership_is_read_from_the_ledger_not_from_the_client(no_signals):
    """`owner` crosses the ledger→context boundary exactly once, here."""
    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })
    assert daemon.contexts["s1"].holder.cdp_owns_browser is False

    other = _daemon_with_session({
        "id": "s2", "backend": "cdp", "owner": "create",
        "workspace": {"port": 9333},
    }, session_id="s2")
    assert other.contexts["s2"].holder.cdp_owns_browser is True


@pytest.mark.asyncio
async def test_teardown_failure_surfaces_as_a_partial_end_session(no_signals):
    """The behaviour change #38 makes user-visible, asserted rather than assumed.

    `env` returned `None` from `_end_raw_session` and so could never report a
    failed teardown — `ok` was structurally always True. A cdp session can now
    return False (budget exceeded), which `session end` reports as partial and
    keeps the row for retry. More honest, but genuinely new state.
    """
    import time

    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })

    # An already-expired budget is the real path to a False: teardown aborts
    # the close etiquette rather than running past the caller's deadline.
    result = await daemon.teardown_cdp_context(
        "s1", deadline=time.monotonic() - 1.0)

    assert result is False
    # Retained for the retry rather than silently dropped — a context whose
    # close never completed still owns a socket.
    assert "s1" in daemon.contexts


@pytest.mark.asyncio
async def test_teardown_of_an_unknown_session_is_false_not_an_error(no_signals):
    shared = make_context(backend="extension", cfg=Config(backend="extension"))
    daemon = Daemon(cfg=Config(backend="extension"), shared_context=shared,
                    make_context=make_context)

    assert await daemon.teardown_cdp_context("nope") is False


def test_drop_context_is_sync_and_returns_what_it_dropped(no_signals):
    daemon = _daemon_with_session({
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })
    ctx = daemon.contexts["s1"]

    assert daemon.drop_cdp_context("s1") is ctx
    assert daemon.drop_cdp_context("s1") is None


def test_holder_starts_with_no_pid_regardless_of_ownership(no_signals):
    """`cdp_pid` has exactly one writer, and it is not context creation."""
    for owner in ("attach", "create"):
        daemon = _daemon_with_session({
            "id": "s1", "backend": "cdp", "owner": owner,
            "workspace": {"port": 9222},
        })
        assert daemon.contexts["s1"].holder.cdp_pid is None
