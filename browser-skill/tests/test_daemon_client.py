"""DaemonClient: env override + retry + active-tab + doctor."""
import os
import subprocess
from unittest.mock import patch

import pytest

from browser_skill.daemon_client import DaemonClient
from browser_skill.errors import DaemonUnavailable


def test_env_ws_overrides_subprocess(monkeypatch):
    monkeypatch.setenv("BS_CDP_WS", "ws://example/devtools/browser/foo")
    client = DaemonClient()
    assert client.resolve_ws_url() == "ws://example/devtools/browser/foo"


def test_subprocess_resolve(monkeypatch):
    monkeypatch.delenv("BS_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BS_DAEMON_URL_CMD", raising=False)
    client = DaemonClient(url_cmd="echo ws://from-subprocess")
    assert client.resolve_ws_url() == "ws://from-subprocess"
    # Cached on second call (would otherwise have to re-spawn).
    assert client.resolve_ws_url() == "ws://from-subprocess"


def test_subprocess_failure_raises(monkeypatch):
    monkeypatch.delenv("BS_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BS_DAEMON_URL_CMD", raising=False)
    # ``false`` returns nonzero; should raise after a single retry.
    client = DaemonClient(url_cmd="false")
    with pytest.raises(DaemonUnavailable):
        client.resolve_ws_url()


def test_missing_binary(monkeypatch):
    monkeypatch.delenv("BS_CDP_WS", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BS_DAEMON_URL_CMD", raising=False)
    client = DaemonClient(url_cmd="/nonexistent/bin/browser-daemon url")
    with pytest.raises(DaemonUnavailable):
        client.resolve_ws_url()


def test_active_tab_returns_none_on_missing_binary(monkeypatch):
    client = DaemonClient(daemon_bin="/nonexistent/bin/browser-daemon")
    assert client.active_tab() is None


def test_doctor_synthesises_failure(monkeypatch):
    client = DaemonClient(daemon_bin="/nonexistent/bin/browser-daemon")
    info = client.doctor()
    assert info["schema_version"] == 1
    assert info["skill_synthetic"] is True
