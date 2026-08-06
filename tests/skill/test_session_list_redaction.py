"""Session records became credential-bearing in #38 — don't print the secret.

An external browser's CDP URL routinely carries a reusable token in its
userinfo or query string. Before #38 that value lived in a daemon's process
environment; now it lives in `$BS_HOME/sessions/ledger.json`, which means every
path that prints a record is a new disclosure surface.
"""
from __future__ import annotations

import json

import pytest

from browserwright import cli, session_create
from browserwright import session_registry as reg

TOKEN_URL = "wss://user:s3cr3t@cloud.example.com:443/cdp?apiKey=deadbeef&x=1"


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)


def _attached(tmp_bs_home) -> str:
    assert cli._cmd_session(
        ["new", "--backend=cdp", "--name=cloud", f"--attach={TOKEN_URL}"]) == 0
    return reg.list_all()[0]["id"]


def test_session_list_json_does_not_print_the_token(tmp_bs_home, capsys):
    _attached(tmp_bs_home)
    capsys.readouterr()

    assert cli._cmd_session(["list", "--json"]) == 0
    out = capsys.readouterr().out

    assert "s3cr3t" not in out
    assert "deadbeef" not in out
    # Still useful: you can tell which endpoint the session is on.
    assert "cloud.example.com" in out
    assert json.loads(out)[0]["workspace"]["url"].startswith("wss://")


def test_whoami_omits_the_workspace_entirely(tmp_bs_home, capsys):
    sid = _attached(tmp_bs_home)
    capsys.readouterr()

    cli._cmd_whoami(["--session", sid])
    out = capsys.readouterr().out

    assert "workspace" not in out
    assert "s3cr3t" not in out


def test_the_ledger_itself_keeps_the_real_url(tmp_bs_home):
    """Redaction is for output only — the daemon still has to dial the thing."""
    sid = _attached(tmp_bs_home)

    assert reg.get(sid)["workspace"]["url"] == TOKEN_URL


def test_redacted_leaves_non_url_records_untouched(tmp_bs_home):
    port_row = {"id": "1", "backend": "cdp", "workspace": {"port": 9222}}
    ext_row = {"id": "2", "backend": "extension", "workspace": None}

    assert reg.redacted(port_row) == port_row
    assert reg.redacted(ext_row) == ext_row
    assert reg.redacted(None) is None


def test_redacted_does_not_mutate_the_input(tmp_bs_home):
    """A redactor that edits in place would corrupt the caller's live record."""
    row = {"id": "1", "backend": "cdp", "workspace": {"url": TOKEN_URL}}

    out = reg.redacted(row)

    assert row["workspace"]["url"] == TOKEN_URL
    assert out["workspace"]["url"] != TOKEN_URL


def test_ledger_file_is_not_world_readable_when_it_holds_a_token(tmp_bs_home):
    """Redacting stdout is pointless if the file next to it is 0644."""
    import stat

    _attached(tmp_bs_home)
    mode = stat.S_IMODE(reg._ledger_path().stat().st_mode)

    assert mode == 0o600
