"""Build and reset the isolated workspace for agent-e2e tests.

The workspace layout mirrors what a real agent sees:
  _workspace/
    skill/
      SKILL.md      # symlink -> real skill doc (read-only)
      tasks.md      # symlink -> real skill doc (read-only)
      memory.md     # copy (writable — Case B writes preferences here)
    .browser-skill/ # BS_HOME points here
      site-skills/  # empty initially; Case C/D write here
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# Locate the real skill/ directory (repo root / skill/).
SKILL_SRC = (Path(__file__).resolve().parents[3] / "skill")

_SYMLINKS = ["SKILL.md", "tasks.md"]
_COPIES = ["memory.md"]


def build_workspace(root: Path) -> None:
    """Create a fresh workspace at *root*."""
    skill_dir = root / "skill"
    skill_dir.mkdir(parents=True, exist_ok=True)

    for name in _SYMLINKS:
        dst = skill_dir / name
        dst.symlink_to(SKILL_SRC / name)

    for name in _COPIES:
        shutil.copy2(SKILL_SRC / name, skill_dir / name)

    ss = root / ".browser-skill" / "site-skills"
    ss.mkdir(parents=True, exist_ok=True)


def reset_workspace(root: Path) -> None:
    """Tear down and rebuild *root* to pristine state."""
    if root.exists():
        shutil.rmtree(root)
    build_workspace(root)
