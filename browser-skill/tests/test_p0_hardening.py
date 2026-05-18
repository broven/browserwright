"""P0 review fixes: F-4b production hardening + F-4d BS_CDP_WS guard +
F-5d Mode-B backend identity verification.

All tests pure mock — no real Chrome, no real daemon, no popup risk
(chrome-popup-test-policy + its filesystem analogue per ONBOARDING).
"""
from __future__ import annotations

import os
import subprocess

import pytest


# ---- F-4b: production hardening ------------------------------------


def _purge_hardening_env(monkeypatch):
    for v in ("BS_PRODUCTION_HARDENING",
              "BS_ALLOW_PORT_9222_LISTENER",
              "BS_CDP_WS", "BU_CDP_WS", "BD_BACKEND"):
        monkeypatch.delenv(v, raising=False)


def test_assert_safe_environment_proceeds_when_no_listener(monkeypatch):
    _purge_hardening_env(monkeypatch)
    from browser_skill import _hardening
    monkeypatch.setattr(_hardening, "_port_is_listening",
                        lambda *a, **kw: False)
    _hardening.assert_safe_environment()  # no raise


def test_assert_safe_environment_refuses_on_default_port_listener(monkeypatch):
    _purge_hardening_env(monkeypatch)
    from browser_skill import _hardening
    monkeypatch.setattr(_hardening, "_port_is_listening",
                        lambda *a, **kw: True)
    with pytest.raises(_hardening.ProductionHardeningRefused) as exc:
        _hardening.assert_safe_environment()
    msg = str(exc.value)
    assert "9222" in msg
    assert "popup" in msg.lower() or "chrome 144" in msg.lower()
    assert "BS_ALLOW_PORT_9222_LISTENER" in msg
    # User-actionable alternatives surfaced.
    assert "launch-chrome" in msg or "isolated" in msg


def test_assert_safe_environment_bypass_via_env(monkeypatch, capsys):
    _purge_hardening_env(monkeypatch)
    monkeypatch.setenv("BS_ALLOW_PORT_9222_LISTENER", "1")
    from browser_skill import _hardening
    monkeypatch.setattr(_hardening, "_port_is_listening",
                        lambda *a, **kw: True)
    _hardening.assert_safe_environment()  # no raise
    err = capsys.readouterr().err
    assert "WARNING" in err and "9222" in err


def test_assert_safe_environment_disabled_globally(monkeypatch):
    _purge_hardening_env(monkeypatch)
    monkeypatch.setenv("BS_PRODUCTION_HARDENING", "0")
    from browser_skill import _hardening
    # Even if the port IS listening, the check is skipped.
    monkeypatch.setattr(_hardening, "_port_is_listening",
                        lambda *a, **kw: True)
    _hardening.assert_safe_environment()  # no raise


def test_assert_safe_environment_skips_when_explicit_non_default_ws(monkeypatch):
    _purge_hardening_env(monkeypatch)
    monkeypatch.setenv("BS_CDP_WS",
                       "ws://127.0.0.1:9333/devtools/browser/abc")
    from browser_skill import _hardening
    # User pinned a non-default port — they've opted into autoconnect-
    # alternative, leave them alone.
    monkeypatch.setattr(_hardening, "_port_is_listening",
                        lambda *a, **kw: True)
    _hardening.assert_safe_environment()  # no raise


def test_assert_safe_environment_skips_when_non_autoconnect_backend(monkeypatch):
    _purge_hardening_env(monkeypatch)
    monkeypatch.setenv("BD_BACKEND", "rdp")
    from browser_skill import _hardening
    monkeypatch.setattr(_hardening, "_port_is_listening",
                        lambda *a, **kw: True)
    _hardening.assert_safe_environment()  # no raise


def test_assert_daemon_url_safe_refuses_default_port(monkeypatch):
    _purge_hardening_env(monkeypatch)
    from browser_skill import _hardening

    class _FakeProc:
        returncode = 0
        stdout = "ws://127.0.0.1:9222/devtools/browser/xyz\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc())
    with pytest.raises(_hardening.ProductionHardeningRefused) as exc:
        _hardening.assert_daemon_url_safe()
    msg = str(exc.value)
    assert "9222" in msg
    assert "BD_PORT" in msg  # mentions the typo-class root cause


def test_assert_daemon_url_safe_passes_for_isolated_port(monkeypatch):
    _purge_hardening_env(monkeypatch)
    from browser_skill import _hardening

    class _FakeProc:
        returncode = 0
        stdout = "ws://127.0.0.1:9333/devtools/browser/xyz\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc())
    _hardening.assert_daemon_url_safe()  # no raise


def test_assert_daemon_url_safe_skips_when_daemon_missing(monkeypatch):
    _purge_hardening_env(monkeypatch)
    from browser_skill import _hardening

    def _no_daemon(*a, **kw):
        raise FileNotFoundError("browser-daemon")

    monkeypatch.setattr(subprocess, "run", _no_daemon)
    _hardening.assert_daemon_url_safe()  # no raise — nothing to assert


# ---- F-4d: BS_CDP_WS inline-gate validation ------------------------


def _purge_inline_env(monkeypatch):
    for v in ("BS_FORCE_AUTOCONNECT_INLINE", "BS_DAEMON_BACKEND",
              "BS_CDP_WS", "BU_CDP_WS", "BS_DAEMON_URL_CMD",
              "BS_PRODUCTION_HARDENING", "BS_ALLOW_PORT_9222_LISTENER"):
        monkeypatch.delenv(v, raising=False)


