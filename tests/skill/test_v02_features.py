"""v0.2 feature unit tests: selftest cache, OUTPUT_SCHEMA, memory forget,
project-level site-skills, solidify by analogy."""
import os
import shutil
import time
from pathlib import Path

import pytest


# ---- selftest cache ---------------------------------------------------


def test_selftest_cache_pass_then_skip(tmp_bs_home, fresh_modules):
    from browserwright import selftest_cache

    tdir = tmp_bs_home / "fake-task"
    tdir.mkdir()
    task_path = tdir / "demo.py"
    task_path.write_text("# v1")
    assert selftest_cache.is_fresh("siteA", "demo", task_path) is False
    selftest_cache.remember_pass("siteA", "demo", task_path)
    assert selftest_cache.is_fresh("siteA", "demo", task_path) is True
    # editing the file invalidates.
    task_path.write_text("# v2")
    assert selftest_cache.is_fresh("siteA", "demo", task_path) is False


def test_selftest_cache_env_bypass(tmp_bs_home, monkeypatch, fresh_modules):
    from browserwright import selftest_cache

    task_path = tmp_bs_home / "t.py"
    task_path.write_text("# x")
    selftest_cache.remember_pass("siteA", "demo", task_path)
    monkeypatch.setenv("BS_SELFTEST_NOCACHE", "1")
    assert selftest_cache.is_fresh("siteA", "demo", task_path) is False


def test_selftest_fail_not_cached_as_skip(tmp_bs_home, fresh_modules):
    from browserwright import selftest_cache

    task_path = tmp_bs_home / "t.py"
    task_path.write_text("# x")
    selftest_cache.remember_fail("siteA", "demo", task_path, "drifted")
    # Failures do not short-circuit the next call; only successes do.
    assert selftest_cache.is_fresh("siteA", "demo", task_path) is False


# ---- OUTPUT_SCHEMA ---------------------------------------------------


def test_output_schema_simple_pass():
    from browserwright.output_schema import validate

    schema = {"type": "array", "items": {
        "type": "object",
        "required": ["title", "url"],
        "properties": {
            "title": {"type": "string"},
            "url": {"type": "string"},
            "score": {"type": "integer"},
        },
    }}
    validate([{"title": "a", "url": "u", "score": 1}], schema)


def test_output_schema_missing_required_fails():
    from browserwright.output_schema import OutputSchemaError, validate

    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}
    with pytest.raises(OutputSchemaError) as exc:
        validate({"y": 1}, schema)
    assert ".x" in str(exc.value)


def test_output_schema_wrong_type():
    from browserwright.output_schema import OutputSchemaError, validate

    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(OutputSchemaError):
        validate({"n": "not-an-int"}, schema)


def test_output_schema_enum():
    from browserwright.output_schema import OutputSchemaError, validate

    schema = {"enum": ["a", "b"]}
    validate("a", schema)
    with pytest.raises(OutputSchemaError):
        validate("c", schema)


def test_output_schema_additional_properties_false():
    from browserwright.output_schema import OutputSchemaError, validate

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"x": {"type": "string"}},
    }
    with pytest.raises(OutputSchemaError):
        validate({"x": "ok", "y": "bad"}, schema)


# ---- memory forget / replace ----------------------------------------


def test_site_memory_forget_dry_run_then_commit(tmp_bs_home, fresh_modules):
    from browserwright.memory import site_memory

    mem = site_memory("github.com")
    mem.append("first note")
    mem.append("second note")
    mem.append("keep this")
    # Dry-run: nothing changes.
    matches = mem.forget("note", confirm=True)
    assert len(matches) == 2
    body = mem.read()["body"]
    assert "first note" in body
    # Commit.
    removed = mem.forget("note", confirm=False)
    assert len(removed) == 2
    body = mem.read()["body"]
    assert "first note" not in body
    assert "second note" not in body
    assert "keep this" in body


def test_site_memory_forget_no_match(tmp_bs_home, fresh_modules):
    from browserwright.memory import site_memory

    mem = site_memory("nothing.com")
    mem.append("hello")
    assert mem.forget("nope", confirm=True) == []


def test_global_memory_forget(tmp_bs_home, fresh_modules):
    from browserwright.memory import global_memory

    mem = global_memory()
    mem.append("delete me")
    mem.append("retain")
    matches = mem.forget("delete", confirm=True)
    assert len(matches) == 1
    mem.forget("delete", confirm=False)
    assert "delete me" not in mem.read()["body"]
    assert "retain" in mem.read()["body"]


# ---- project-level site-skills ---------------------------------------


def test_project_level_overrides_bundled(tmp_path, monkeypatch):
    """A project ./site-skills/google/tasks/search.py wins over the bundled
    starter when both define the same site/task name."""
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    monkeypatch.setenv("BS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(proj_root)

    (proj_root / "site-skills" / "google" / "tasks").mkdir(parents=True)
    (proj_root / "site-skills" / "google" / "tasks" / "search.py").write_text(
        '"""project search override."""\nARGS = {}\n'
        'OUTPUT = "anything"\nTAGS = ["override"]\n'
        'def selftest(): return True\n'
        'def run(args, ctx=None): return "from-project"\n',
        encoding="utf-8",
    )

    # Force module re-import so site_skills_roots() sees the new cwd.
    import importlib
    import sys
    for name in list(sys.modules):
        if name.startswith("browserwright"):
            del sys.modules[name]
    from browserwright.discovery import find_task_path, rebuild_index

    rebuild_index()
    path = find_task_path("google", "search")
    assert "proj" in str(path)


# ---- solidify by analogy ----------------------------------------------


def test_propose_like_seeds_from_donor(tmp_bs_home, fresh_modules):
    from browserwright.session import Session
    from browserwright.solidify import propose
    # Build a fake donor task in BS_HOME.
    donor_dir = tmp_bs_home / "site-skills" / "example" / "tasks"
    donor_dir.mkdir(parents=True)
    (donor_dir / "search.py").write_text(
        '"""donor."""\nARGS = {"q": {"type": "str", "required": True}}\n'
        'OUTPUT = "list"\nTAGS = []\n'
        'def selftest(): return True\n'
        'def run(args, ctx=None):\n'
        '    new_tab(f"https://example.com/?q={args[\'q\']}")\n'
        '    return js("return 1")\n',
        encoding="utf-8",
    )
    # Force re-import so site_skills_roots() picks up our temp dir.
    import sys
    for k in list(sys.modules):
        if k.startswith("browserwright"):
            del sys.modules[k]
    from browserwright.solidify import propose
    from browserwright.session import Session
    sess = Session()
    # Inject a small history so propose passes its readiness threshold.
    sess.history = [
        {"code": "q = 'foo'", "ok": True, "stdout": "", "result": None, "exception": None, "ts": 0},
        {"code": "new_tab('https://demo.org/?q=' + q)", "ok": True, "stdout": "",
         "result": None, "exception": None, "ts": 0},
        {"code": "results = js('return Array.from(document.querySelectorAll(\"a\")).map(a => a.href)')",
         "ok": True, "stdout": "", "result": None, "exception": None, "ts": 0},
    ]
    out = propose.propose(sess, name_hint="search_demo", like="example/search")
    assert out is not None
    assert "donor" in out
    assert out["donor"] == "example/search"
    # URL host should have been swapped from example.com to demo.org (the
    # current session's host hint from history).
    assert "demo.org" in out["draft_run_body"]
    assert "example.com" not in out["draft_run_body"]
