"""``read_markdown()`` — the content view (ADR-0006).

``snapshot()`` answers *what can I do on this page*; this answers *what does it
say*. They are the two members of the **view** class: per-heredoc injected,
read-only, and — because a view needs the live ``page`` and ``EXPORTS`` holds
module-level functions that cannot have one — deliberately absent from
``browserwright.EXPORTS``.

The pipeline, and why it has the shape it has (all of it is ADR-0007):

    live DOM
      │  page.evaluate(_md_normalize)      absolutize URLs, flatten open shadow
      │                                    roots, inline same-origin iframes
      ▼
    normalized HTML  ─── optional ───►  Readability (in the same pass, on a
      │                                 scratch document it may destroy)
      ▼                                      │
    _md_convert.convert_html  ◄──────────────┘
      ▼
    markdown  ──►  whole-line truncation for the agent
               └─►  the untruncated text on disk, path reported out-of-band

**This view takes no ``url``.** Navigating would move the agent's working tab
and invalidate every ``[ref=eN]`` it currently holds, breaking the
observe → act → observe discipline from a call that reads like a read. To move,
the agent calls ``page.goto(url)`` — already patched for SPA-safe waiting by
``_smart_goto``. The one-shot ``browserwright markdown <url>`` command is the
surface that owns a URL, and it owns a whole session lifecycle to go with it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..errors import UnsupportedContentType
# Shared on purpose rather than reimplemented: cutting Markdown at a byte offset
# produces a broken `[text](http://…` link for exactly the reason cutting an
# aria snapshot produces a broken `[ref=e1` — and letting the two truncators
# drift apart is how one of them silently loses that property.
from .._text import PRODUCER_BUDGET, truncate_lines as _truncate_lines
from . import _md_normalize
from ._md_convert import convert_html

MODES = ("auto", "article", "full")

# Kept UNDER the executor transport's own bound (`MAX_TEXT_CHARS`), and now
# derived from it rather than restated: a heredoc normally prints other things
# alongside this payload, so the producer budget needs headroom under the
# channel bound. The transport's cut is whole-line aware too as of #54/#55, but
# staying under it is still what keeps THIS view's line-integrity promise from
# being re-cut downstream.
DEFAULT_MAX_CHARS = PRODUCER_BUDGET

# Below this, an extraction is treated as collapsed rather than concise.
# Aligned with Readability's own `charThreshold` default, which is the number it
# uses to decide a document has an article at all. Fitted to a 7-page corpus and
# expected to be tuned — which is why it is one named constant (ADR-0007).
ARTICLE_MIN_CHARS = 500

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})


@dataclass
class RenderedPage:
    """One page rendered to Markdown, plus everything the caller must be told.

    ``notes`` is the out-of-band channel: which path was taken, what was left
    out. It never goes into ``markdown`` itself — every downstream consumer
    would have to strip it back out, and that is what a metadata channel is for.

    ``mode_used`` is what actually happened, which is not the requested ``mode``:
    ``"article"`` (extracted), ``"stripped"`` (whole page minus recognized
    chrome — where ``auto`` lands when extraction collapses), or ``"full"``
    (verbatim).
    """

    markdown: str
    url: str = ""
    title: str = ""
    mode_used: str = "full"
    notes: list[str] = field(default_factory=list)


def _content_type(raw: str) -> str:
    """`text/html; charset=utf-8` -> `text/html`."""
    return (raw or "").split(";", 1)[0].strip().lower()


def render_page_markdown(page: Any, *, mode: str = "auto") -> RenderedPage:
    """Render ``page``'s CURRENT document to Markdown. Never navigates.

    Shared by both surfaces (ADR-0006): the injected view and the one-shot
    ``browserwright markdown <url>`` command. Returns the full text — capping it
    is the caller's job, because the two surfaces want opposite things.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES!r}, got {mode!r}")

    want_article = mode in ("auto", "article")
    raw = page.evaluate(
        _md_normalize.build_script(extract=want_article),
        {"extract": want_article},
    )

    ctype = _content_type(raw.get("contentType", ""))
    if ctype and ctype not in _HTML_TYPES:
        raise UnsupportedContentType(raw.get("url", ""), ctype)

    notes: list[str] = []
    stats = raw.get("stats") or {}
    if stats.get("crossOriginFrames"):
        # ADR-0007: excluded content is announced, never silently dropped.
        notes.append(
            f"{stats['crossOriginFrames']} cross-origin iframe(s) excluded "
            "(their content is unreachable from the page)"
        )
    if stats.get("sameOriginFrames"):
        notes.append(f"{stats['sameOriginFrames']} same-origin iframe(s) inlined")

    full_html = raw.get("fullHtml") or ""

    article_md = ""
    if want_article and raw.get("articleHtml"):
        # No chrome-stripping here: Readability has already removed everything
        # it believes is furniture, and running a second remover over its output
        # only adds a second chance to delete something real.
        article_md = convert_html(raw["articleHtml"])

    # The collapse test. Deliberately absolute, not a ratio: measured on the
    # same corpus, a GOOD extraction (StackOverflow) retained 1.6% of links and
    # 4.6% of text while a TOTAL collapse (a GitHub issue page) retained 0% and
    # 3.2% — no ratio threshold separates them, and the one that would reject
    # the collapse also rejects the best result in the set. What distinguishes
    # a collapse is that nothing is left in absolute terms.
    if mode == "article":
        # Forced. Refuse to silently hand back a collapse; the caller asked for
        # extraction specifically, so a fallback would be answering a different
        # question than the one asked.
        if len(article_md.strip()) < ARTICLE_MIN_CHARS:
            notes.append(
                f"extraction produced {len(article_md.strip())} chars "
                f"(< {ARTICLE_MIN_CHARS}); re-run with mode='full' for the "
                "whole page"
            )
        return RenderedPage(article_md, raw.get("url", ""), raw.get("title", ""),
                            "article", notes)

    if mode == "auto":
        if len(article_md.strip()) >= ARTICLE_MIN_CHARS:
            notes.append(
                "main content extracted; mode='full' returns the whole page")
            return RenderedPage(article_md, raw.get("url", ""),
                                raw.get("title", ""), "article", notes)
        # Extraction collapsed. The point of this call is still the body text,
        # so falling all the way back to the verbatim page would answer with a
        # screenful of navigation. Strip recognized chrome instead: tag/class
        # based, so unlike the extractor it cannot delete the article itself.
        notes.append(
            f"extraction collapsed to {len(article_md.strip())} chars, so the "
            "page is returned with nav/aside/footer/forms removed; "
            "mode='full' returns it verbatim"
        )
        return RenderedPage(convert_html(full_html, strip_chrome=True),
                            raw.get("url", ""), raw.get("title", ""),
                            "stripped", notes)

    return RenderedPage(convert_html(full_html), raw.get("url", ""),
                        raw.get("title", ""), "full", notes)


