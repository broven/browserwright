"""v0.3.1 — fixes for 4 bugs surfaced by the agent-sdk-tester E2E run.

Each test names the bug. All four bugs are independent.
"""
from __future__ import annotations

import time

import pytest


def _hist(code: str, ok: bool = True) -> dict:
    return {"code": code, "ok": ok, "stdout": "", "result": None,
            "exception": None, "ts": time.time()}


# ----- Bug 1: host_stem now returns eTLD+1 ---------------------------


@pytest.mark.parametrize("host,expected", [
    ("news.ycombinator.com", "ycombinator.com"),
    ("en.wikipedia.org", "wikipedia.org"),
    ("www.google.com", "google.com"),
    ("https://github.com/foo/bar", "github.com"),
    ("producthunt.com", "producthunt.com"),
    ("bbc.co.uk", "bbc.co.uk"),                 # multi-label TLD: 2 labels
    ("www.bbc.co.uk", "bbc.co.uk"),             # multi-label TLD + subdomain
    ("shop.example.com.cn", "example.com.cn"),  # CN multi-label TLD
    ("plain", "plain"),                          # single-label fallback
])
def test_host_stem_etld_plus_one(host, expected):
    from browser_skill.memory.site_mem import host_stem
    assert host_stem(host) == expected


def test_host_stem_overrides_still_win():
    from browser_skill.memory.site_mem import host_stem
    # Spec §B.1 override list — these must beat the algorithmic eTLD+1.
    assert host_stem("mail.google.com") == "gmail"
    assert host_stem("boss.zhipin.com") == "boss-zhipin"
    assert host_stem("zhipin.com") == "boss-zhipin"


def test_find_task_path_accepts_full_host_for_renamed_bundle(tmp_bs_home,
                                                              fresh_modules):
    """`browser-skill task news.ycombinator.com/front_page` still resolves
    even though the bundled dir was renamed to ``ycombinator.com/`` —
    ``find_task_path`` retries with ``host_stem(site)`` as fallback."""
    from browser_skill.discovery import find_task_path
    p = find_task_path("news.ycombinator.com", "front_page")
    assert p.parent.parent.name == "ycombinator.com"
    assert p.name == "front_page.py"
    # eTLD+1 form must also work directly.
    p2 = find_task_path("ycombinator.com", "front_page")
    assert p2 == p


def test_site_memory_reads_legacy_short_stem(tmp_bs_home, fresh_modules):
    """A pre-v0.3.1 install wrote memory under the short-stem dir name
    (``news/`` for ``news.ycombinator.com``). After the eTLD+1 fix we
    must still be able to read it."""
    from browser_skill.memory.site_mem import (
        SiteMemory, _legacy_host_stem, host_stem,
    )
    # Sanity-check the migration scenario the test simulates.
    assert host_stem("news.ycombinator.com") == "ycombinator.com"
    assert _legacy_host_stem("news.ycombinator.com") == "news"

    legacy_dir = tmp_bs_home / "site-skills" / "news"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "memory.md").write_text(
        "---\nsite: news\n---\n\n## Notes\n- legacy memory entry\n",
        encoding="utf-8",
    )

    blob = SiteMemory("news.ycombinator.com").read()
    assert "legacy memory entry" in blob["body"]


# ----- Bug 2: scaffold validates args-schema shape -------------------


def test_scaffold_rejects_flat_string_value(tmp_bs_home, fresh_modules):
    """Pre-fix: ``{"q": "str"}`` crashed with AttributeError deep in the
    template render. Now: ValueError with the corrected shape in the
    message."""
    from browser_skill.session import Session
    from browser_skill.solidify import scaffold

    bad_spec = {
        "site": "example.com",
        "suggested_name": "demo",
        "draft_args_schema": {"q": "str"},     # ← malformed: must be dict
        "draft_run_body": "    return None\n",
        "host_hint": "example.com",
    }
    with pytest.raises(ValueError) as exc_info:
        scaffold.commit(Session(), bad_spec)
    msg = str(exc_info.value)
    assert "args schema" in msg.lower()
    assert "'type'" in msg or "type" in msg
    # The error message must teach the correct shape.
    assert "{'q':" in msg or "{'q' :" in msg


def test_scaffold_rejects_missing_type_field(tmp_bs_home, fresh_modules):
    from browser_skill.session import Session
    from browser_skill.solidify import scaffold

    bad_spec = {
        "site": "example.com",
        "suggested_name": "demo",
        "draft_args_schema": {"q": {"required": True}},  # missing 'type'
        "draft_run_body": "    return None\n",
        "host_hint": "example.com",
    }
    with pytest.raises(ValueError) as exc_info:
        scaffold.commit(Session(), bad_spec)
    assert "type" in str(exc_info.value).lower()


