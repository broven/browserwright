"""v0.3.1 — fixes for 4 bugs surfaced by the agent-sdk-tester E2E run.

Each test names the bug. All four bugs are independent.
"""
from __future__ import annotations

import pytest


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
    from browserwright.memory.site_mem import host_stem
    assert host_stem(host) == expected


def test_host_stem_overrides_still_win():
    from browserwright.memory.site_mem import host_stem
    # Spec §B.1 override list — these must beat the algorithmic eTLD+1.
    assert host_stem("mail.google.com") == "gmail"
    assert host_stem("boss.zhipin.com") == "boss-zhipin"
    assert host_stem("zhipin.com") == "boss-zhipin"


def test_find_task_path_accepts_full_host_for_renamed_bundle(tmp_bs_home,
                                                              fresh_modules):
    """`browserwright task news.ycombinator.com/front_page` still resolves
    even though the bundled dir was renamed to ``ycombinator.com/`` —
    ``find_task_path`` retries with ``host_stem(site)`` as fallback."""
    from browserwright.discovery import find_task_path
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
    from browserwright.memory.site_mem import (
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


# ----- Bug 4: remember_preference dotted-key docs --------------------


def test_remember_preference_dotted_key_writes_nested(tmp_bs_home,
                                                      fresh_modules):
    """The documented behavior: ``"a.b.c"`` writes to nested frontmatter,
    not a literal flat key. This test guards the behavior so a refactor
    can't silently flip it (which would invalidate the docstring)."""
    from browserwright.memory.global_mem import global_memory

    mem = global_memory()
    mem.set_preference("daemon.preferred_backend", "extension", confirm=False)
    blob = mem.read()
    daemon_fm = blob.get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("preferred_backend") == "extension"
    # And the flat key form should NOT have been written.
    assert "daemon.preferred_backend" not in blob.get("frontmatter", {})


def test_remember_preference_flat_key_stays_flat(tmp_bs_home, fresh_modules):
    """No dots → top-level key. Documents the contrast for callers."""
    from browserwright.memory.global_mem import global_memory

    mem = global_memory()
    mem.set_preference("dark_mode", True, confirm=False)
    blob = mem.read()
    assert blob.get("frontmatter", {}).get("dark_mode") is True


def test_remember_preference_docstring_explains_dotted_key():
    """Docstring must teach the nested-write contract — agents shouldn't
    have to read source or run ``memory show`` to discover it (Bug 4)."""
    from browserwright.primitives.site import remember_preference
    doc = remember_preference.__doc__ or ""
    assert "Dotted" in doc or "dotted" in doc
    # Should walk through the example.
    assert "daemon" in doc and "preferred_backend" in doc
