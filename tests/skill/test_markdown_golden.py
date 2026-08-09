"""Golden files for the HTML -> Markdown conversion seam.

ADR-0007 makes these a **precondition** of tracking `html-to-markdown` with a
`>=` rather than an exact pin, not a nice-to-have. The library ships roughly 150
releases a year and is running a systematic markdown-fidelity campaign, so its
output bytes move. Without these, an upstream bump silently changes what every
agent reads; with them, it fails CI with a diff.

**When one of these fails after a dependency bump, that is the test working.**
Read the diff, decide whether the new output is better, and update the constant
deliberately — do not regenerate blindly.
"""
from __future__ import annotations

import pytest

from browserwright.repl._md_convert import convert_html


def test_headings_links_and_code_language():
    """The reason this converter was chosen over `markdownify`: the fence keeps
    its language. Losing that on documentation pages is the whole ballgame."""
    html = (
        "<h1>Title</h1><h2>Sub</h2>"
        '<p>Text with <a href="https://example.com/a">a link</a>.</p>'
        '<pre><code class="language-python">print("hi")</code></pre>'
    )
    assert convert_html(html) == (
        "# Title\n\n"
        "## Sub\n\n"
        "Text with [a link](https://example.com/a).\n\n"
        "```python\n"
        'print("hi")\n'
        "```\n"
    )


def test_table_separator_is_compact():
    """`compact_tables=True`. Left at its default, the separator row is padded
    to the widest cell, which measured 2.7x larger output on wide tables."""
    html = (
        "<table><thead><tr><th>a</th><th>looooooooooooong</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    assert convert_html(html) == (
        "| a | looooooooooooong |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
    )


def test_no_yaml_front_matter():
    """`extract_metadata=False`. The default prepends a YAML block built from
    <title>/<meta>; metadata belongs out-of-band (ADR-0007), not in the body."""
    html = (
        "<html><head><title>T</title>"
        '<meta name="description" content="D"></head>'
        "<body><h1>Hi</h1></body></html>"
    )
    out = convert_html(html)
    assert not out.startswith("---")
    assert out == "# Hi\n"


# The two tiers of removal, which is the distinction ADR-0007 turns on. Every
# marker is a bare word so a failure names exactly what appeared or vanished.
_CHROME_PAGE = (
    '<nav><a href="/n">NAVLINK</a></nav>'
    "<header>HEADERTEXT</header>"
    '<main><h1>Title</h1><p>BODYTEXT</p><p><a href="/d">BODYLINK</a></p></main>'
    '<aside class="sidebar">ASIDETEXT</aside>'
    "<footer>FOOTERTEXT</footer>"
    "<form><label>FORMLABEL</label></form>"
)


def test_verbatim_keeps_everything_including_nav_and_forms():
    """Regression guard on the nastiest default in this dependency.

    `preprocessing=None` does NOT mean "no preprocessing" — it means "use the
    built-in default", which is `remove_navigation=True, remove_forms=True`.
    Left alone, the library silently deletes navigation and forms from EVERY
    conversion, including the one the caller explicitly asked to be verbatim.
    If this test ever fails, someone dropped the explicit
    `PreprocessingOptions(enabled=False)`.
    """
    out = convert_html(_CHROME_PAGE)
    for marker in ("NAVLINK", "HEADERTEXT", "BODYTEXT", "BODYLINK",
                   "ASIDETEXT", "FOOTERTEXT", "FORMLABEL"):
        assert marker in out, f"verbatim conversion lost {marker}"


def test_strip_chrome_removes_furniture_but_never_the_body():
    """The fallback tier. It must remove page furniture and it must NOT be
    capable of removing the article — that is the property that makes it a safe
    landing spot when the extractor collapses."""
    out = convert_html(_CHROME_PAGE, strip_chrome=True)
    assert "BODYTEXT" in out and "BODYLINK" in out
    for marker in ("NAVLINK", "ASIDETEXT", "FOOTERTEXT", "FORMLABEL"):
        assert marker not in out, f"strip_chrome left {marker} behind"


@pytest.mark.parametrize("html", ["", "   ", "\n\t "])
def test_empty_input_is_empty_output(html):
    assert convert_html(html) == ""


def test_stray_table_cell_collapses_to_empty_upstream_defect():
    """Pins a known upstream defect so we notice if it is ever fixed.

    A stray `<td>`/`<th>` outside a table silently collapses its whole subtree
    (Chrome, by contrast, keeps the text). It cannot fire on browser-serialized
    HTML, which is always well-formed — only on hand-built fragments, i.e. the
    Readability path — where `render_page_markdown`'s "empty means collapsed"
    rule already catches it. If this starts failing, upstream fixed it; that is
    good news, and this test should be deleted rather than "repaired".
    """
    assert convert_html("<div><td>hello</td></div>") == ""
    assert convert_html("<div><th>x</th></div><p>after</p>") == "after\n"
