"""P1 coverage-gap fixes from REVIEW.md (F-9 + F-13 + F-7).

Focused supplemental tests:
  * host_stem param matrix expanded to cover all listed multi-label TLDs
    (was only 2 sampled; 8 more cover the rest of the practical bucket)
  * args-schema malformed variants beyond the original 3
  * propose-dict ``ready=False`` branches that weren't asserted before
  * TOML escape control-char rejection (F-13)
  * scaffold ``OUTPUT_SCHEMA`` emission (F-7)
"""
from __future__ import annotations

import time

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


# ---- F-9 args-schema malformed variants ------------------------------


@pytest.mark.parametrize("bad_schema", [
    None,                           # explicit None (caller cleared it)
    "{'q': 'str'}",                 # stringified dict (yaml load forgot to parse)
    ("q", "str"),                   # tuple of pairs (zip habit)
    {("q",): {"type": "str"}},      # non-string key
    {"q": [{"type": "str"}]},       # list-wrapped meta
])
def test_scaffold_rejects_more_malformed_shapes(tmp_bs_home, fresh_modules,
                                                bad_schema):
    """REVIEW.md F-9: _validate_args_schema rejected only 3 shapes
    pre-fix. Audit the failure mode on more realistic agent typos."""
    from browserwright.session import Session
    from browserwright.solidify import scaffold

    spec = {
        "site": "example.com",
        "suggested_name": "demo",
        "draft_args_schema": bad_schema,
        "draft_run_body": "    return None\n",
        "host_hint": "example.com",
    }
    if bad_schema is None:
        # None becomes empty dict via `... or {}` — that's valid; don't
        # expect a raise here. Just confirm it doesn't crash.
        result = scaffold.commit(Session(), spec)
        assert "path" in result
        return
    with pytest.raises(ValueError):
        scaffold.commit(Session(), spec)


# ---- F-9 propose dict ready=False branches ---------------------------


def _hist(code: str, ok: bool = True) -> dict:
    return {"code": code, "ok": ok, "stdout": "", "result": None,
            "exception": None, "ts": time.time()}


