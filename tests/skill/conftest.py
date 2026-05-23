"""Shared fixtures: temp BS_HOME, etc."""
import os
from pathlib import Path

import pytest


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
    from browserwright.memory import global_mem
    monkeypatch.setattr(global_mem, "_singleton", None)
    yield home


@pytest.fixture
def fresh_modules():
    """Compatibility no-op: kept for callers but no longer reloads modules
    (which would re-bind exception classes and break ``pytest.raises``).
    """
    import browserwright
    yield browserwright
