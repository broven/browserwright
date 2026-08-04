"""Shared fixtures.

We isolate every test from the developer's real environment:
- proxy env vars are cleared
- BD_* / BU_* env vars are cleared
- HOME is **not** redirected (the platforms.profile_paths() table reads $HOME)
  — individual tests that touch the profile scan use the `tmp_home` fixture.
"""
from __future__ import annotations


import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Pop everything daemon-relevant. Some tests re-set what they need."""
    for var in [
        "BD_BACKEND", "BD_CDP_WS", "BD_CDP_URL", "BD_CONFIG", "BD_NAME",
        "BD_TIMEOUT", "BD_CHROME_BINARY", "BD_SESSION_IDLE_PRUNE",
        "BU_CDP_WS", "BU_CDP_URL",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    """Redirect Path.home() to a temp dir. Used by tests that scan profiles."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads HOME on POSIX, USERPROFILE on Windows.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path
