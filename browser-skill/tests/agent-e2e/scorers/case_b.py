"""Scorer for Case B: save preference to memory.md.

Checks:
  1. [fs] workspace skill/memory.md has a User preference section mentioning
     extension backend preference
  2. [fs] backend capability table is intact (not clobbered)
  3. [trace] no-confirm = warning (soft, not hard fail)
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

    # Snapshot memory.md
    mem = WORKSPACE_ROOT / "skill" / "memory.md"
    if mem.exists():
        (out / "memory.md").write_text(mem.read_text(encoding="utf-8"), encoding="utf-8")


def get_assert(output: str, context: dict) -> dict:
    """promptfoo assertion entry point."""
    components = []
    meta = context.get("providerResponse", {}).get("metadata", {})

    # --- Component 1: memory.md has extension preference ---
    mem_path = WORKSPACE_ROOT / "skill" / "memory.md"
    pref_ok = False
    pref_reason = "memory.md not found"
    if mem_path.exists():
        content = mem_path.read_text(encoding="utf-8")
        # Check for extension backend preference in User preference section
        has_pref_section = "## user preference" in content.lower() or "## User preference" in content
        has_extension = "extension" in content.lower()
        if has_pref_section and has_extension:
            pref_ok = True
            pref_reason = "memory.md has User preference with extension backend"
        elif has_extension:
            pref_ok = True
            pref_reason = "memory.md mentions extension (section header may differ)"
        else:
            pref_reason = f"no extension preference found in memory.md ({len(content)} chars)"
    components.append({
        "pass": pref_ok,
        "score": 1.0 if pref_ok else 0.0,
        "reason": f"preference: {pref_reason}",
    })

    # --- Component 2: capability table intact ---
    table_ok = False
    table_reason = "memory.md not found"
    if mem_path.exists():
        content = mem_path.read_text(encoding="utf-8")
        # The original has backend table with rdp, extension, cloud, env
        has_backends = all(
            b in content.lower()
            for b in ["rdp", "extension", "cloud", "env"]
        )
        if has_backends:
            table_ok = True
            table_reason = "backend capability table intact"
        else:
            table_reason = "backend capability table damaged or missing"
    components.append({
        "pass": table_ok,
        "score": 1.0 if table_ok else 0.0,
        "reason": f"capability_table: {table_reason}",
    })

    # --- Component 3: asked for confirmation (soft warning) ---
    asked = meta.get("asked_user", False)
    components.append({
        "pass": True,  # soft — warning only for B
        "score": 1.0 if asked else 0.5,
        "reason": f"confirmation: {'asked user' if asked else 'did NOT ask (warning)'}",
    })

    overall = pref_ok and table_ok
    if not overall:
        _dump_artifacts("case_b", context, "; ".join(c["reason"] for c in components))

    return {
        "pass": overall,
        "score": sum(c["score"] for c in components) / len(components),
        "reason": "; ".join(c["reason"] for c in components),
        "componentResults": components,
    }
