"""Markdown helpers for frontmatter + section editing.

Lightweight: split a file into (frontmatter_dict, body_str). When appending
into a named ``## Section``, create the section at the bottom if it doesn't
exist; otherwise append to the end of that section preserving order.
"""
from __future__ import annotations

from pathlib import Path

from . import _yaml


def parse_doc(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            fm_raw = text[4:end]
            body = text[end + 4:].lstrip("\n")
            try:
                fm = _yaml.loads(fm_raw)
            except Exception:
                fm = {}
            return fm, body
    return {}, text


def render_doc(frontmatter: dict, body: str) -> str:
    body = body.lstrip("\n")
    if not body.endswith("\n"):
        body += "\n"
    if frontmatter:
        return "---\n" + _yaml.dumps(frontmatter) + "---\n\n" + body
    return body


def append_to_section(body: str, section: str, line: str) -> str:
    """Append ``line`` to ``## {section}`` block, creating it if absent.

    Sections are matched case-insensitively against the level-2 heading.
    Body remains markdown — no parser, just header line manipulation.
    """
    heading = f"## {section}"
    lines = body.splitlines()
    out: list[str] = []
    appended = False
    next_heading_idx: int | None = None

    # First pass: find target heading + the next heading after it.
    target_idx: int | None = None
    for idx, ln in enumerate(lines):
        if ln.strip().lower() == heading.lower():
            target_idx = idx
            break
    if target_idx is not None:
        for idx in range(target_idx + 1, len(lines)):
            if lines[idx].startswith("## "):
                next_heading_idx = idx
                break
    # Decide where to insert.
    if target_idx is None:
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.append(heading)
        out.append("")
        out.append(line)
        appended = True
    else:
        end = next_heading_idx if next_heading_idx is not None else len(lines)
        # Strip trailing blanks inside the target block before appending.
        section_block = list(lines[target_idx:end])
        while section_block and not section_block[-1].strip():
            section_block.pop()
        section_block.append(line)
        out = list(lines[:target_idx]) + section_block + [""] + list(lines[end:])
        appended = True
    if not appended:
        out = list(lines) + [line]
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    return text


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def find_matching_lines(body: str, pattern: str) -> list[tuple[int, str]]:
    """Return ``(line_no, content)`` pairs for bullet lines whose content
    contains ``pattern`` (case-insensitive substring). Headings and
    blank lines are skipped — we only match list items so we don't
    accidentally delete the user's prose.
    """
    import re

    rx = re.compile(re.escape(pattern), re.IGNORECASE)
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(body.splitlines()):
        stripped = ln.lstrip()
        if not stripped.startswith(("- ", "* ", "+ ")):
            continue
        item = stripped[2:].rstrip()
        if rx.search(item):
            out.append((i, ln))
    return out


def remove_lines(body: str, line_nos: set[int]) -> str:
    """Return ``body`` with the listed line numbers removed."""
    kept = [ln for i, ln in enumerate(body.splitlines()) if i not in line_nos]
    text = "\n".join(kept)
    if not text.endswith("\n"):
        text += "\n"
    return text
