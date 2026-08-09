"""The one and only place HTML becomes Markdown.

ADR-0007 requires this seam: we track `html-to-markdown` with a `>=` rather
than freezing it, so the day we swap converters — or the day upstream changes a
default out from under us — exactly one function has to change, and
``tests/skill/test_markdown_golden.py`` is what makes the change visible.

Nothing else in the tree may import ``html_to_markdown`` directly.
"""
from __future__ import annotations

from typing import Any

# Built once per variant. `ConversionOptions` is a plain frozen options object,
# so module singletons are safe and save rebuilding 44 fields per page.
_OPTIONS: dict[bool, Any] = {}


def _options(strip_chrome: bool) -> Any:
    if strip_chrome not in _OPTIONS:
        from html_to_markdown import ConversionOptions, PreprocessingOptions

        # Two tiers of "remove things", and the difference is the whole point:
        #
        #   strip_chrome=False -> remove NOTHING. The verbatim page.
        #   strip_chrome=True  -> remove KNOWN chrome by tag/class (`<nav>`,
        #                         `<aside>`, `<footer>`, forms, `.sidebar`…).
        #
        # Neither one scores or guesses which block is the article — that is
        # Readability's job, upstream of here, and the reason this second tier
        # exists at all is that Readability's guessing can wipe a page out
        # (a GitHub issue page: 118 links -> 0, 2866 tokens -> 92). Tag-based
        # removal cannot do that: it deletes the elements it recognizes and
        # keeps everything else, so it is the honest fallback when extraction
        # collapses — the reader wanted the body text, not the site furniture.
        #
        # Measured on 3.10.6 with nav/header/main/aside/footer/.sidebar/form:
        #   enabled=False      keeps all 8 markers
        #   preset="standard"  drops only <nav> and <form>
        #   preset="aggressive" keeps header + body text + body links; drops the
        #                       rest of the furniture
        pre = (
            PreprocessingOptions(enabled=True, preset="aggressive")
            if strip_chrome
            else PreprocessingOptions(enabled=False)
        )
        _OPTIONS[strip_chrome] = ConversionOptions(
            # Never left at its default. `preprocessing=None` is NOT "off" — it
            # means "use the built-in default", which is
            # `enabled=True, remove_navigation=True, remove_forms=True`:
            #   '<nav><a href="/n">nav link</a></nav><p>body</p><form>…</form>'
            #     default        -> 'body\n'
            #     enabled=False  -> '[nav link](/n)\n\nbody\n\nQ\n'
            # So out of the box this library quietly deletes navigation and
            # forms on EVERY conversion, including the one the caller asked to
            # be verbatim. Removal has to be our decision, driven by `mode`, and
            # announced — never a converter default nobody chose.
            preprocessing=pre,
            # Default is False, and leaving it there pads every separator row
            # out to the widest cell in the column: a wide Wikipedia table
            # measured 984,849 chars vs 359,598 with this on. That single flag
            # is the whole of this library's "token-hungry" reputation.
            compact_tables=True,
            # Default is True, which prepends a YAML front-matter block built
            # from <title>/<meta> whenever the document has a <head>. The agent
            # asked for page content, not for a document envelope, and every
            # downstream consumer would have to strip it (ADR-0007: metadata
            # goes out-of-band, never into the body).
            extract_metadata=False,
            # Already the upstream default today. Pinned explicitly anyway
            # because we track upstream with `>=`: ATX (`# h1`) vs setext
            # (`h1\n===`) changes the bytes of every heading, and a default
            # flip should break a golden file, not silently reshape output.
            heading_style="atx",
        )
    return _OPTIONS[strip_chrome]


def convert_html(html: str, *, strip_chrome: bool = False) -> str:
    """Convert an HTML string to Markdown. Returns "" for empty input.

    ``strip_chrome=True`` additionally removes recognized page furniture
    (nav / aside / footer / forms / sidebar-ish class names) by tag and class,
    never by scoring. Use it for the fallback path where the reader wants body
    text; leave it off wherever the caller asked for the page verbatim, and off
    for a Readability fragment, which has already been stripped.

    The caller owns the empty-output decision. This function does NOT guard
    against the upstream defect where a stray ``<td>``/``<th>`` outside a table
    silently collapses its whole subtree to ``""`` (verified against 3.10.6:
    ``<div><td>hello</td></div>`` -> ``""``). That defect cannot fire on
    browser-serialized HTML, which is always well-formed — only on hand-built
    fragments, i.e. the Readability path — so the guard lives where the
    fragment is produced, as the "empty -> fall back to the full page" rule of
    ADR-0007.
    """
    if not html or not html.strip():
        return ""
    from html_to_markdown import convert

    return convert(html, _options(strip_chrome)).content
