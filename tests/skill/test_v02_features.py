"""v0.2 feature unit tests: OUTPUT_SCHEMA, memory forget,
project-level site-skills."""
import pytest


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
