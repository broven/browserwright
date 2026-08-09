"""`read_markdown()` — the content view (ADR-0006) and its content rules (0007).

The in-page half is exercised against a real browser elsewhere; here the page is
faked so the *decisions* — which tier was used, what got announced, what was
refused — are tested without a browser and without flakiness.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from browserwright.errors import UnsupportedContentType
from browserwright.repl.markdown import (
    ARTICLE_MIN_CHARS,
    make_read_markdown,
    render_page_markdown,
)

_LONG = "<p>" + ("Substantive article body text. " * 40) + "</p>"
_CHROME = (
    '<nav><a href="https://e.com/n">NAVLINK</a></nav>'
    "<footer>FOOTERTEXT</footer>"
)


class _FakePage:
    """Stands in for a Playwright Page. `evaluate` returns the canned payload
    the in-page normalizer would have produced."""

    def __init__(self, **payload):
        self.payload = {
            "fullHtml": "",
            "articleHtml": None,
            "articleLinks": 0,
            "title": "T",
            "url": "https://e.com/p",
            "contentType": "text/html",
            "stats": {},
        }
        self.payload.update(payload)
        self.scripts: list[str] = []

    def evaluate(self, script, arg=None):
        self.scripts.append(script)
        return self.payload


def _page(**kw):
    return _FakePage(**kw)


# --- tier selection -------------------------------------------------------

def test_auto_uses_extraction_when_it_did_not_collapse():
    page = _page(fullHtml="<h1>T</h1>" + _CHROME + _LONG, articleHtml=_LONG)
    r = render_page_markdown(page, mode="auto")
    assert r.mode_used == "article"
    assert "NAVLINK" not in r.markdown
    assert "Substantive article body text." in r.markdown


def test_auto_falls_back_to_stripped_not_verbatim_when_extraction_collapses():
    """The load-bearing behaviour. A collapsed extraction must not dump the raw
    page back: the caller came for body text either way, so the fallback is the
    page minus recognized chrome."""
    page = _page(fullHtml="<h1>T</h1>" + _CHROME + _LONG,
                 articleHtml="<p>tiny</p>")
    r = render_page_markdown(page, mode="auto")
    assert r.mode_used == "stripped"
    assert "Substantive article body text." in r.markdown   # body survives
    assert "NAVLINK" not in r.markdown                      # furniture does not
    assert "FOOTERTEXT" not in r.markdown
    assert any("collapsed" in n for n in r.notes)


def test_auto_treats_missing_extraction_as_collapse():
    """Readability returning nothing at all (or throwing in-page) is the same
    event as returning too little."""
    page = _page(fullHtml=_LONG, articleHtml=None)
    r = render_page_markdown(page, mode="auto")
    assert r.mode_used == "stripped"


def test_full_is_verbatim():
    page = _page(fullHtml="<h1>T</h1>" + _CHROME + _LONG, articleHtml=_LONG)
    r = render_page_markdown(page, mode="full")
    assert r.mode_used == "full"
    assert "NAVLINK" in r.markdown and "FOOTERTEXT" in r.markdown


def test_full_does_not_ship_the_extractor_across_the_wire():
    """`mode="full"` cannot use Readability, so it must not pay to send it.

    Asserted on payload size rather than a substring: the vendored library is
    ~91 KB and the normalizer is a couple of KB, so an order-of-magnitude gap is
    the honest signal that the source is or is not inlined.
    """
    full_page, auto_page = _page(fullHtml=_LONG), _page(fullHtml=_LONG)
    render_page_markdown(full_page, mode="full")
    render_page_markdown(auto_page, mode="auto")
    assert len(full_page.scripts[0]) < 20_000
    assert len(auto_page.scripts[0]) > 80_000


def test_article_is_forced_and_reports_a_collapse_rather_than_substituting():
    """A forced extraction that collapses still returns the extraction. Handing
    back the whole page would answer a different question than the one asked —
    but staying silent about it would be the failure ADR-0007 forbids."""
    page = _page(fullHtml=_LONG, articleHtml="<p>tiny</p>")
    r = render_page_markdown(page, mode="article")
    assert r.mode_used == "article"
    assert "Substantive" not in r.markdown
    assert any(str(ARTICLE_MIN_CHARS) in n for n in r.notes)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="mode must be one of"):
        render_page_markdown(_page(), mode="fuller")


# --- refusals and announcements -------------------------------------------

@pytest.mark.parametrize("ctype", [
    "application/pdf", "image/png", "application/json", "text/plain",
])
def test_non_html_is_refused_loudly(ctype):
    """A PDF renders a real DOM in Chrome's viewer, so best-effort conversion
    would 'succeed' and return a plausible-looking nothing."""
    page = _page(contentType=ctype, fullHtml="<p>whatever</p>")
    with pytest.raises(UnsupportedContentType) as e:
        render_page_markdown(page, mode="full")
    assert ctype in str(e.value)


@pytest.mark.parametrize("ctype", [
    "text/html", "text/html; charset=utf-8", "application/xhtml+xml", "",
])
def test_html_content_types_are_accepted(ctype):
    r = render_page_markdown(_page(contentType=ctype, fullHtml="<p>ok</p>"),
                             mode="full")
    assert r.markdown.strip() == "ok"


def test_excluded_cross_origin_frames_are_announced():
    """Excluded content is announced, never silently dropped."""
    page = _page(fullHtml="<p>x</p>",
                 stats={"crossOriginFrames": 2, "sameOriginFrames": 1})
    r = render_page_markdown(page, mode="full")
    assert any("2 cross-origin" in n for n in r.notes)
    assert any("1 same-origin" in n for n in r.notes)


def test_notes_never_leak_into_the_markdown_body():
    page = _page(fullHtml="<p>body</p>", stats={"crossOriginFrames": 1})
    r = render_page_markdown(page, mode="full")
    assert r.notes
    assert r.markdown.strip() == "body"


# --- the injected view ----------------------------------------------------

def _view(page, warnings):
    return make_read_markdown(types.SimpleNamespace(page=page),
                              {"_bw_warn": warnings.append})


def test_view_takes_no_url_argument():
    """ADR-0006: navigating would invalidate every [ref=eN] the agent holds."""
    warnings: list[str] = []
    with pytest.raises(TypeError):
        _view(_page(fullHtml="<p>x</p>"), warnings)("https://e.com")


def test_view_emits_notes_out_of_band_and_returns_only_content():
    warnings: list[str] = []
    page = _page(fullHtml="<p>body</p>", stats={"crossOriginFrames": 1})
    out = _view(page, warnings)(mode="full")
    assert out.strip() == "body"
    assert any("cross-origin" in w for w in warnings)


def test_truncation_is_whole_line_and_spills_the_rest_to_a_file():
    warnings: list[str] = []
    page = _page(fullHtml="".join(f"<p>line {i} padding</p>" for i in range(200)))
    out = _view(page, warnings)(mode="full", max_chars=200)

    assert len(out) <= 200
    assert out.endswith("… [truncated]")
    # Whole lines only: a mid-line cut is what produces broken `[text](http://…`
    assert all(ln == "" or ln.startswith("line ") or ln.startswith("…")
               for ln in out.splitlines())

    spill = [w.split("written to ")[1] for w in warnings if "written to " in w]
    assert spill, f"no spill path announced: {warnings}"
    full = Path(spill[0]).read_text(encoding="utf-8")
    try:
        assert "line 199 padding" in full
        assert len(full) > len(out)
    finally:
        Path(spill[0]).unlink(missing_ok=True)


def test_no_spill_and_no_note_when_output_fits():
    warnings: list[str] = []
    out = _view(_page(fullHtml="<p>short</p>"), warnings)(mode="full")
    assert out.strip() == "short"
    assert not any("written to" in w for w in warnings)


def test_max_chars_none_disables_the_cap():
    warnings: list[str] = []
    page = _page(fullHtml="".join(f"<p>line {i}</p>" for i in range(500)))
    out = _view(page, warnings)(mode="full", max_chars=None)
    assert "line 499" in out
    assert not any("truncated" in w for w in warnings)


def test_view_survives_without_the_executor_warn_channel(capsys):
    """The in-process path has no `_bw_warn`; notes must still surface, on
    stderr, where `inline.py` renders warnings anyway."""
    view = make_read_markdown(
        types.SimpleNamespace(page=_page(fullHtml="<p>x</p>",
                                         stats={"crossOriginFrames": 1})),
        None,
    )
    view(mode="full")
    assert "cross-origin" in capsys.readouterr().err
