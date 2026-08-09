"""Text budgets and the two truncators that enforce them.

Zero-dependency on purpose: both the executor transport (`_executor/protocol.py`,
`_executor/process.py`) and the content producers (`repl/snapshot.py`,
`repl/markdown.py`, `cli.py`) import from here, and the producers must not drag
Playwright into the transport's import graph.

Two truncators, because there are two genuinely different jobs
--------------------------------------------------------------
A producer promises **line integrity**: cutting an aria snapshot mid-line yields
`[ref=e2` where the real token was `[ref=e291]` — a ref that is not merely
corrupt but *well-formed for a different live element*, so acting on it fails
silently. Same reason a Markdown cut mid-line yields a broken
`[text](http://…` link. For a producer, overflowing a soft budget by one line is
strictly better than emitting a partial ref, so `truncate_lines` keeps the first
line whole even when it alone exceeds the budget.

The transport promises a **bound**. Its whole reason to exist is that a runaway
`print('x' * 5_000_000)` must not ship megabytes to the agent — and that payload
is ONE line with no newlines, so the producer's "keep the first line whole"
fallback would return 5,000,014 characters for a 10,000 budget. A soft
truncator cannot serve as a hard cap. `truncate_hard` therefore drops whole
lines wherever it can and cuts mid-line only when a single line still overflows,
where it says so in the marker so a severed ref is at least VISIBLE rather than
silent.

The invariant this file exists to keep (ADR-0007 §5): no layer may truncate a
payload at a raw offset once a layer above it has promised line integrity —
except a hard bound, which must, and must announce it.
"""
from __future__ import annotations

# The transport's hard bound on any single text channel of an executor response.
# Mirrors playwriter's ~10000-char truncation. This is the ceiling every other
# budget in the tree is derived from — see PRODUCER_BUDGET.
MAX_TEXT_CHARS = 10000

# What a single content producer (`snapshot()`, `read_markdown()`) may return by
# default. Deliberately UNDER MAX_TEXT_CHARS rather than equal to it: a heredoc
# normally prints other things alongside the payload, and the transport bound
# applies to the WHOLE console blob, not to one producer's output. The headroom
# is what keeps a producer's line-integrity promise from being re-cut downstream.
PRODUCER_BUDGET = 8000

TRUNC_MARKER = "… [truncated]"
# A distinct marker for the last-resort mid-line cut, so a caller (and the agent
# reading the output) can tell "you are missing whole lines" apart from "the last
# line you can see is itself incomplete, do not trust a token at its end".
MID_LINE_MARKER = "… [truncated mid-line]"


def _keep_whole_lines(text: str, budget: int) -> str | None:
    """Longest whole-line prefix of ``text`` fitting ``budget``, else None.

    None means not even the first line fits — the case the two callers below
    resolve in opposite directions.
    """
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for ln in lines:
        # +1 for the newline joining this line to the previous body.
        add = len(ln) + (1 if kept else 0)
        if used + add > budget:
            break
        kept.append(ln)
        used += add
    if not kept:
        return None
    return "\n".join(kept)


def truncate_lines(text: str, max_chars: int) -> str:
    """Cap ``text`` at ``max_chars`` on a LINE boundary, never mid-line.

    SOFT: if even the first line overflows the budget it is still emitted whole,
    because a partial `[ref=eN]` the agent might act on is worse than
    overflowing a soft cap. Producers want this. The transport must not use it —
    see :func:`truncate_hard`.

    Callers apply this only when ``len(text) > max_chars``; the marker is
    unconditional so a truncated payload always says so.
    """
    marker_cost = len(TRUNC_MARKER) + 1  # + the "\n" before the marker
    body = _keep_whole_lines(text, max_chars - marker_cost)
    if body is None:
        # Budget too small for even one line + marker: surface line one whole.
        lines = text.splitlines()
        body = lines[0] if lines else text
    return body + "\n" + TRUNC_MARKER


def truncate_hard(text: str, budget: int) -> str:
    """Cap ``text`` at ``budget`` characters — a bound that is never exceeded.

    Prefers whole lines exactly like :func:`truncate_lines`, so a payload that
    arrives with intact `[ref=eN]` tokens keeps them intact. Falls back to a
    mid-line cut ONLY when a single line still overflows the budget, which is
    the runaway-output case the bound exists for; that fallback is marked with
    ``MID_LINE_MARKER`` so the incomplete tail is never mistaken for a whole one.

    Guarantees ``len(result) <= budget`` for any ``budget >= 0``.
    """
    if len(text) <= budget:
        return text
    marker_cost = len(TRUNC_MARKER) + 1
    if budget > marker_cost:
        body = _keep_whole_lines(text, budget - marker_cost)
        if body is not None:
            return body + "\n" + TRUNC_MARKER
    # A single line overflows the budget: cut it, and SAY that we cut it.
    mid_cost = len(MID_LINE_MARKER) + 1
    if budget > mid_cost:
        return text[: budget - mid_cost] + "\n" + MID_LINE_MARKER
    # Budget too small to even carry a marker — the bound still wins.
    return text[:budget]