def spill_path(url: str = "") -> str:
    """A non-colliding /tmp path for the untruncated Markdown.

    Mirrors `cli._fresh_screenshot_path`: the executor and its client share a
    filesystem, so a large payload rides the disk instead of the wire (the same
    reason `screenshots` in the executor protocol is path-based). Nothing prunes
    these, exactly like the screenshots — a deliberate, recorded choice.
    """
    i = 0
    while True:
        cand = Path("/tmp") / f"browserwright-md-{os.getpid()}-{i}.md"
        if not cand.exists():
            return str(cand)
        i += 1


def make_read_markdown(handle: Any, namespace: Optional[dict] = None):
    """Build the per-heredoc ``read_markdown()`` bound to this heredoc's handle.

    ``handle`` is anything exposing a live ``.page``: the lazy
    ``PlaywrightHandle`` on the in-process path, or the resident executor's
    live-page holder (issue #59 — resolving the lazy handle *inside* the
    executor re-enters ``sync_playwright()`` in a running asyncio loop).

    ``namespace`` is the exec globals dict. It is read LAZILY at call time
    because the executor injects its ``_bw_warn`` channel into that same dict
    only after this factory has run — so binding it now would always miss.
    Absent that channel (the in-process path) notes go to stderr, which is where
    `inline.py` renders warnings anyway.
    """

    def _emit(notes: list[str]) -> None:
        if not notes:
            return
        warn = (namespace or {}).get("_bw_warn")
        for note in notes:
            if callable(warn):
                warn(f"read_markdown: {note}")
            else:
                import sys

                print(f"[WARNING] read_markdown: {note}", file=sys.stderr)

    def read_markdown(*, mode: str = "auto",
                      max_chars: int | None = DEFAULT_MAX_CHARS) -> str:
        """Read the current ``page`` as Markdown — the content view.

        Links are absolute, open shadow roots are flattened in, and same-origin
        iframes are inlined, none of which survives a plain ``page.content()``.

        Does NOT navigate. Move the tab yourself first::

            page.goto("https://example.com/docs")
            print(read_markdown())

        Args:
          mode: ``"auto"`` (default) extracts the main content, and when
            extraction collapses falls back to the whole page with
            nav/aside/footer/forms removed — the goal is body text either way.
            ``"article"`` forces extraction. ``"full"`` returns the page
            verbatim, nothing removed: use it when you came for the navigation,
            a form, or every link on the page.
          max_chars: whole-line cap on the returned string, never a mid-line
            cut. The untruncated Markdown is written to a temp file whose path
            is reported alongside, so you can read it if you need the rest.
            Pass None to disable the cap — but note the executor transport caps
            printed output at 10000 characters regardless.

        Returns the Markdown as a string. Raises ``UnsupportedContentType`` when
        the page is not HTML.
        """
        rendered = render_page_markdown(handle.page, mode=mode)
        text = rendered.markdown
        notes = list(rendered.notes)

        if max_chars is not None and len(text) > max_chars:
            path = spill_path(rendered.url)
            try:
                Path(path).write_text(text, encoding="utf-8")
                notes.append(
                    f"output truncated to {max_chars} chars; full "
                    f"{len(text)}-char markdown written to {path}"
                )
            except OSError as e:
                notes.append(
                    f"output truncated to {max_chars} chars; could not write "
                    f"the full text ({e}) — re-run with a larger max_chars"
                )
            text = _truncate_lines(text, max_chars)

        _emit(notes)
        return text

    return read_markdown
