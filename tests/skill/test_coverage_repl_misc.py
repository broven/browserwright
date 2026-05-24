"""Focused offline coverage for REPL/misc low-coverage modules.

These tests deliberately stay in one file so the curated unit set can pick a
small number of high-signal nodeids without pulling in browser/daemon work.
"""
from __future__ import annotations

import io
import json
import socket
import threading
from collections import deque
from pathlib import Path

import pytest


def _stub_inline_session(monkeypatch):
    """Bind inline.run to a fake resolved session without opening a daemon."""
    from browserwright import session, session_ctx

    record = {"id": "s-test", "backend": "extension", "owner": "attach"}
    captured = {}

    class DummySession:
        def __init__(self, *, record):
            self.record = record

    monkeypatch.setattr(session_ctx, "resolve_session", lambda explicit=None: record)
    monkeypatch.setattr(session, "Session", DummySession)
    monkeypatch.setattr(session, "set_session", lambda sess: captured.setdefault("session", sess))
    return captured


def test_inline_run_empty_input_prints_usage(capsys):
    from browserwright.repl import inline

    rc = inline.run(io.StringIO(" \n\t\n"))

    assert rc == 1
    assert "browserwright <<'PY'" in capsys.readouterr().err


def test_inline_run_replays_stdout_before_browserwright_error(monkeypatch, capsys):
    from browserwright.errors import BrowserwrightError
    from browserwright.repl import inline

    captured = _stub_inline_session(monkeypatch)
    rc = inline.run(io.StringIO("print('before')\nraise BrowserwrightError('boom', fix='retry')\n"))
    streams = capsys.readouterr()

    assert rc == 3
    assert streams.out == "before\n"
    payload = json.loads(streams.err)
    assert payload["type"] == "BrowserwrightError"
    assert payload["fix"] == "retry"
    assert captured["session"].record["id"] == "s-test"


def test_inline_run_system_exit_non_integer_is_success(monkeypatch, capsys):
    from browserwright.repl import inline

    _stub_inline_session(monkeypatch)
    rc = inline.run(io.StringIO("print('leaving')\nraise SystemExit('done')\n"))

    assert rc == 0
    assert capsys.readouterr().out == "leaving\n"


def test_inline_run_unexpected_exception_returns_traceback(monkeypatch, capsys):
    from browserwright.repl import inline

    _stub_inline_session(monkeypatch)
    rc = inline.run(io.StringIO("print('pre')\n1 / 0\n"))
    streams = capsys.readouterr()

    assert rc == 3
    assert streams.out == "pre\n"
    assert "ZeroDivisionError" in streams.err
    assert "<inline>" in streams.err


def test_namespace_skips_same_object_shadow_without_warning(tmp_bs_home, capsys):
    from browserwright.repl import _namespace

    (tmp_bs_home / "agent_helpers.py").write_text("import json\n", encoding="utf-8")
    g = _namespace.build_globals()

    assert g["json"].dumps({"ok": True}) == '{"ok": true}'
    assert capsys.readouterr().err == ""


def test_skill_doc_falls_back_when_cli_help_import_fails(monkeypatch):
    from browserwright import skill_doc

    real_import = __import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and name == "cli" and fromlist == ("HELP",):
            raise RuntimeError("cli unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(skill_doc.Path, "is_file", lambda self: False)
    monkeypatch.setattr("builtins.__import__", guarded_import)

    assert skill_doc._curated_header() == "browserwright — Layer 2 of the browser stack."


def test_skill_doc_signature_uses_ellipsis_for_uninspectable_callable(monkeypatch):
    from browserwright import skill_doc

    def broken_signature(_obj):
        raise ValueError("no signature")

    monkeypatch.setattr(skill_doc.inspect, "signature", broken_signature)

    assert skill_doc._signature("mystery", object()) == "mystery(...)"


def test_discovery_iter_site_dirs_precedence_and_filters(monkeypatch, tmp_path):
    from browserwright import discovery

    project = tmp_path / "project"
    home = tmp_path / "home"
    subscription = tmp_path / "sub"
    bundled = tmp_path / "bundled"
    for root in (project, home, subscription, bundled):
        root.mkdir()
    (project / "example.com" / "tasks").mkdir(parents=True)
    (home / "example.com" / "tasks").mkdir(parents=True)
    (subscription / "subsite.test").mkdir()
    (subscription / "subsite.test" / "memory.md").write_text("# sub\n", encoding="utf-8")
    (bundled / "bundled.test").mkdir()
    (bundled / "bundled.test" / "SKILL.md").write_text("# ignored without tasks/memory\n", encoding="utf-8")

    monkeypatch.setattr(discovery, "site_skills_roots", lambda: [project, home])
    monkeypatch.setattr(discovery, "_bundled_root", lambda: bundled)
    monkeypatch.setattr(
        "browserwright.subscriptions.iter_subscription_site_roots",
        lambda: [subscription],
    )

    dirs = discovery._iter_site_dirs()

    assert [d.name for d in dirs] == ["example.com", "subsite.test"]
    assert dirs[0].parent == project


def test_discovery_load_task_meta_reports_import_error(tmp_path):
    from browserwright.discovery import _load_task_meta

    task = tmp_path / "broken.py"
    task.write_text("raise RuntimeError('bad import')\n", encoding="utf-8")

    assert _load_task_meta(task) == {"name": "broken", "load_error": True}


def test_discovery_load_site_entry_parses_skill_memory_and_task(tmp_path):
    from browserwright.discovery import _load_site_entry

    site = tmp_path / "docs.example"
    (site / "tasks").mkdir(parents=True)
    (site / "SKILL.md").write_text("# Docs Helper\nmore\n", encoding="utf-8")
    (site / "memory.md").write_text(
        "---\nhost_patterns:\n  - docs.example\naliases:\n  - docs\n---\nnotes\n",
        encoding="utf-8",
    )
    (site / "tasks" / "lookup.py").write_text(
        '"""Lookup docs."""\n'
        'ARGS = {"q": {"type": "str"}}\n'
        'OUTPUT = "dict"\n'
        'TAGS = ["docs"]\n'
        'REQUIRES_LOGIN = True\n'
        'LAST_VERIFIED = "2026-05-01"\n',
        encoding="utf-8",
    )

    entry = _load_site_entry(site)

    assert entry["site"] == "docs.example"
    assert entry["host_patterns"] == ["docs.example"]
    assert entry["aliases"] == ["docs"]
    assert entry["description_first_line"] == "Docs Helper"
    expected_task = {
        "name": "lookup",
        "desc": "Lookup docs.",
        "output": "dict",
        "tags": ["docs"],
        "requires_login": True,
        "last_verified": "2026-05-01",
    }
    for key, value in expected_task.items():
        assert entry["tasks"][0][key] == value
    assert Path(entry["tasks"][0]["path"]).name == "lookup.py"


def test_discovery_load_index_rebuilds_invalid_json(monkeypatch, tmp_bs_home):
    from browserwright import discovery
    from browserwright.memory.site_mem import site_skills_root

    target = site_skills_root() / "index.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    rebuilt = {"version": 99, "sites": []}
    monkeypatch.setattr(discovery, "rebuild_index", lambda: rebuilt)

    assert discovery._load_index() == rebuilt


def test_output_schema_integer_rejects_bool_with_path():
    from browserwright.output_schema import OutputSchemaError, validate

    with pytest.raises(OutputSchemaError) as exc:
        validate(
            {"items": [{"count": True}]},
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"count": {"type": "integer"}},
                        },
                    },
                },
            },
            site="s",
            task="t",
        )

    assert exc.value.path == "$.items[0].count"
    assert "got bool" in str(exc.value)


