"""Tests for workspace build/reset logic."""
from __future__ import annotations

from pathlib import Path

from workspace import SKILL_SRC, build_workspace, reset_workspace


def test_build_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    build_workspace(root)

    skill_dir = root / "skill"
    # SKILL.md is a symlink to the real file
    assert (skill_dir / "SKILL.md").is_symlink()
    assert (skill_dir / "SKILL.md").resolve() == (SKILL_SRC / "SKILL.md").resolve()

    # tasks.md is a symlink
    assert (skill_dir / "tasks.md").is_symlink()
    assert (skill_dir / "tasks.md").resolve() == (SKILL_SRC / "tasks.md").resolve()

    # memory.md is a real file (copy), not symlink
    mem = skill_dir / "memory.md"
    assert mem.exists()
    assert not mem.is_symlink()
    assert mem.read_text() == (SKILL_SRC / "memory.md").read_text()

    # .browserwright/site-skills/ exists and is empty
    ss = root / ".browserwright" / "site-skills"
    assert ss.is_dir()
    assert list(ss.iterdir()) == []


def test_reset_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    build_workspace(root)

    # Mutate memory.md
    mem = root / "skill" / "memory.md"
    mem.write_text("MUTATED")

    # Drop a file in .browserwright
    junk = root / ".browserwright" / "site-skills" / "junk.txt"
    junk.write_text("junk")

    # Reset
    reset_workspace(root)

    # memory.md restored
    assert mem.read_text() == (SKILL_SRC / "memory.md").read_text()
    assert not mem.is_symlink()

    # junk file gone
    assert not junk.exists()
    assert list((root / ".browserwright" / "site-skills").iterdir()) == []
