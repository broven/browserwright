"""P0 #75 — inline heredoc must abort on Chrome-popup-cost backends.

Spec: ``src/browser_skill/repl/inline.py`` — when the daemon would pick the
autoconnect backend (or any backend whose ``ux_cost`` mentions ``popup``)
and no long-lived ws is available to absorb the popup, ``browser-skill``
heredoc invocations must exit 2 with a clear error *before* attempting to
connect. Override: ``BS_FORCE_AUTOCONNECT_INLINE=1``. Bypass: Mode B alive,
``BS_CDP_WS`` set, or Skill REPL daemon already up.
"""
from __future__ import annotations

import io
import sys

import pytest


# ---- direct gate tests --------------------------------------------------
# `_check_inline_gate()` returns (should_abort, reason). Driving it
# directly keeps the test off the subprocess plane.


def _purge_env(monkeypatch):
    for v in (
        "BS_FORCE_AUTOCONNECT_INLINE",
        "BS_DAEMON_BACKEND",
        "BS_CDP_WS",
        "BU_CDP_WS",
        "BS_DAEMON_URL_CMD",
    ):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def gate(monkeypatch):
    """Yield ``_check_inline_gate`` with all environment-trip vars cleared
    and ``ModeBClient.is_alive`` stubbed to ``False`` by default."""
    _purge_env(monkeypatch)
    from browser_skill import mode_b_client
    monkeypatch.setattr(mode_b_client.ModeBClient, "is_alive",
                        lambda self: False)
    from browser_skill.repl import inline
    return inline


def _stub_doctor(monkeypatch, blob: dict) -> None:
    from browser_skill import daemon_client
    monkeypatch.setattr(daemon_client.DaemonClient, "doctor",
                        lambda self: blob)


def test_autoconnect_recommended_aborts(monkeypatch, gate):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "autoconnect",
        "backends": [
            {"name": "autoconnect", "available": True,
             "ux_cost": "popup-per-ws+banner"},
        ],
    })
    should_abort, reason = gate._check_inline_gate()
    assert should_abort is True
    assert "autoconnect" in reason


def test_popup_ux_cost_aborts_even_when_name_differs(monkeypatch, gate):
    # A custom backend that still costs a popup (defensive: the gate keys on
    # ``popup`` in ux_cost, not on the literal name "autoconnect").
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "weird_backend",
        "backends": [
            {"name": "weird_backend", "available": True,
             "ux_cost": "popup-per-ws"},
        ],
    })
    should_abort, _ = gate._check_inline_gate()
    assert should_abort is True


def test_rdp_backend_does_not_abort(monkeypatch, gate):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "rdp",
        "backends": [
            {"name": "rdp", "available": True, "ux_cost": "none"},
        ],
    })
    should_abort, reason = gate._check_inline_gate()
    assert should_abort is False
    assert reason == ""


def test_env_backend_does_not_abort(monkeypatch, gate):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "env",
        "backends": [
            {"name": "env", "available": True, "ux_cost": "none"},
        ],
    })
    should_abort, _ = gate._check_inline_gate()
    assert should_abort is False


def test_force_env_overrides_abort(monkeypatch, gate):
    monkeypatch.setenv("BS_FORCE_AUTOCONNECT_INLINE", "1")
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "autoconnect",
        "backends": [
            {"name": "autoconnect", "available": True,
             "ux_cost": "popup-per-ws+banner"},
        ],
    })
    should_abort, _ = gate._check_inline_gate()
    assert should_abort is False


def test_mode_b_alive_skips_abort(monkeypatch, gate):
    from browser_skill import mode_b_client
    monkeypatch.setattr(mode_b_client.ModeBClient, "is_alive",
                        lambda self: True)
    # Doctor must not even be reached — make it raise so we catch any leak.

    def _boom(self):
        raise AssertionError("doctor() should not be called when Mode B is alive")
    from browser_skill import daemon_client
    monkeypatch.setattr(daemon_client.DaemonClient, "doctor", _boom)
    should_abort, _ = gate._check_inline_gate()
    assert should_abort is False


