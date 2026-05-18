"""Minimal extraction from REPL history (spec §B.4.2).

The goal isn't perfection — it's "have *something* to fill ``run()`` with so
the agent reviews instead of hand-typing from scratch." See §B.4.2 for the
algorithm and §11 for known limitations.
"""
from __future__ import annotations

import re
from typing import Any


# Probe-style calls to *strip* (pure observation, no state change).
_EXPLORE_PATTERNS = [
    re.compile(r"^\s*(?:capture_screenshot|page_info|list_tabs|current_tab|current_page|drain_events)\b"),
    re.compile(r"^\s*print\(\s*(?:page_info|current_tab|current_page|list_tabs)\s*\("),
]

# Lines we always keep even if they look "exploratory".
_KEEP_PATTERNS = [
    re.compile(r"^\s*(?:goto_url|new_tab|switch_tab|click_at_xy|type_text|press_key|fill_input|scroll|js|cdp|wait_for_load|wait_for_element|wait_for_network_idle|http_get|upload_file)\b"),
    re.compile(r"^\s*[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*"),  # assignments
]


def _is_exploratory(line: str) -> bool:
    if any(p.match(line) for p in _KEEP_PATTERNS):
        return False
    return any(p.match(line) for p in _EXPLORE_PATTERNS)


def _strip_blank(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _flatten_history(history: list[dict], *, max_entries: int = 50) -> list[str]:
    """Take the last ``max_entries`` success-only history entries → flat list
    of code lines (split on newline)."""
    rev = [h for h in reversed(history) if h.get("ok") and h.get("exception") is None]
    rev = rev[:max_entries]
    rev.reverse()
    out: list[str] = []
    for h in rev:
        code = h.get("code", "").strip()
        if not code:
            continue
        for ln in code.splitlines():
            if ln.strip():
                out.append(ln)
    return out


def _dedupe_clicks(lines: list[str]) -> list[str]:
    """spec §B.4.2 step 4: two consecutive ``click_at_xy`` calls with different
    coordinates → keep the second only."""
    out: list[str] = []
    i = 0
    click_rx = re.compile(r"^\s*click_at_xy\(")
    while i < len(lines):
        if (i + 1 < len(lines)
                and click_rx.match(lines[i]) and click_rx.match(lines[i + 1])):
            # skip the first (failed) click
            i += 1
        out.append(lines[i])
        i += 1
    return out


# ---- parameter extraction ---------------------------------------------

_ASSIGN_RX = re.compile(r"^(?P<indent>\s*)(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?P<rhs>[\"'].+[\"'])\s*$")
_URL_RX = re.compile(r"https?://[^\s'\"]+")


def _looks_like_param(name: str, value: str) -> bool:
    # Heuristic: short string literals on the RHS of a top-level assignment
    # are candidate parameters. URLs and obvious flags too.
    if name.startswith("_"):
        return False
    if name in {"i", "j", "k", "n", "x", "y"}:
        return False
    return True


def _stringify(rhs: str) -> str:
    # Strip outer quotes — value will be inserted into args dict.
    if (rhs.startswith("'") and rhs.endswith("'")) or (rhs.startswith('"') and rhs.endswith('"')):
        return rhs[1:-1]
    return rhs


def derive_params(lines: list[str]) -> tuple[list[str], dict[str, dict]]:
    """Rewrite simple ``foo = "bar"`` assignments at the top into ``args["foo"]``
    references inside the run body. Returns ``(rewritten_lines, args_schema)``.
    """
    schema: dict[str, dict] = {}
    rewritten: list[str] = []
    seen: set[str] = set()
    rename: dict[str, str] = {}
    for ln in lines:
        m = _ASSIGN_RX.match(ln)
        if m and _looks_like_param(m["name"], m["rhs"]) and m["name"] not in seen:
            name = m["name"]
            value = _stringify(m["rhs"])
            schema[name] = {
                "type": "str",
                "required": False,
                "default": value,
                "desc": "",
            }
            rename[name] = f"args[{name!r}]"
            seen.add(name)
            # Replace the assignment line with a compact note so the
            # generated task keeps the original ordering intact.
            rewritten.append(f"{m['indent']}# parameterized: {name}")
            continue
        rewritten.append(ln)
    if not rename:
        return rewritten, schema
    # Substitute variable references throughout the body.
    word_rx = re.compile(r"\b(" + "|".join(re.escape(k) for k in rename) + r")\b")

    def _sub(m):
        return rename[m.group(1)]

    rewritten = [word_rx.sub(_sub, ln) for ln in rewritten]
    return rewritten, schema


# ---- public -----------------------------------------------------------


def extract_run_body(history: list[dict]) -> tuple[str, dict[str, dict]]:
    """Return ``(python_code_for_run_body, args_schema)``."""
    lines = _flatten_history(history)
    lines = [ln for ln in lines if not _is_exploratory(ln)]
    lines = _dedupe_clicks(lines)
    lines = _strip_blank(lines)
    if not lines:
        return "pass  # nothing extracted — agent should fill in run()\n", {}
    lines, schema = derive_params(lines)
    # Wrap final expression-ish line into ``return ...`` if not already.
    if not any(ln.strip().startswith("return ") for ln in lines):
        last = lines[-1]
        if "=" not in last and not last.strip().endswith(":"):
            # Reasonable bet that the last bare expression is the result.
            indent = len(last) - len(last.lstrip(" "))
            lines[-1] = " " * indent + f"return {last.lstrip()}"
    return "\n".join("    " + ln for ln in lines) + "\n", schema
