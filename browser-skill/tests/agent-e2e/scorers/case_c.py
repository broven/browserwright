"""Scorer for Case C: solidify task (proactive ask = required).

Checks:
  1. [trace] Agent recognized the solidify trigger (ask or stated intent)
  2. [fs] Task file created under $BS_HOME/site-skills/<host>/tasks/
  3. [content] Valid Python file with scrape/fetch logic (parseable by ast)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scorers._artifacts import WORKSPACE_ROOT, dump as _dump_artifacts


def _find_task_files(workspace: Path) -> list[Path]:
    """Find any .py files under site-skills that look like task files."""
    ss = workspace / ".browser-skill" / "site-skills"
    if not ss.exists():
        return []
    candidates = []
    for py in ss.rglob("tasks/*.py"):
        candidates.append(py)
    return candidates


def get_assert(output: str, context: dict) -> dict:
    """promptfoo assertion entry point."""
    components = []
    meta = context.get("providerResponse", {}).get("metadata", {})
    trace = meta.get("trace", [])

    # --- Component 1: agent recognized this as a solidify-worthy task ---
    # In bypassPermissions mode the agent cannot pause to ask, so we check
    # that it at least recognized the recurring/solidify pattern by:
    # (a) mock-user flow caught a question, OR
    # (b) trace text shows intent to solidify (mentioned saving/task + the
    #     recurring nature of the request).
    asked_via_mock = meta.get("asked_user", False)
    questions = meta.get("user_questions", [])
    asked_save_mock = False
    if asked_via_mock:
        for q in questions:
            q_lower = q.lower()
            if any(kw in q_lower for kw in ["save", "task", "reusable", "固化", "保存"]):
                asked_save_mock = True
                break

    # Check trace for solidify intent (save/task + recurring indicators)
    solidify_intent = False
    solidify_kws = ["save", "task", "reusable", "solidif", "固化", "保存",
                    "task file", "site-skills"]
    recurring_kws = ["recurring", "every", "daily", "morning", "每天", "每早",
                     "定时", "scheduled", "automat"]
    for entry in trace:
        if entry.get("type") == "AssistantMessage":
            for block in entry.get("content", []):
                if block.get("type") == "text":
                    text = block["text"].lower()
                    has_solidify = any(kw in text for kw in solidify_kws)
                    has_recurring = any(kw in text for kw in recurring_kws)
                    if has_solidify and has_recurring:
                        solidify_intent = True
                        break

    intent_ok = asked_save_mock or solidify_intent
    components.append({
        "pass": intent_ok,
        "score": 1.0 if asked_save_mock else (0.8 if solidify_intent else 0.0),
        "reason": (
            f"solidify_intent: "
            f"{'asked (mock-user)' if asked_save_mock else ''}"
            f"{'recognized (trace)' if solidify_intent and not asked_save_mock else ''}"
            f"{'NOT recognized (FAIL)' if not intent_ok else ''}"
        ).strip(),
    })

    # --- Component 2: task file exists in correct location ---
    task_files = _find_task_files(WORKSPACE_ROOT)
    has_task = len(task_files) > 0
    # Check it's under a ycombinator/HN-related directory
    hn_task = any(
        "ycombinator" in str(f).lower() or "hn" in str(f).lower() or "hacker" in str(f).lower()
        for f in task_files
    )
    file_ok = has_task  # accept any task file under site-skills
    file_reason = (
        f"found {len(task_files)} task file(s)"
        + (f" (HN-related: {hn_task})" if has_task else " (none)")
    )
    components.append({
        "pass": file_ok,
        "score": 1.0 if file_ok and hn_task else (0.5 if file_ok else 0.0),
        "reason": f"task_file: {file_reason}",
    })

    # --- Component 3: content is valid Python with scrape/fetch logic ---
    content_ok = False
    content_reason = "no task file to check"
    if task_files:
        tf = task_files[0]
        try:
            src = tf.read_text(encoding="utf-8")
            ast.parse(src)
            has_scrape = any(kw in src for kw in [
                "browser_skill", "new_tab", "page_info", "http_get",
                "click_at_xy", "wait_for_load", "capture_screenshot",
                "js(", "goto_url", "requests", "urllib", "fetch",
                "BeautifulSoup", "re.findall", "html",
            ])
            if has_scrape:
                content_ok = True
                content_reason = f"valid Python with scrape logic ({tf.name})"
            else:
                content_reason = f"valid Python but no scrape/fetch logic ({tf.name})"
        except SyntaxError as e:
            content_reason = f"Python syntax error: {e}"
    components.append({
        "pass": content_ok,
        "score": 1.0 if content_ok else 0.0,
        "reason": f"content: {content_reason}",
    })

    # Pass if at least 2 of 3 components pass (LLM non-determinism means
    # the agent sometimes creates the file without asking, or asks but
    # runs out of turns before writing the file).
    overall = sum(1 for c in components if c["pass"]) >= 2
    if not overall:
        _dump_artifacts("case_c", context, "; ".join(c["reason"] for c in components))

    return {
        "pass": overall,
        "score": sum(c["score"] for c in components) / len(components),
        "reason": "; ".join(c["reason"] for c in components),
        "componentResults": components,
    }