def test_bs_cdp_ws_skips_abort(monkeypatch, gate):
    # F-4d (v0.5.0): BS_CDP_WS short-circuits the popup gate *only* when
    # the URL doesn't point at the autoconnect default port :9222 —
    # otherwise an env-pinned ws to the user's daily Chrome would
    # silently bypass every defense.
    monkeypatch.setenv("BS_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/abc")

    def _boom(self):
        raise AssertionError("doctor() should not be called when BS_CDP_WS is set")
    from browser_skill import daemon_client
    monkeypatch.setattr(daemon_client.DaemonClient, "doctor", _boom)
    should_abort, _ = gate._check_inline_gate()
    assert should_abort is False


def test_bs_daemon_backend_autoconnect_env_aborts_without_doctor(monkeypatch, gate):
    monkeypatch.setenv("BS_DAEMON_BACKEND", "autoconnect")

    def _boom(self):
        raise AssertionError("doctor() should not be called once env is set")
    from browser_skill import daemon_client
    monkeypatch.setattr(daemon_client.DaemonClient, "doctor", _boom)
    should_abort, reason = gate._check_inline_gate()
    assert should_abort is True
    assert "BS_DAEMON_BACKEND" in reason


def test_doctor_rate_limited_propagates_abort(monkeypatch, gate):
    # Daemon's #74 rate-limiter returned exit==2 with a "rate-limited"
    # stderr — DaemonClient packs that into a synthetic doctor blob.
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [],
        "error": "rate-limited: too many doctor probes in 10s window",
        "skill_synthetic": True,
        "exit_code": 2,
    })
    should_abort, reason = gate._check_inline_gate()
    assert should_abort is True
    assert "rate-limit" in reason.lower()


def test_doctor_unreachable_does_not_abort(monkeypatch, gate):
    # Binary missing, transient failure → don't block the agent; we can't
    # prove popup risk and would just create false negatives.
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [],
        "error": "browser-daemon: not found on PATH",
        "skill_synthetic": True,
    })
    should_abort, _ = gate._check_inline_gate()
    assert should_abort is False


# ---- end-to-end via run() ------------------------------------------------


def test_run_aborts_with_exit_2_and_writes_help(monkeypatch, gate, capsys):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "autoconnect",
        "backends": [
            {"name": "autoconnect", "available": True,
             "ux_cost": "popup-per-ws+banner"},
        ],
    })
    # Make absolutely sure we don't trip is_repl_running() and short-circuit
    # past the gate.
    from browser_skill.repl import client as repl_client
    monkeypatch.setattr(repl_client, "is_repl_running", lambda: False)
    from browser_skill.repl import inline
    monkeypatch.setattr(inline, "is_repl_running", lambda: False)

    rc = gate.run(io.StringIO("print('should never run')\n"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "Refusing inline heredoc" in err
    # Spec: error must point the user at the safe alternatives.
    assert "repl start" in err
    assert "launch-chrome" in err
    assert "BS_FORCE_AUTOCONNECT_INLINE" in err


def test_run_force_env_lets_safe_python_through(monkeypatch, gate, capsys):
    monkeypatch.setenv("BS_FORCE_AUTOCONNECT_INLINE", "1")
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "recommended": "autoconnect",
        "backends": [
            {"name": "autoconnect", "available": True,
             "ux_cost": "popup-per-ws+banner"},
        ],
    })
    from browser_skill.repl import client as repl_client
    monkeypatch.setattr(repl_client, "is_repl_running", lambda: False)
    from browser_skill.repl import inline
    monkeypatch.setattr(inline, "is_repl_running", lambda: False)

    # Pure-Python snippet (no CDP needed) — exercises the gate-passes path.
    rc = gate.run(io.StringIO("print('hello inline')\n"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "hello inline" in out
