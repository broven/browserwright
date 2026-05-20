"""Shared artifact dumping for scorers."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "_artifacts"
WORKSPACE_ROOT = Path(__file__).resolve().parents[1] / "_workspace"


def dump(case_dir: str, context: dict, reason: str) -> None:
    """Dump debugging artifacts on scorer failure."""
    out = ARTIFACTS_DIR / case_dir
    out.mkdir(parents=True, exist_ok=True)

    meta = context.get("providerResponse", {}).get("metadata", {})
    trace = meta.get("trace", [])
    (out / "agent_trace.json").write_text(
        json.dumps(trace, indent=2, default=str), encoding="utf-8"
    )
    (out / "failure_reason.txt").write_text(reason, encoding="utf-8")

    # Snapshot daemon log if available
    daemon_log = ARTIFACTS_DIR / "daemon.log"
    if daemon_log.exists():
        shutil.copy2(daemon_log, out / "daemon.log")

    # Snapshot memory.md
    mem = WORKSPACE_ROOT / "skill" / "memory.md"
    if mem.exists():
        shutil.copy2(mem, out / "memory.md")

    # Snapshot site-skills tree
    ss = WORKSPACE_ROOT / ".browser-skill" / "site-skills"
    if ss.exists():
        tree = []
        for p in ss.rglob("*"):
            tree.append(f"{'D' if p.is_dir() else 'F'} {p.relative_to(ss)}")
        (out / "site_skills_tree.txt").write_text(
            "\n".join(sorted(tree)), encoding="utf-8"
        )