def test_inline_gate_aborts_when_bs_cdp_ws_points_at_default_port(monkeypatch):
    _purge_inline_env(monkeypatch)
    monkeypatch.setenv("BS_CDP_WS",
                       "ws://127.0.0.1:9222/devtools/browser/foo")
    from browser_skill import mode_b_client
    monkeypatch.setattr(mode_b_client.ModeBClient, "is_alive",
                        lambda self: False)
    from browser_skill.repl import inline
    should_abort, reason = inline._check_inline_gate()
    assert should_abort is True
    assert "9222" in reason
    assert "BS_CDP_WS" in reason or "autoconnect" in reason


def test_inline_gate_allows_bs_cdp_ws_on_isolated_port(monkeypatch):
    _purge_inline_env(monkeypatch)
    monkeypatch.setenv("BS_CDP_WS",
                       "ws://127.0.0.1:9333/devtools/browser/foo")
    from browser_skill import mode_b_client
    monkeypatch.setattr(mode_b_client.ModeBClient, "is_alive",
                        lambda self: False)
    from browser_skill.repl import inline
    should_abort, _ = inline._check_inline_gate()
    assert should_abort is False


def test_inline_gate_force_flag_overrides_default_port_ws(monkeypatch):
    _purge_inline_env(monkeypatch)
    monkeypatch.setenv("BS_CDP_WS",
                       "ws://127.0.0.1:9222/devtools/browser/foo")
    monkeypatch.setenv("BS_FORCE_AUTOCONNECT_INLINE", "1")
    from browser_skill import mode_b_client
    monkeypatch.setattr(mode_b_client.ModeBClient, "is_alive",
                        lambda self: False)
    from browser_skill.repl import inline
    should_abort, _ = inline._check_inline_gate()
    assert should_abort is False


# ---- F-5d: Mode-B backend identity verification --------------------


def test_assert_backend_matches_passes_when_same(monkeypatch):
    from browser_skill.mode_b_client import ModeBClient
    mb = ModeBClient()
    monkeypatch.setattr(ModeBClient, "get_backend_info",
                        lambda self: {"backend": "rdp"})
    mb.assert_backend_matches("rdp")  # no raise


def test_assert_backend_matches_raises_on_mismatch(monkeypatch):
    from browser_skill.errors import DaemonBackendMismatch
    from browser_skill.mode_b_client import ModeBClient
    mb = ModeBClient(name="foo")
    monkeypatch.setattr(ModeBClient, "get_backend_info",
                        lambda self: {"backend": "autoconnect"})
    with pytest.raises(DaemonBackendMismatch) as exc:
        mb.assert_backend_matches("rdp")
    err = exc.value
    assert err.requested == "rdp"
    assert err.actual == "autoconnect"
    assert err.name == "foo"
    # User-actionable message.
    msg = str(err)
    assert "rdp" in msg and "autoconnect" in msg
    assert "browser-daemon stop" in msg or "BD_NAME" in msg


def test_assert_backend_matches_silently_skips_when_daemon_lacks_rpc(monkeypatch):
    """Older daemons without ``backend-info`` CLI → can't verify →
    fall through to original behaviour (no raise)."""
    from browser_skill.mode_b_client import ModeBClient
    mb = ModeBClient()
    monkeypatch.setattr(ModeBClient, "get_backend_info",
                        lambda self: None)
    mb.assert_backend_matches("rdp")  # no raise


def test_assert_backend_matches_skip_env(monkeypatch):
    monkeypatch.setenv("BS_SKIP_BACKEND_IDENTITY_CHECK", "1")
    from browser_skill.mode_b_client import ModeBClient
    mb = ModeBClient()
    monkeypatch.setattr(ModeBClient, "get_backend_info",
                        lambda self: {"backend": "autoconnect"})
    mb.assert_backend_matches("rdp")  # skip env → no raise


def test_assert_backend_matches_noop_when_no_requested(monkeypatch):
    """Caller didn't pin a backend → no check to enforce."""
    from browser_skill.mode_b_client import ModeBClient
    mb = ModeBClient()

    def _boom(self):
        raise AssertionError("get_backend_info should not be called when "
                             "no backend was requested")

    monkeypatch.setattr(ModeBClient, "get_backend_info", _boom)
    mb.assert_backend_matches("")  # no raise


def test_auto_client_factory_invokes_backend_check(monkeypatch):
    """``auto_client(backend="rdp")`` against a Mode-B daemon serving
    a different backend should fail loudly via auto_client()."""
    from browser_skill.errors import DaemonBackendMismatch
    from browser_skill import mode_b_client
    from browser_skill.mode_b_client import ModeBClient, auto_client

    monkeypatch.setattr(ModeBClient, "is_alive", lambda self: True)
    monkeypatch.setattr(ModeBClient, "get_backend_info",
                        lambda self: {"backend": "autoconnect"})
    monkeypatch.delenv("BS_DAEMON_MODE", raising=False)
    monkeypatch.delenv("BS_DAEMON_BACKEND", raising=False)
    monkeypatch.delenv("BD_BACKEND", raising=False)
    with pytest.raises(DaemonBackendMismatch):
        auto_client(backend="rdp")


def test_get_backend_info_returns_none_when_daemon_missing(monkeypatch):
    """``browser-daemon backend-info`` failure modes → ``None``, never
    raise (the assert_backend_matches contract relies on this)."""
    from browser_skill.mode_b_client import ModeBClient

    def _no_daemon(*a, **kw):
        raise FileNotFoundError("browser-daemon")

    monkeypatch.setattr(subprocess, "run", _no_daemon)
    assert ModeBClient().get_backend_info() is None
