"""Shared fixtures: temp BS_HOME, etc."""
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _disable_production_hardening_by_default(monkeypatch):
    """v0.5.0 (F-4b): production hardening is on by default for real
    users but off for tests — the suite drives CLI entry points and
    inline runs through mocked transports, so the port-9222 listener
    and daemon-url checks would tip false-positive on dev boxes where
    Chrome is already running. Tests that *want* to exercise hardening
    (``tests/test_p0_hardening.py``) flip ``BS_PRODUCTION_HARDENING``
    back on explicitly via ``monkeypatch.setenv``.
    """
    monkeypatch.setenv("BS_PRODUCTION_HARDENING", "0")


@pytest.fixture
def tmp_bs_home(tmp_path, monkeypatch):
    """Reset the global_memory singleton to a tmp_path-rooted home.

    We monkeypatch ``BS_HOME``, ``cd`` into ``tmp_path`` (so site_skills_root()
    doesn't pick up the repo's own bundled site-skills), and null out the
    singleton so the next ``global_memory()`` call rebuilds with the new path.
    """
    home = tmp_path / "bs-home"
    home.mkdir()
    monkeypatch.setenv("BS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    # Reset module-level singletons so they re-read $BS_HOME.
    from browser_skill.memory import global_mem
    monkeypatch.setattr(global_mem, "_singleton", None)
    yield home


@pytest.fixture
def fresh_modules():
    """Compatibility no-op: kept for callers but no longer reloads modules
    (which would re-bind exception classes and break ``pytest.raises``).
    """
    import browser_skill
    yield browser_skill