def test_scaffold_rejects_non_dict_schema(tmp_bs_home, fresh_modules):
    from browser_skill.session import Session
    from browser_skill.solidify import scaffold

    bad_spec = {
        "site": "example.com",
        "suggested_name": "demo",
        "draft_args_schema": ["q", "p"],   # list, not dict
        "draft_run_body": "    return None\n",
        "host_hint": "example.com",
    }
    with pytest.raises(ValueError) as exc_info:
        scaffold.commit(Session(), bad_spec)
    assert "dict" in str(exc_info.value).lower()


def test_scaffold_accepts_correct_schema(tmp_bs_home, fresh_modules):
    from browser_skill.session import Session
    from browser_skill.solidify import scaffold

    good_spec = {
        "site": "example.com",
        "suggested_name": "demo_ok",
        "draft_args_schema": {"q": {"type": "str", "required": True}},
        "draft_run_body": "    return args['q'].upper()\n",
        "host_hint": "example.com",
    }
    result = scaffold.commit(Session(), good_spec)
    assert "path" in result
    assert "demo_ok.py" in result["path"]


# ----- Bug 3: propose returns dict, never None ------------------------


def test_propose_below_threshold_returns_dict_with_reasons(tmp_bs_home,
                                                            fresh_modules):
    """A history that trips the auth/login penalty drops the score below
    threshold. Pre-fix this returned ``None``; now it returns a dict with
    ``ready=False`` and a populated ``warnings`` list."""
    from browser_skill.session import Session
    from browser_skill.solidify import propose

    sess = Session()
    sess.history = [
        _hist("goto_url('https://example.com/login')"),
        _hist("fill_input('input[name=password]', 'x')"),
    ]
    out = propose.propose(sess)
    assert isinstance(out, dict)
    assert out["ready"] is False
    assert 0.0 <= out["readiness_score"] < out["threshold"]
    assert any("auth" in w.lower() for w in out["warnings"])
    # Must expose a name_hint even on a not-ready result (UI shows it).
    assert isinstance(out["name_hint"], str)


def test_propose_empty_with_like_does_not_clone_blind(tmp_bs_home,
                                                       fresh_modules):
    """`like` without history → still ready=False, with a clear warning so
    the agent knows why."""
    from browser_skill.session import Session
    from browser_skill.solidify import propose

    out = propose.propose(Session(), like="ycombinator.com/front_page")
    assert isinstance(out, dict)
    assert out["ready"] is False
    assert any("history" in w.lower() for w in out["warnings"])


def test_propose_ready_path_carries_scaffold_seed(tmp_bs_home, fresh_modules):
    from browser_skill.session import Session
    from browser_skill.solidify import propose

    sess = Session()
    sess.history = [
        _hist("limit = 10"),
        _hist("new_tab('https://news.ycombinator.com/')"),
        _hist("rows = js('return [...document.querySelectorAll(\"tr.athing\")].slice(0, limit).map(r => r.id)')"),
        _hist("print(rows)"),
    ]
    out = propose.propose(sess, name_hint="hn_top")
    assert out["ready"] is True
    # Bug 1: site is eTLD+1, matching the bundled site_skills_starter dir.
    assert out["site"] == "ycombinator.com"
    assert "draft_run_body" in out
    assert "draft_args_schema" in out


# ----- Bug 4: remember_preference dotted-key docs --------------------


def test_remember_preference_dotted_key_writes_nested(tmp_bs_home,
                                                      fresh_modules):
    """The documented behavior: ``"a.b.c"`` writes to nested frontmatter,
    not a literal flat key. This test guards the behavior so a refactor
    can't silently flip it (which would invalidate the docstring)."""
    from browser_skill.memory.global_mem import global_memory

    mem = global_memory()
    mem.set_preference("daemon.preferred_backend", "extension", confirm=False)
    blob = mem.read()
    daemon_fm = blob.get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("preferred_backend") == "extension"
    # And the flat key form should NOT have been written.
    assert "daemon.preferred_backend" not in blob.get("frontmatter", {})


def test_remember_preference_flat_key_stays_flat(tmp_bs_home, fresh_modules):
    """No dots → top-level key. Documents the contrast for callers."""
    from browser_skill.memory.global_mem import global_memory

    mem = global_memory()
    mem.set_preference("dark_mode", True, confirm=False)
    blob = mem.read()
    assert blob.get("frontmatter", {}).get("dark_mode") is True


def test_remember_preference_docstring_explains_dotted_key():
    """Docstring must teach the nested-write contract — agents shouldn't
    have to read source or run ``memory show`` to discover it (Bug 4)."""
    from browser_skill.primitives.site import remember_preference
    doc = remember_preference.__doc__ or ""
    assert "Dotted" in doc or "dotted" in doc
    # Should walk through the example.
    assert "daemon" in doc and "preferred_backend" in doc