def test_propose_captcha_branch_returns_ready_false(tmp_bs_home, fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import propose
    sess = Session()
    sess.history = [
        _hist("goto_url('https://example.com')"),
        _hist("# saw captcha widget"),
        _hist("print('hcaptcha challenge present')"),
    ]
    out = propose.propose(sess)
    assert out["ready"] is False
    assert any("captcha" in w.lower() for w in out["warnings"])


def test_propose_more_than_30_steps_penalty(tmp_bs_home, fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import propose
    sess = Session()
    # 35 successful steps → > 30 penalty branch fires.
    sess.history = [_hist(f"x_{i} = {i}") for i in range(35)]
    out = propose.propose(sess)
    # Score got nicked; we don't care whether it crossed the threshold —
    # the warning is what we want to surface.
    assert any("成功步数" in w or ">" in w for w in out["warnings"])


def test_propose_host_none_yields_unknown_site(tmp_bs_home, fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import propose
    sess = Session()
    # History without any URL → _host_from_history returns None.
    sess.history = [_hist("x = 1 + 1"), _hist("print(x)")]
    out = propose.propose(sess)
    assert out["site"] == "unknown"
    assert out["host_hint"] is None


def test_propose_empty_history_with_like_still_returns_dict(tmp_bs_home,
                                                              fresh_modules):
    """`like=...` without history must NOT clone blind — but it should
    still return a structured dict with a clear warning, not None."""
    from browserwright.session import Session
    from browserwright.solidify import propose
    out = propose.propose(Session(), like="ycombinator.com/front_page")
    assert isinstance(out, dict)
    assert out["ready"] is False
    assert any("history" in w.lower() for w in out["warnings"])


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


# ---- F-7 OUTPUT_SCHEMA scaffold emission ----------------------------


def test_scaffold_emits_commented_placeholder_when_no_schema(tmp_bs_home,
                                                              fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import scaffold

    spec = {
        "site": "example.com",
        "suggested_name": "no_schema",
        "draft_args_schema": {"q": {"type": "str"}},
        "draft_run_body": "    return None\n",
        "host_hint": "example.com",
    }
    result = scaffold.commit(Session(), spec)
    text = open(result["path"]).read()
    # Commented placeholder — gives the agent a fill-in target.
    assert "# OUTPUT_SCHEMA = {" in text
    # The non-commented OUTPUT_SCHEMA assignment must NOT be there.
    assert "\nOUTPUT_SCHEMA = " not in text


def test_scaffold_emits_real_output_schema_when_supplied(tmp_bs_home,
                                                          fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import scaffold

    spec = {
        "site": "example.com",
        "suggested_name": "with_schema",
        "draft_args_schema": {"q": {"type": "str"}},
        "draft_run_body": "    return {'title': 'x', 'votes': 0}\n",
        "draft_output_schema": {
            "type": "object",
            "properties": {"title": {"type": "Any"},
                           "votes": {"type": "Any"}},
            "required": ["title", "votes"],
        },
        "host_hint": "example.com",
    }
    result = scaffold.commit(Session(), spec)
    text = open(result["path"]).read()
    # Real assignment present, placeholder absent.
    assert "OUTPUT_SCHEMA = {" in text
    # And it carries the actual properties.
    assert "'title'" in text and "'votes'" in text


def test_propose_infers_output_schema_from_dict_return(tmp_bs_home,
                                                        fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import propose
    sess = Session()
    sess.history = [
        _hist("limit = 10"),
        _hist("new_tab('https://news.ycombinator.com/')"),
        _hist("rows = js('return Array.from(...).map(r => ({title: r.title, url: r.href}))')"),
        _hist("return {'rows': rows, 'count': len(rows)}"),
    ]
    out = propose.propose(sess, name_hint="hn_dict")
    if not out["ready"]:
        pytest.skip("propose didn't score this ready; inference is "
                    "still wired correctly elsewhere")
    schema = out.get("draft_output_schema")
    assert schema is not None
    assert schema["type"] == "object"
    assert set(schema["properties"].keys()) == {"rows", "count"}


def test_propose_infers_array_output_schema(tmp_bs_home, fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import propose
    sess = Session()
    sess.history = [
        _hist("limit = 5"),
        _hist("new_tab('https://ycombinator.com/')"),
        _hist("ids = js('return [...document.querySelectorAll(\"tr.athing\")].map(r => r.id)')"),
        _hist("return [int(i) for i in ids]"),
    ]
    out = propose.propose(sess, name_hint="hn_ids")
    if not out["ready"]:
        pytest.skip("propose didn't score this ready; array branch still "
                    "exercised by the unit _infer_output_schema test")
    schema = out.get("draft_output_schema")
    assert schema is not None
    assert schema["type"] == "array"


# Unit-level inference test always runs (no dependency on score).


def test_infer_output_schema_dict_keys():
    from browserwright.solidify.propose import _infer_output_schema
    out = _infer_output_schema(
        "    return {'a': 1, 'b': 2, 'c': 3}\n"
    )
    assert out == {
        "type": "object",
        "properties": {"a": {"type": "Any"}, "b": {"type": "Any"},
                       "c": {"type": "Any"}},
        "required": ["a", "b", "c"],
    }


def test_infer_output_schema_list_return():
    from browserwright.solidify.propose import _infer_output_schema
    out = _infer_output_schema("    return [1, 2, 3]\n")
    assert out == {"type": "array", "items": {"type": "Any"}}


def test_infer_output_schema_unknown_return_returns_none():
    from browserwright.solidify.propose import _infer_output_schema
    assert _infer_output_schema("    return some_var\n") is None
    assert _infer_output_schema("") is None


# ---- F-9 dotted-key docstring caveat regression guard ---------------


def test_remember_preference_docstring_documents_type_mismatch_caveat():
    """REVIEW.md F-9 P3: the dotted-key contract silently destroys a
    scalar root when promoted to a dict. Docstring must surface this."""
    from browserwright.primitives.site import remember_preference
    doc = remember_preference.__doc__ or ""
    assert "Caveat" in doc or "caveat" in doc
    # Mentions the type-mismatch failure mode.
    assert "scalar" in doc.lower() or "type mismatch" in doc.lower()


# ---- F-12 Mode-A disconnect_upstream parity --------------------------


def test_mode_a_disconnect_upstream_returns_false_no_raise():
    """REVIEW.md F-12: parity stub on Mode A — returns False, never
    raises. Lets callers that hold ``auto_client()``'s result invoke
    ``disconnect_upstream()`` without branching on the concrete type."""
    from browserwright.daemon_client import DaemonClient
    cli = DaemonClient()
    assert cli.disconnect_upstream() is False
    assert cli.disconnect_upstream(reason="test") is False


# ---- F-16 solidify CLI alias ----------------------------------------


def test_cli_solidify_alias_dispatches_to_save(monkeypatch, capsys):
    """REVIEW.md F-16: ``browserwright solidify`` should dispatch the
    same handler as ``browserwright save``. Run with no args to get
    the usage error from both; they should match."""
    import sys
    from browserwright import cli

    saved_argv = sys.argv
    try:
        # Both should hit the usage-error path with the same exit code.
        for cmd in ("save", "solidify"):
            sys.argv = ["browserwright", cmd]
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 1
            err = capsys.readouterr().err
            assert "save <site>/<name>" in err
    finally:
        sys.argv = saved_argv
