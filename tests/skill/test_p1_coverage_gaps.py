"""P1 coverage-gap fixes from REVIEW.md (F-9 + F-13).

Focused supplemental tests:
  * host_stem param matrix expanded to cover all listed multi-label TLDs
    (was only 2 sampled; 8 more cover the rest of the practical bucket)
  * TOML escape control-char rejection (F-13)
"""
from __future__ import annotations

import pytest


# ---- F-9 host_stem TLD coverage --------------------------------------


@pytest.mark.parametrize("host,expected", [
    # UK multi-labels (4 total in _MULTI_LABEL_TLDS).
    ("gov.example.gov.uk", "example.gov.uk"),
    ("ac.example.ac.uk", "example.ac.uk"),
    ("dept.example.org.uk", "example.org.uk"),
    # JP multi-labels (5 total).
    ("a.example.ac.jp", "example.ac.jp"),
    ("b.example.or.jp", "example.or.jp"),
    ("c.example.ne.jp", "example.ne.jp"),
    ("d.example.go.jp", "example.go.jp"),
    # Asia / Pacific.
    ("x.example.com.au", "example.com.au"),
    ("y.example.co.in", "example.co.in"),
    ("z.example.co.za", "example.co.za"),
    # CN public suffix family.
    ("foo.example.net.cn", "example.net.cn"),
    ("foo.example.gov.cn", "example.gov.cn"),
    # LATAM.
    ("a.example.com.br", "example.com.br"),
    ("b.example.com.mx", "example.com.mx"),
])
def test_host_stem_multi_label_tlds(host, expected):
    from browserwright.memory.site_mem import host_stem
    assert host_stem(host) == expected


@pytest.mark.parametrize("host,expected", [
    # Uppercase / mixed-case → lowercased.
    ("WWW.GitHub.com", "github.com"),
    ("EXAMPLE.CO.UK", "example.co.uk"),
    # Trailing dot (FQDN form) → stripped.
    ("github.com.", "github.com"),
    # IPv4 — no TLD logic, returns as-is.
    ("127.0.0.1", "0.1"),  # current behaviour: last-two labels; documents it
])
def test_host_stem_edge_cases(host, expected):
    from browserwright.memory.site_mem import host_stem
    assert host_stem(host) == expected


# ---- F-13 TOML escape control-char rejection -------------------------


def test_toml_escape_rejects_control_chars():
    from browserwright import install
    with pytest.raises(ValueError) as exc:
        install._toml_escape("hello\x00world")
    msg = str(exc.value)
    assert "U+0000" in msg
    assert "offset 5" in msg


def test_toml_escape_rejects_del_char():
    from browserwright import install
    with pytest.raises(ValueError):
        install._toml_escape("data\x7Fmore")


def test_toml_escape_allows_tab_newline_cr():
    """These three are the whitelisted control chars (TOML basic-string
    escapes ``\\t \\n \\r``). Must NOT raise."""
    from browserwright import install
    out = install._toml_escape("a\tb\nc\rd")
    assert out == "a\\tb\\nc\\rd"


def test_toml_escape_handles_backslash_and_quote():
    from browserwright import install
    assert install._toml_escape('he said "hi"') == r'he said \"hi\"'
    assert install._toml_escape("c:\\path") == r"c:\\path"


# ---- F-9 dotted-key docstring caveat regression guard ---------------


def test_remember_preference_docstring_documents_type_mismatch_caveat():
    """REVIEW.md F-9 P3: the dotted-key contract silently destroys a
    scalar root when promoted to a dict. Docstring must surface this."""
    from browserwright.primitives.site import remember_preference
    doc = remember_preference.__doc__ or ""
    assert "Caveat" in doc or "caveat" in doc
    # Mentions the type-mismatch failure mode.
    assert "scalar" in doc.lower() or "type mismatch" in doc.lower()
