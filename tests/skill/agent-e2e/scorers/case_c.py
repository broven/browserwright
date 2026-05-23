"""Scorer for Case C: scrape the top 5 Hacker News headlines (one-shot).

Independent verification — does NOT trust the sub-agent's self-reported output.
Checks:
  1. [trace] Agent drove browserwright (Bash) — it actually used the tool.
  2. [trace] A tool RESULT shows it reached Hacker News (ycombinator content),
     so the headlines came from the live page, not the model's memory.
  3. [output] The final answer lists >= 5 distinct headline-like lines.

The llm-rubric in the YAML judges whether those lines read like real HN titles.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scorers._artifacts import dump as _dump_artifacts


def _iter_text_blocks(trace: list[dict]):
    """Yield every text/result string in the trace (assistant text + tool results)."""
    for entry in trace:
        content = entry.get("content", "")
        blocks = content if isinstance(content, list) else [content]
        for b in blocks:
            if isinstance(b, str):
                yield b
            elif isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    yield b["text"]
                if isinstance(b.get("content"), str):
                    yield b["content"]
                if isinstance(b.get("content"), list):
                    for inner in b["content"]:
                        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                            yield inner["text"]
                inp = b.get("input")
                if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                    yield inp["command"]


def _used_browserwright(trace: list[dict]) -> bool:
    for entry in trace:
        content = entry.get("content")
        for block in (content if isinstance(content, list) else []):
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and block.get("name") == "Bash":
                if "browserwright" in (block.get("input") or {}).get("command", ""):
                    return True
    return False


def _reached_hn(trace: list[dict]) -> bool:
    """A tool result / page content references Hacker News, so the data is live."""
    markers = ("news.ycombinator.com", "hacker news", "ycombinator",
               "y combinator", "news.yc")
    for text in _iter_text_blocks(trace):
        low = text.lower()
        if any(m in low for m in markers):
            return True
    return False


def _count_headlines(output: str) -> int:
    """Count distinct list-like lines in the final answer (numbered or bulleted)."""
    seen: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        m = re.match(r"^(?:\d+[.)]\s+|[-*•]\s+)(.*\S.*)$", line)
        if not m:
            continue
        item = m.group(1).strip().strip("*").strip()
        if len(item) >= 8:  # ignore trivially short fragments
            seen.add(item.lower())
    return len(seen)


def get_assert(output: str, context: dict) -> dict:
    meta = context.get("providerResponse", {}).get("metadata", {})
    trace = meta.get("trace", [])

    components = []

    used = _used_browserwright(trace)
    components.append({
        "pass": used,
        "score": 1.0 if used else 0.0,
        "reason": f"used_browserwright: {used}",
    })

    reached = _reached_hn(trace)
    components.append({
        "pass": reached,
        "score": 1.0 if reached else 0.0,
        "reason": f"reached_hacker_news (live data in trace): {reached}",
    })

    n = _count_headlines(output)
    listed = n >= 5
    components.append({
        "pass": listed,
        "score": 1.0 if listed else (0.5 if n >= 3 else 0.0),
        "reason": f"listed_headlines: {n} distinct list items (need >= 5)",
    })

    # Require: tool actually used + reached HN, and at least 5 headlines listed.
    overall = used and reached and listed
    if not overall:
        _dump_artifacts("case_c", context, "; ".join(c["reason"] for c in components))

    return {
        "pass": overall,
        "score": sum(c["score"] for c in components) / len(components),
        "reason": "; ".join(c["reason"] for c in components),
        "componentResults": components,
    }
