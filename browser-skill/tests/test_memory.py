"""Memory tier tests (global frontmatter, site append, redaction, lazy-create)."""
import pytest

from browser_skill.errors import NeedsUserConfirm


def test_global_set_preference_requires_confirm(tmp_bs_home, fresh_modules):
    from browser_skill.memory import global_memory

    with pytest.raises(NeedsUserConfirm):
        global_memory().set_preference("daemon.preferred_backend", "extension")


def test_global_set_preference_writes(tmp_bs_home, fresh_modules):
    from browser_skill.memory import global_memory, read_daemon_preferred_backend

    res = global_memory().set_preference(
        "daemon.preferred_backend", "extension", confirm=False
    )
    assert res["value"] == "extension"
    assert read_daemon_preferred_backend() == "extension"


def test_global_preference_history(tmp_bs_home, fresh_modules):
    from browser_skill.memory import global_memory, read_daemon_preferred_backend

    global_memory().set_preference("daemon.preferred_backend", "extension", confirm=False)
    global_memory().set_preference("daemon.preferred_backend", "autoconnect", confirm=False)
    assert read_daemon_preferred_backend() == "autoconnect"
    body = global_memory().read()
    daemon = body["frontmatter"]["daemon"]
    # old value preserved in notes (spec §C.3 exception clause).
    assert "extension" in str(daemon.get("notes", ""))


def test_site_bootstrap_lazy_create(tmp_bs_home, fresh_modules):
    from browser_skill.memory import bootstrap_site, site_dir

    out = bootstrap_site("producthunt.com")
    assert out.exists()
    assert (out / "memory.md").exists()
    assert (out / "SKILL.md").exists()
    assert (out / "tasks").is_dir()


def test_site_append_writes_to_notes(tmp_bs_home, fresh_modules):
    from browser_skill.memory import site_memory

    mem = site_memory("github.com")
    mem.append("first observation")
    mem.append("second observation")
    body = mem.read()["body"]
    assert "first observation" in body
    assert "second observation" in body
    assert "## Notes" in body


def test_site_append_to_known_traps(tmp_bs_home, fresh_modules):
    from browser_skill.memory import site_memory

    mem = site_memory("example.com")
    mem.append(".foo selector failed, use .bar", section="Known traps")
    body = mem.read()["body"]
    assert "## Known traps" in body
    assert ".bar" in body


def test_redaction_blocks_high_entropy(tmp_bs_home, fresh_modules):
    from browser_skill.memory.site_mem import RedactionRejected, SiteMemory

    mem = SiteMemory("github.com")
    with pytest.raises(RedactionRejected):
        mem.append("Bearer abcdefghijklmnopqrstuvwxyz1234567890")
    with pytest.raises(RedactionRejected):
        mem.append("/Users/alice/secrets/key.pem")


def test_host_stem_overrides():
    from browser_skill.memory.site_mem import host_stem

    # Bug 1 (v0.3.1): host_stem now returns eTLD+1 — see
    # tests/test_e2e_bugs_v031.py for full coverage.
    assert host_stem("https://www.google.com/search") == "google.com"
    assert host_stem("zhipin.com") == "boss-zhipin"
    assert host_stem("mail.google.com") == "gmail"
