"""Focused offline coverage for task requests on the resident executor."""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from browserwright._executor import client, protocol
from browserwright._executor.process import _Worker
from browserwright.repl import inline


def _connected_worker() -> _Worker:
    worker = _Worker("task-session")
    worker._connected = True
    worker._page = object()
    worker._context = object()
    worker._snapshot = lambda: "snapshot"
    return worker


def test_task_request_and_result_roundtrip():
    request = protocol.ExecuteRequest(
        code="",
        timeout_ms=1234,
        env={"TASK_TOKEN": "one-call"},
        executor_id="executor-task",
        task=protocol.TaskEnvelope(
            site="example.com",
            name="check",
            args={"count": 2, "filters": ["open", None]},
            isolated=True,
        ),
    )

    decoded = protocol.ExecuteRequest.from_dict(request.to_dict())

    assert decoded.task == request.task
    assert decoded.env == {"TASK_TOKEN": "one-call"}
    response = protocol.ExecuteResponse.from_dict(
        protocol.ExecuteResponse(task_result_json='{"ok": true}').to_dict()
    )
    assert json.loads(response.task_result_json or "") == {"ok": True}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": "print('ambiguous')",
            "task": {"site": "site", "name": "name"},
        },
        {
            "code": "",
            "task": {"site": "", "name": "name"},
        },
        {
            "code": "",
            "task": {"site": "site", "name": "name", "isolated": "yes"},
        },
        {
            "code": "",
            "task": {"site": "site", "name": "name", "args": {"bad": object()}},
        },
    ],
)
def test_task_request_validation_rejects_invalid_envelopes(payload):
    with pytest.raises(ValueError):
        protocol.ExecuteRequest.from_dict(payload)


def test_task_response_rejects_invalid_result_json():
    with pytest.raises(ValueError, match="valid JSON"):
        protocol.ExecuteResponse.from_dict({"task_result_json": "{broken"})


def test_task_client_uses_shared_executor_lease_and_request(monkeypatch):
    captured = {}

    class _Connection:
        def close(self):
            captured["closed"] = True

    class _Session:
        session_record: ClassVar = {"id": "task-session"}

    monkeypatch.setattr(
        client.reg, "touch", lambda sid: captured.setdefault("sid", sid)
    )
    monkeypatch.setattr(
        client,
        "_ensure_executor_lease",
        lambda _sess: client.ExecutorLease("/tmp/task.sock", "executor-task"),
    )
    monkeypatch.setattr(client, "_connect", lambda *_args, **_kwargs: _Connection())
    monkeypatch.setattr(
        client,
        "send_message",
        lambda _conn, payload: captured.setdefault("payload", payload),
    )
    monkeypatch.setattr(
        client,
        "recv_message",
        lambda _conn: protocol.ExecuteResponse(
            task_result_json='{"ok": true}'
        ).to_dict(),
    )

    response = client.run_task_on_executor(
        _Session(),
        "example.com",
        "check",
        args={"count": 2},
        isolated=True,
        env={"TASK_TOKEN": "one-call"},
    )

    assert json.loads(response.task_result_json or "") == {"ok": True}
    assert captured["sid"] == "task-session"
    assert captured["closed"] is True
    assert captured["payload"]["executor_id"] == "executor-task"
    assert captured["payload"]["env"] == {"TASK_TOKEN": "one-call"}
    assert captured["payload"]["task"] == {
        "site": "example.com",
        "name": "check",
        "args": {"count": 2},
        "isolated": True,
    }


def test_worker_task_request_borrows_live_surface(monkeypatch):
    from browserwright import task_runner

    worker = _connected_worker()
    captured = {}

    def fake_run(site, name, *, browser_surface, isolated, **kwargs):
        captured.update(
            site=site,
            name=name,
            surface=browser_surface,
            isolated=isolated,
            kwargs=kwargs,
        )
        print("task log")
        return {"ok": True}

    monkeypatch.setattr(task_runner, "_run_task_on_surface", fake_run)
    response = worker._execute(
        protocol.ExecuteRequest(
            code="",
            task=protocol.TaskEnvelope(
                site="example.com",
                name="check",
                args={"count": 2},
                isolated=True,
            ),
        )
    )

    assert response.error is None
    assert response.console == "task log\n"
    assert response.return_value == "{'ok': True}"
    assert json.loads(response.task_result_json or "") == {"ok": True}
    assert captured["surface"].page is worker._page
    assert captured["surface"].context is worker._context
    assert captured["surface"].snapshot is worker._snapshot
    assert captured["isolated"] is True
    assert captured["kwargs"] == {"count": 2}


def test_inline_run_task_routes_and_uses_executor_wrapper(monkeypatch):
    from browserwright import task_runner

    assert inline._touches_executor_surface(
        compile("run_task('example.com/check')", "<test>", "exec")
    )
    worker = _connected_worker()
    captured = {}

    def fake_run(site, name, *, browser_surface, isolated, **kwargs):
        captured.update(
            site=site,
            name=name,
            surface=browser_surface,
            isolated=isolated,
            kwargs=kwargs,
        )
        return {"count": kwargs["count"]}

    monkeypatch.setattr(task_runner, "_run_task_on_surface", fake_run)
    response = worker._execute(
        protocol.ExecuteRequest("print(run_task('example.com/check', count=3))")
    )

    assert response.error is None
    assert response.console == "{'count': 3}\n"
    assert captured["site"] == "example.com"
    assert captured["name"] == "check"
    assert captured["surface"].page is worker._page
    assert captured["isolated"] is False
