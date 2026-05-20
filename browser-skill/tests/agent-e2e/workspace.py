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

import shutil
import subprocess
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
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(SKILL_SRC / name)

    for name in _COPIES:
        shutil.copy2(SKILL_SRC / name, skill_dir / name)

    ss = root / ".browser-skill" / "site-skills"
    ss.mkdir(parents=True, exist_ok=True)


def reset_workspace(root: Path) -> None:
    """Reset mutable parts of the workspace without removing the root dir.

    This avoids issues with sub-agent processes that may still hold the
    workspace as their CWD.
    """
    skill_dir = root / "skill"
    bs_dir = root / ".browser-skill"

    # Restore memory.md (the only mutable copy)
    mem = skill_dir / "memory.md"
    if skill_dir.exists():
        if mem.exists():
            mem.unlink()
        shutil.copy2(SKILL_SRC / "memory.md", mem)

    # Reset .browser-skill/site-skills/ (wipe and recreate)
    ss = bs_dir / "site-skills"
    if ss.exists():
        shutil.rmtree(ss, ignore_errors=True)
    ss.mkdir(parents=True, exist_ok=True)

    # Ensure symlinks are intact
    for name in _SYMLINKS:
        dst = skill_dir / name
        if not dst.is_symlink():
            if dst.exists():
                dst.unlink()
            dst.symlink_to(SKILL_SRC / name)

    # If workspace doesn't exist at all, full build
    if not skill_dir.exists():
        build_workspace(root)