def test_output_schema_nullable_and_unknown_type_are_permissive():
    from browserwright.output_schema import validate

    validate(None, {"type": "string", "nullable": True})
    validate({"anything": object()}, {"type": "made-up"})
    validate("value", "not-a-schema")


def test_output_schema_nested_array_enum_path():
    from browserwright.output_schema import OutputSchemaError, validate

    with pytest.raises(OutputSchemaError) as exc:
        validate([["ok"], ["bad"]], {"type": "array", "items": {"type": "array", "items": {"enum": ["ok"]}}})

    assert exc.value.path == "$[1][0]"
    assert "not in enum" in exc.value.msg_short


def test_errors_serialize_reprs_unjsonable_attributes():
    from browserwright.errors import AuthWall, BrowserwrightError, serialize

    exc = AuthWall("https://login.example", signals={object()})
    payload = serialize(exc)

    assert payload["signals"].startswith("[")
    assert "object at" in payload["signals"]
    assert serialize(BrowserwrightError("plain")) == {
        "type": "BrowserwrightError",
        "msg": "plain",
        "fix": "",
    }


def test_cdp_unix_socket_adapter_ignores_tcp_options_and_delegates():
    from browserwright.cdp import _UnixSocketAdapter

    class RawSocket:
        def __init__(self):
            self.calls = []
            self.marker = "raw"

        def setsockopt(self, level, optname, value):
            self.calls.append((level, optname, value))
            return "delegated"

    raw = RawSocket()
    adapter = _UnixSocketAdapter(raw)

    assert adapter.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True) is None
    assert raw.calls == []
    assert adapter.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) == "delegated"
    assert raw.calls == [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    assert adapter.marker == "raw"


def test_cdp_read_loop_routes_responses_and_events():
    from browserwright.cdp import CDPSession

    sess = object.__new__(CDPSession)
    sess._ws = iter([
        "not json",
        json.dumps({"id": 7, "result": {"ok": True}}),
        json.dumps({"method": "Browser.event", "params": {"x": 1}}),
        json.dumps({"sessionId": "sid-1", "method": "Page.loadEventFired"}),
    ])
    sess._lock = threading.Lock()
    sess._inflight = {7: {}}
    sess._inflight_cv = threading.Condition(sess._lock)
    sess._events = {None: deque(maxlen=4)}
    sess._closed = False
    sess._closed_reason = None

    sess._read_loop()

    assert sess._closed is True
    assert sess._closed_reason is None
    assert sess._inflight[7]["result"] == {"ok": True}
    assert list(sess._events[None]) == [
        {"method": "Browser.event", "params": {"x": 1}, "sessionId": None}
    ]
    assert list(sess._events["sid-1"]) == [
        {"method": "Page.loadEventFired", "params": {}, "sessionId": "sid-1"}
    ]


def test_cdp_drain_events_clears_existing_buffer_and_missing_is_empty():
    from browserwright.cdp import CDPSession

    sess = object.__new__(CDPSession)
    sess._lock = threading.Lock()
    sess._events = {"sid": deque([{"method": "A"}, {"method": "B"}])}

    assert sess.drain_events("missing") == []
    assert sess.drain_events("sid") == [{"method": "A"}, {"method": "B"}]
    assert sess.drain_events("sid") == []
