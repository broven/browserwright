"""Focused offline coverage for REPL/misc low-coverage modules."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest


def _stub_inline_session(monkeypatch):
    """Bind inline.run_code to a fake resolved session without opening a daemon."""
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


def test_inline_run_code_empty_input_prints_usage(capsys):
    from browserwright.repl import inline

    rc = inline.run_code(" \n\t\n", session_id="s-test")

    assert rc == 1
    assert "browserwright -s <session-id> -e" in capsys.readouterr().err


def test_inline_run_replays_stdout_before_browserwright_error(monkeypatch, capsys):
    from browserwright.errors import BrowserwrightError
    from browserwright.repl import inline

    captured = _stub_inline_session(monkeypatch)
    rc = inline.run_code(
        "print('before')\nraise BrowserwrightError('boom', fix='retry')\n",
        session_id="s-test",
    )
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
    rc = inline.run_code("print('leaving')\nraise SystemExit('done')\n", session_id="s-test")

    assert rc == 0
    assert capsys.readouterr().out == "leaving\n"


def test_inline_run_unexpected_exception_returns_traceback(monkeypatch, capsys):
    from browserwright.repl import inline

    _stub_inline_session(monkeypatch)
    rc = inline.run_code("print('pre')\n1 / 0\n", session_id="s-test")
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


def test_snapshot_default_max_chars_is_less_aggressive():
    from browserwright.repl import snapshot as snapshot_mod

    class _Page:
        def aria_snapshot(self, *, mode):
            assert mode == "ai"
            return "- root\n" + ("  - button \"Save\" [ref=e1]\n" * 700)

    snap = snapshot_mod.make_snapshot(type("Handle", (), {"page": _Page()})())

    assert len(snap()) > 6000
    assert len(snap()) <= 20000 + len(snapshot_mod._TRUNC_MARKER) + 2


def test_executor_control_plane_uses_browserwright_session_param():
    from browserwright._executor import client as exec_client

    calls = []

    class _CDP:
        def send(self, method, **kwargs):
            calls.append((method, kwargs))
            return {"exec_sock": "/tmp/bw-exec.sock"}

    sess = type("Sess", (), {
        "session_record": {"id": "rdp-7", "backend": "rdp"},
        "cdp": _CDP(),
    })()

    assert exec_client.ensure_executor(sess) == "/tmp/bw-exec.sock"
    assert calls == [(
        "BrowserwrightDaemon.ensureExecutor",
        {"bsSession": "rdp-7"},
    )]


def test_discovery_iter_site_dirs_precedence_and_filters(monkeypatch, tmp_path):
    from browserwright import discovery

    project = tmp_path / "project"
    home = tmp_path / "home"
    bundled = tmp_path / "bundled"
    for root in (project, home, bundled):
        root.mkdir()
    (project / "example.com" / "tasks").mkdir(parents=True)
    (home / "example.com" / "tasks").mkdir(parents=True)
    (home / "memsite.test").mkdir()
    (home / "memsite.test" / "memory.md").write_text("# mem\n", encoding="utf-8")
    (bundled / "bundled.test").mkdir()
    (bundled / "bundled.test" / "SKILL.md").write_text("# ignored without tasks/memory\n", encoding="utf-8")

    monkeypatch.setattr(discovery, "site_skills_roots", lambda: [project, home])
    monkeypatch.setattr(discovery, "_bundled_root", lambda: bundled)

    dirs = discovery._iter_site_dirs()

    assert [d.name for d in dirs] == ["example.com", "memsite.test"]
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
    sess._closed = False
    sess._closed_reason = None

    sess._read_loop()

    assert sess._closed is True
    assert sess._closed_reason is None
    # Responses route into inflight; events (frames without an id) are dropped.
    assert sess._inflight[7]["result"] == {"ok": True}
