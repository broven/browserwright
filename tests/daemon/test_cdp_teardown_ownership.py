"""Teardown must never close a browser we did not launch (#38).

Before the merge, `env` sessions were kept safe *structurally*: they had no
per-session context, so teardown found nothing to do and returned early. That
accident is gone — every cdp session has a context now, so attach-owned
sessions run the full teardown path.

Which means the ownership rule is no longer protected by "we never get there".
It rests on one data dependency inside `CdpUpstream`: `_launch_browser` is the
only writer of `browser_pid`, it runs only when `owns_browser` is true, and
every kill path is gated on `browser_pid is not None`. These tests pin that
chain through the daemon's one teardown entry point, because a browser closed
out from under a user is not a failure they can undo.
"""
from __future__ import annotations

import os
import time

import pytest

from browserwright.daemon.config import Config
from browserwright.daemon.errors import Unavailable
from browserwright.daemon.server.daemon import Daemon, UnknownSessionError
from browserwright.daemon.server.upstream_context import build_context


@pytest.fixture
def no_signals(monkeypatch):
    """Any signal sent to any pid fails the test, loudly and immediately."""
    def _boom(pid, sig):
        pytest.fail(
            f"teardown sent signal {sig} to pid {pid} — an attached browser "
            "belongs to someone else and must be left running")
    monkeypatch.setattr(os, "kill", _boom)


@pytest.fixture
def ledger(monkeypatch):
    """An in-memory ledger: the daemon routes by record, never by argument."""
    rows: dict[str, dict] = {}
    monkeypatch.setattr("browserwright.session_registry.get", rows.get)
    return rows


def _daemon_with_session(ledger: dict, record: dict,
                         session_id: str = "s1") -> Daemon:
    ledger[session_id] = record
    shared = build_context(backend="extension", cfg=Config(backend="extension"))
    daemon = Daemon(cfg=Config(backend="extension"), shared_context=shared)
    ctx = daemon.context_for_required(session_id)
    assert daemon.contexts[session_id] is ctx
    return daemon


@pytest.mark.asyncio
async def test_attach_teardown_sends_no_signal_and_drops_the_context(
        no_signals, ledger):
    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"url": "ws://cloud.example/cdp"},
    })
    upstream = daemon.contexts["s1"].upstream

    assert upstream.owns_browser is False
    assert upstream.browser_pid is None

    result = await daemon.end_workspace("s1")
    assert result["ok"] is True
    assert "s1" not in daemon.contexts


@pytest.mark.asyncio
async def test_attach_by_port_is_equally_untouched(no_signals, ledger):
    """A local port is still someone else's browser when we only attached."""
    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })

    assert (await daemon.end_workspace("s1"))["ok"] is True
    assert "s1" not in daemon.contexts


@pytest.mark.asyncio
async def test_trigger_close_on_an_attach_holder_sends_no_signal(
        no_signals, ledger):
    """`trigger_close` runs on every close path — idle, shutdown, chrome_exit.

    Closing the adapter kills unconditionally *except* for the
    `browser_pid is not None` gate, so it is the widest place the ownership
    rule could leak.
    """
    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"url": "ws://cloud.example/cdp"},
    })

    await daemon.contexts["s1"].holder.trigger_close("idle_close")
    await daemon.contexts["s1"].upstream.close(reason="idle_close")


async def _launch_then_fail_to_connect(daemon, monkeypatch, pid: int) -> None:
    """Drive the real open path far enough to launch: the launcher reports
    ``pid``, then the endpoint cannot be resolved, so the open fails with the
    Chrome still owned."""
    async def fake_launch(cfg, **_kw):
        return {"extras": {"pid": pid, "profile_path": "/tmp/bs-s1"}}

    async def fail_resolve(_cfg):
        raise Unavailable("nothing listening yet")

    monkeypatch.setattr(
        "browserwright.daemon.launch_chrome.launch_chrome", fake_launch)
    monkeypatch.setattr("browserwright.daemon.resolver.resolve", fail_resolve)
    with pytest.raises(Unavailable):
        await daemon.contexts["s1"].holder.ensure_open()


@pytest.mark.asyncio
async def test_create_owned_teardown_does_kill_its_own_chrome(
        monkeypatch, ledger):
    """The other half: a browser we launched must not be leaked."""
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "create",
        "workspace": {"port": 9444},
    })
    upstream = daemon.contexts["s1"].upstream
    assert upstream.owns_browser is True
    await _launch_then_fail_to_connect(daemon, monkeypatch, 424242)
    assert upstream.browser_pid == 424242

    result = await daemon.end_workspace("s1")

    assert result["ok"] is True
    assert [pid for pid, _ in killed] == [424242]
    assert upstream.browser_pid is None


@pytest.mark.asyncio
async def test_ownership_is_read_from_the_ledger_not_from_the_client(
        no_signals, ledger):
    """`owner` crosses the ledger→context boundary exactly once, here."""
    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })
    assert daemon.contexts["s1"].upstream.owns_browser is False

    other = _daemon_with_session(ledger, {
        "id": "s2", "backend": "cdp", "owner": "create",
        "workspace": {"port": 9333},
    }, session_id="s2")
    assert other.contexts["s2"].upstream.owns_browser is True


@pytest.mark.asyncio
async def test_teardown_failure_surfaces_as_a_partial_end_session(
        no_signals, ledger):
    """The behaviour change #38 makes user-visible, asserted rather than assumed.

    `env` could never report a failed teardown — `ok` was structurally always
    True. A cdp session can (budget exceeded), which `session end` reports as
    partial and keeps the row for retry. More honest, but genuinely new state.
    """
    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })

    # An already-expired budget is the real path to a failure: teardown aborts
    # the close etiquette rather than running past the caller's deadline.
    result = await daemon.end_workspace(
        "s1", deadline=time.monotonic() - 1.0)

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["timedOut"] is True
    assert result["failed"] == ["workspace"]
    # Retained for the retry rather than silently dropped — a context whose
    # close never completed still owns a socket.
    assert "s1" in daemon.contexts


@pytest.mark.asyncio
async def test_teardown_of_an_unknown_session_fails_closed(no_signals, ledger):
    shared = build_context(backend="extension", cfg=Config(backend="extension"))
    daemon = Daemon(cfg=Config(backend="extension"), shared_context=shared)

    with pytest.raises(UnknownSessionError):
        await daemon.end_workspace("nope")


def test_drop_context_is_sync_and_returns_what_it_dropped(no_signals, ledger):
    daemon = _daemon_with_session(ledger, {
        "id": "s1", "backend": "cdp", "owner": "attach",
        "workspace": {"port": 9222},
    })
    ctx = daemon.contexts["s1"]

    assert daemon.drop_context("s1") is ctx
    assert daemon.drop_context("s1") is None


def test_adapter_starts_with_no_pid_regardless_of_ownership(no_signals, ledger):
    """`browser_pid` has exactly one writer, and it is not context creation."""
    for owner in ("attach", "create"):
        daemon = _daemon_with_session(ledger, {
            "id": "s1", "backend": "cdp", "owner": owner,
            "workspace": {"port": 9222},
        })
        assert daemon.contexts["s1"].upstream.browser_pid is None
