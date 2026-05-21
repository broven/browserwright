import pytest
from browser_daemon.userscripts import parse_userscript, UserscriptParseError

BASIC = """\
// ==UserScript==
// @name         HN Tidy
// @namespace    bd.userscripts
// @match        https://news.ycombinator.com/*
// @run-at       document-idle
// @version      1.2
// @description  Collapse noise
// ==/UserScript==
(function(){ window.__x = 1; })();
"""


def test_parses_core_fields():
    us = parse_userscript(BASIC)
    assert us.name == "HN Tidy"
    assert us.namespace == "bd.userscripts"
    assert us.matches == ["https://news.ycombinator.com/*"]
    assert us.run_at == "document_idle"
    assert us.version == "1.2"
    assert us.description == "Collapse noise"
    assert us.code.strip().startswith("(function()")


def test_identity_and_id_are_stable():
    us = parse_userscript(BASIC)
    assert us.identity == "bd.userscripts/HN Tidy"
    assert us.id == parse_userscript(BASIC).id
    assert us.id.isalnum() and len(us.id) == 12


def test_multiple_match_include_exclude():
    src = BASIC.replace(
        "// @match        https://news.ycombinator.com/*",
        "// @match        https://a.com/*\n"
        "// @include      https://b.com/*\n"
        "// @exclude      https://a.com/admin/*",
    )
    us = parse_userscript(src)
    assert us.matches == ["https://a.com/*", "https://b.com/*"]
    assert us.exclude_matches == ["https://a.com/admin/*"]


def test_run_at_default_is_document_idle():
    src = BASIC.replace("// @run-at       document-idle\n", "")
    assert parse_userscript(src).run_at == "document_idle"


def test_run_at_normalizes_dashes():
    src = BASIC.replace("document-idle", "document-start")
    assert parse_userscript(src).run_at == "document_start"


def test_namespace_defaults_when_absent():
    src = BASIC.replace("// @namespace    bd.userscripts\n", "")
    us = parse_userscript(src)
    assert us.namespace == "bd.userscripts"


def test_unsupported_directives_warn_not_fail():
    src = BASIC.replace(
        "// @version      1.2",
        "// @version      1.2\n"
        "// @grant        GM_setValue\n"
        "// @require      https://example.com/lib.js",
    )
    us = parse_userscript(src)
    assert any("grant" in w.lower() for w in us.warnings)
    assert any("require" in w.lower() for w in us.warnings)
    assert us.name == "HN Tidy"


def test_missing_header_raises():
    with pytest.raises(UserscriptParseError):
        parse_userscript("just some js without a header")


def test_no_match_raises():
    src = BASIC.replace("// @match        https://news.ycombinator.com/*\n", "")
    with pytest.raises(UserscriptParseError):
        parse_userscript(src)


def test_invalid_match_pattern_is_dropped_with_warning():
    # A scheme-less pattern (the common typo) is not a legal Chrome match
    # pattern: drop it and warn, keeping any valid siblings.
    src = (
        "// ==UserScript==\n"
        "// @name         Mixed\n"
        "// @match        example.com/*\n"          # invalid: no scheme
        "// @match        https://good.com/*\n"      # valid
        "// ==/UserScript==\n"
        "console.log(1);\n"
    )
    us = parse_userscript(src)
    assert us.matches == ["https://good.com/*"]
    assert any("not a valid match pattern" in w for w in us.warnings)


def test_all_invalid_matches_raises():
    src = (
        "// ==UserScript==\n"
        "// @name         AllBad\n"
        "// @match        example.com/*\n"
        "// ==/UserScript==\n"
        "console.log(1);\n"
    )
    with pytest.raises(UserscriptParseError):
        parse_userscript(src)


def test_subdomain_and_all_urls_patterns_accepted():
    src = (
        "// ==UserScript==\n"
        "// @name         Wild\n"
        "// @match        https://*.example.com/*\n"
        "// @match        <all_urls>\n"
        "// @match        *://*/*\n"
        "// ==/UserScript==\n"
        "console.log(1);\n"
    )
    us = parse_userscript(src)
    assert us.matches == ["https://*.example.com/*", "<all_urls>", "*://*/*"]
    assert us.warnings == []
