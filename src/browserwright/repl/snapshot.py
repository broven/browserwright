"""Phase C PR2: ``snapshot()`` — Playwright first-party AI aria snapshot.

The agent calls ``snapshot()`` in a heredoc to observe what is on the current
``page`` and to obtain **stable refs** it can act on, instead of taking a
screenshot or inventing CSS selectors. This is the SAME first-party snapshot
``@playwright/mcp`` uses: ``page.aria_snapshot(mode="ai")`` (Playwright 1.60
Python sync), which renders an accessibility tree where every node carries a
``[ref=eN]`` token.

Ref → locator contract
----------------------
Each ``[ref=eN]`` in the output resolves to a live element via Playwright's
``aria-ref=`` selector engine, scoped to the LAST snapshot taken on that page::

    snapshot()                                  # observe
    page.locator("aria-ref=e3").click()         # act on the e3 node
    page.locator("aria-ref=e5").fill("hello")   # act on the e5 node

The ref store lives on the page and is refreshed on each ``aria_snapshot``
call, so always re-``snapshot()`` after an action (observe → act → observe)
before reusing refs from a stale snapshot.

Why first-party (not a ported custom snapshot)
----------------------------------------------
Verified against Playwright 1.60 Python sync: ``Page.aria_snapshot(mode="ai")``
yields ``[ref=eN]`` refs and ``page.locator("aria-ref=eN")`` round-trips to a
clickable/fillable locator. There is no need to port playwriter's custom
aria-snapshot — the first-party AI mode is cleanly available here.
"""
from __future__ import annotations

from .playwright_handle import PlaywrightHandle


def make_snapshot(handle: PlaywrightHandle):
    """Build the per-heredoc ``snapshot()`` bound to this heredoc's lazy
    Playwright handle. Injected into the exec namespace by ``build_globals``.

    Triggering ``snapshot()`` resolves ``handle.page`` (lazily connecting the
    facade on first use, exactly like ``page``/``context``)."""

    def snapshot(*, interactive_only: bool = True,
                 max_chars: int | None = 6000) -> str:
        """Observe the current ``page`` as a first-party Playwright AI aria
        snapshot. Returns a compact accessibility tree where each node carries
        a ``[ref=eN]`` ref.

        Act on a ref via Playwright's ``aria-ref=`` selector engine on the SAME
        page (the ref store is refreshed by this call)::

            snapshot()
            page.locator("aria-ref=e3").click()
            page.locator("aria-ref=e5").fill("query")

        Re-``snapshot()`` after every action: refs are scoped to the most
        recent snapshot on the page, so a ref from a stale snapshot may no
        longer resolve.

        Args:
          interactive_only: when True (default), drop purely structural /
            decorative nodes that carry neither a ref nor an accessible name —
            keeps the output token-frugal and interaction-oriented. Set False
            for the full accessibility tree (headings, text, structure).
          max_chars: hard cap on the returned string (bounds token cost); the
            tail is replaced with a ``… [truncated]`` marker when exceeded.
            Pass None to disable the cap.

        Returns the snapshot as a string (one node per line, indented to show
        tree structure). Empty-page result is the bare root line.
        """
        page = handle.page
        snap = page.aria_snapshot(mode="ai")
        if interactive_only:
            snap = _filter_interactive(snap)
        if max_chars is not None and len(snap) > max_chars:
            snap = _truncate_lines(snap, max_chars)
        return snap

    return snapshot


_TRUNC_MARKER = "… [truncated]"


def _truncate_lines(snap: str, max_chars: int) -> str:
    """Cap ``snap`` at ``max_chars`` on a LINE boundary, never mid-line.

    Splitting on a raw byte offset can sever a ``[ref=eN]`` token — leaving a
    corrupt partial ref (``[ref=e1``) the agent might try to act on. So we drop
    whole lines from the tail until the kept body plus the ``… [truncated]``
    marker fits the budget. Every line that survives is therefore intact,
    including any ref it carries. If even the first line overflows the budget we
    still emit it whole (a ref is only useful intact) followed by the marker.
    """
    lines = snap.splitlines()
    kept: list[str] = []
    used = 0
    marker_cost = len(_TRUNC_MARKER) + 1  # + the "\n" before the marker
    for ln in lines:
        # +1 for the newline joining this line to the previous body.
        add = len(ln) + (1 if kept else 0)
        if used + add + marker_cost > max_chars:
            break
        kept.append(ln)
        used += add
    if not kept:
        # Budget too small for even one line + marker: still surface line one
        # whole — a partial ref is worse than overflowing a soft cap.
        kept = lines[:1]
    return "\n".join(kept) + "\n" + _TRUNC_MARKER


def _filter_interactive(snap: str) -> str:
    """Drop noise lines from an AI aria snapshot while preserving tree shape.

    Playwright's AI snapshot tags every node it considers actionable/named with
    a ``[ref=eN]``. We keep:
      - any line carrying a ``[ref=`` (an addressable node), and
      - the structural ancestor lines needed to keep indentation readable
        (a kept line's parents).

    A line with neither a ref nor a name (pure ``- generic`` wrappers, ``- text``
    leaves) is dropped UNLESS it is an ancestor of a kept line. This keeps the
    output interaction-oriented and token-frugal without breaking the tree.
    """
    lines = snap.splitlines()
    if not lines:
        return snap

    def indent(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    # First pass: mark lines we want to keep outright (carry a ref).
    keep = [("[ref=" in ln) for ln in lines]

    # Second pass: keep ancestors of any kept line so indentation stays valid.
    # Walk bottom-up tracking the indent of the shallowest still-needed child.
    needed_indent: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        ind = indent(lines[i])
        if keep[i]:
            # This line is kept; its parents (strictly shallower) are needed.
            needed_indent = ind
            continue
        if needed_indent is not None and ind < needed_indent:
            keep[i] = True
            needed_indent = ind

    out = [ln for ln, k in zip(lines, keep) if k]
    return "\n".join(out) if out else snap
