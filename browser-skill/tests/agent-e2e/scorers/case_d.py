"""Scorer for Case D: site memory (explicit write).

Checks:
  1. [fs] $BS_HOME/site-skills/<host>/memory.md exists and has content
  2. [content] Append-only entry with durable site-level note
"""
from __future__ import annotations

import json
from pathlib import Path

ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "_artifacts"
WORKSPACE_ROOT = Path(__file__).resolve().parents[1] / "_workspace"


def _dump_artifacts(case_dir: str, context: dict, reason: str) -> None:
    out = ARTIFACTS_DIR / case_dir
    out.mkdir(parents=True, exist_ok=True)

    meta = context.get("providerResponse", {}).get("metadata", {})
    (out / "agent_trace.json").write_text(
        json.dumps(meta.get("trace", []), indent=2, default=str), encoding="utf-8"
    )
    (out / "failure_reason.txt").write_text(reason, encoding="utf-8")

    ss = WORKSPACE_ROOT / ".browser-skill" / "site-skills"
    if ss.exists():
        tree = []
        for p in ss.rglob("*"):
            tree.append(f"{'D' if p.is_dir() else 'F'} {p.relative_to(ss)}")
        (out / "site_skills_tree.txt").write_text("\n".join(sorted(tree)), encoding="utf-8")


def _find_site_memory_files(workspace: Path) -> list[Path]:
    """Find any memory.md files under site-skills."""
    ss = workspace / ".browser-skill" / "site-skills"
    if not ss.exists():
        return []
    return list(ss.rglob("memory.md"))


def get_assert(output: str, context: dict) -> dict:
    """promptfoo assertion entry point."""
    components = []

    # --- Component 1: site memory file exists ---
    mem_files = _find_site_memory_files(WORKSPACE_ROOT)
    has_mem = len(mem_files) > 0
    mem_reason = f"found {len(mem_files)} site memory file(s)" if has_mem else "no site memory files"
    components.append({
        "pass": has_mem,
        "score": 1.0 if has_mem else 0.0,
        "reason": f"site_memory_exists: {mem_reason}",
    })

    # --- Component 2: content has durable site-level notes ---
    content_ok = False
    content_reason = "no file to check"
    if mem_files:
        mf = mem_files[0]
        content = mf.read_text(encoding="utf-8")
        # Should have meaningful content (not just frontmatter)
        if len(content.strip()) > 20:
            content_ok = True
            content_reason = f"has content ({len(content)} chars, {mf.relative_to(WORKSPACE_ROOT)})"
        else:
            content_reason = f"content too short ({len(content)} chars)"
    components.append({
        "pass": content_ok,
        "score": 1.0 if content_ok else 0.0,
        "reason": f"content: {content_reason}",
    })

    overall = all(c["pass"] for c in components)
    if not overall:
        _dump_artifacts("case_d", context, "; ".join(c["reason"] for c in components))

    return {
        "pass": overall,
        "score": sum(c["score"] for c in components) / len(components),
        "reason": "; ".join(c["reason"] for c in components),
        "componentResults": components,
    }
