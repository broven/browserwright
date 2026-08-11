"""`browserwright session attach-active` — the agent-side wrapper.

The wrapper shells out to `browserwright-daemon attach-active --json` (the
daemon CLI owns the verb); these tests pin the Layer-2 contract: session
resolution via `-s`/`--session`/`BD_SESSION`, JSON passthrough with `--json`,
the friendly human line by default, and the occupied-refusal error
propagating verbatim with a non-zero exit code.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from browserwright import cli, session_create
from browserwright import session_registry as reg


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)


@pytest.fixture
def _fake_daemon_cli(monkeypatch):
    def _install(returncode=0, stdout="", stderr=""):
        proc = subprocess.CompletedProcess(
            ["browserwright-daemon", "attach-active"], returncode,
            stdout=stdout, stderr=stderr)
        monkeypatch.setattr(session_create.subprocess, "run",
                            lambda *a, **k: proc)
    return _install


_PAYLOAD = {"sessionId": "fab", "targetId": "T1", "tabId": 17,
            "url": "https://example.com/x", "title": "Example", "groupId": 9}


def _new_extension_session() -> str:
    assert cli._cmd_session(["new", "--backend=extension", "--name=t"]) == 0
    rows = reg.list_all()
    assert len(rows) == 1, rows
    return str(rows[0]["id"])


def test_attach_active_prints_human_line(tmp_bs_home, capsys, _fake_daemon_cli):
    sid = _new_extension_session()
    _fake_daemon_cli(stdout=json.dumps(_PAYLOAD))
    assert cli._cmd_session(["attach-active", "--session", sid]) == 0
    out = capsys.readouterr().out
    assert "adopted the active tab" in out
    assert "Example" in out and "https://example.com/x" in out
    assert "17" in out and "9" in out


def test_attach_active_json_passthrough(tmp_bs_home, capsys, _fake_daemon_cli):
    sid = _new_extension_session()
    capsys.readouterr()  # drain the `session new` bare-id line
    _fake_daemon_cli(stdout=json.dumps(_PAYLOAD))
    assert cli._cmd_session(["attach-active", "-s", sid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _PAYLOAD


def test_attach_active_refusal_propagates_verbatim(
        tmp_bs_home, capsys, _fake_daemon_cli):
    sid = _new_extension_session()
    msg = ("focused tab is in a tab group (groupId=5); refusing to take it "
           "over. Drag the tab out of the group first, then retry.")
    _fake_daemon_cli(returncode=1, stderr=msg + "\n")
    rc = cli._cmd_session(["attach-active", "--session", sid])
    assert rc == 3  # BrowserwrightError default exit code
    err = capsys.readouterr().err
    assert "focused tab is in a tab group" in err
    assert "Drag the tab out" in err


def test_attach_active_resolves_bd_session_env(
        tmp_bs_home, capsys, _fake_daemon_cli, monkeypatch):
    sid = _new_extension_session()
    _fake_daemon_cli(stdout=json.dumps(_PAYLOAD))
    monkeypatch.setenv("BD_SESSION", sid)
    assert cli._cmd_session(["attach-active"]) == 0
    assert "adopted the active tab" in capsys.readouterr().out
