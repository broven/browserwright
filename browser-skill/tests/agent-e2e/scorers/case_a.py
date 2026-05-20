"""Scorer for Case A: connect + open + summarize.

Independent verification — does NOT trust the sub-agent's self-reported output.
Checks:
  1. [trace] Sub-agent used browser-skill and got a successful response mentioning
     example.com (from page_info or similar)
  2. [trace] Failed Bash count is reasonable (<= 2 = good, > 2 = wandered)
  3. The LLM rubric in YAML handles output quality separately.
"""
from __future__ import annotations

import json
from pathlib import Path

ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "_artifacts"


def _dump_artifacts(case_dir: str, context: dict, reason: str) -> None:
    """Dump debugging artifacts on failure."""
    out = ARTIFACTS_DIR / case_dir
    out.mkdir(parents=True, exist_ok=True)

    meta = context.get("providerResponse", {}).get("metadata", {})
    trace = meta.get("trace", [])
    (out / "agent_trace.json").write_text(
        json.dumps(trace, indent=2, default=str), encoding="utf-8"
    )
    (out / "failure_reason.txt").write_text(reason, encoding="utf-8")

    daemon_log = ARTIFACTS_DIR / "daemon.log"
    if daemon_log.exists():
        import shutil
        shutil.copy2(daemon_log, out / "daemon.log")


def _trace_mentions_example_com(trace: list[dict]) -> bool:
    """Check if any tool result in the trace mentions example.com."""
    for entry in trace:
        content = entry.get("content", "")
        if isinstance(content, str) and "example.com" in content.lower():
            return True
        if isinstance(content, list):
            for block in content:
                text = block.get("text", "") or block.get("input", "")
                if isinstance(text, str) and "example.com" in text.lower():
                    return True
                if isinstance(text, dict):
                    text_str = json.dumps(text)
                    if "example.com" in text_str.lower():
                        return True
    return False


def _trace_used_browser_skill(trace: list[dict]) -> bool:
    """Check if the agent used browser-skill Bash commands."""
    for entry in trace:
        if entry.get("type") == "AssistantMessage":
            for block in entry.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    cmd = block.get("input", {})
                    if isinstance(cmd, dict):
                        cmd = cmd.get("command", "")
                    if "browser-skill" in str(cmd):
                        return True
    return False


def get_assert(output: str, context: dict) -> dict:
    """promptfoo assertion entry point."""
    components = []
    meta = context.get("providerResponse", {}).get("metadata", {})
    trace = meta.get("trace", [])

    # --- Component 1: browser-skill usage + example.com in trace ---
    used_bs = _trace_used_browser_skill(trace)
    saw_example = _trace_mentions_example_com(trace)
    daemon_ok = used_bs and saw_example

    if not used_bs:
        daemon_reason = "agent did not use browser-skill"
    elif not saw_example:
        daemon_reason = "trace does not mention example.com"
    else:
        daemon_reason = "agent used browser-skill and navigated to example.com"

    components.append({
        "pass": daemon_ok,
        "score": 1.0 if daemon_ok else 0.0,
        "reason": f"browser_usage: {daemon_reason}",
    })

    # --- Component 2: wandering check (soft) ---
    failed_bash = meta.get("failed_bash", 0)
    wandered = failed_bash > 2
    components.append({
        "pass": True,  # soft — warning only
        "score": 0.5 if wandered else 1.0,
        "reason": f"failed_bash={failed_bash}" + (" (wandered)" if wandered else " (clean)"),
    })

    overall = daemon_ok
    if not overall:
        _dump_artifacts("case_a", context, daemon_reason)

    return {
        "pass": overall,
        "score": sum(c["score"] for c in components) / len(components),
        "reason": "; ".join(c["reason"] for c in components),
        "componentResults": components,
    }
