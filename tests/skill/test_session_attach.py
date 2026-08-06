"""`session new --attach=<port|url>` — the argument, end to end.

Before #38 **nothing in the repo passed `--attach` at all**: every attach-owned
row in the suite was hand-allocated through `session_registry`, so the parsing,
the validation, and the ledger shape it produces were entirely uncovered. The
old `int(attach)` — including its bare-flag bug — was never exercised once.

These go through `cli._cmd_session` rather than calling `session_create.new`
directly, because `_parse_kv_args` / `_coerce` are part of what decides a port
from a URL: `--attach=9222` arrives as `int`, `--attach=ws://…` as `str`.
"""
from __future__ import annotations

import pytest

from browserwright import cli, session_create
from browserwright import session_registry as reg


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch):
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)


def _new(*args) -> int:
    return cli._cmd_session(["new", "--backend=cdp", "--name=t", *args])


def _only_row() -> dict:
    rows = reg.list_all()
    assert len(rows) == 1, rows
    return rows[0]


# ---- accepted shapes -------------------------------------------------------


def test_port_is_stored_as_a_port(tmp_bs_home, capsys):
    assert _new("--attach=9222") == 0
    row = _only_row()
    assert row["workspace"] == {"port": 9222}
    assert row["owner"] == "attach"
    # The dead `target` key is gone: it was written and never read, a second
    # slot shadowing `port`.
    assert "target" not in row["workspace"]


@pytest.mark.parametrize("url", [
    "ws://box.local:9222/devtools/browser/2f1c",
    "wss://connect.example.com/session/abc",
    "http://127.0.0.1:9222",
    "https://cloud.example.com/cdp",
])
def test_urls_are_stored_verbatim(tmp_bs_home, capsys, url):
    assert _new(f"--attach={url}") == 0
    assert _only_row()["workspace"] == {"url": url}


def test_token_bearing_url_is_not_rewritten(tmp_bs_home, capsys):
    """No normalisation, ever — the token is *in* the URL.

    Userinfo and query strings are exactly the shapes cloud and anti-detect
    browsers hand out. A well-meaning canonicaliser would invalidate them.
    """
    url = "wss://user:s3cr3t@cloud.example.com:443/cdp?apiKey=deadbeef&x=1"
    assert _new(f"--attach={url}") == 0
    assert _only_row()["workspace"] == {"url": url}


# ---- rejected shapes -------------------------------------------------------


def test_bare_attach_is_rejected_and_writes_nothing(tmp_bs_home, capsys):
    """The regression this suite exists for.

    `--attach` with no value parses to `True`; `bool` is a subclass of `int`,
    so the old `int(attach)` accepted it and pinned the session to **port 1**.
    The session was created, looked fine, and could never connect.
    """
    assert _new("--attach") == 1
    assert reg.list_all() == []
    err = capsys.readouterr().err
    assert "--attach" in err
    assert "9222" in err and "ws://" in err  # both accepted forms are shown


@pytest.mark.parametrize("bad", ["0", "70000", "-1"])
def test_out_of_range_ports_are_rejected(tmp_bs_home, capsys, bad):
    assert _new(f"--attach={bad}") == 1
    assert reg.list_all() == []
    assert "range" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [
    "ftp://host/path",       # wrong scheme
    "notaurl",               # no scheme at all
    "ws://",                 # scheme but no host
    "[1,2]",                 # _coerce turns this into a list
])
def test_unusable_targets_are_rejected(tmp_bs_home, capsys, bad):
    assert _new(f"--attach={bad}") == 1
    assert reg.list_all() == []
    assert "--attach" in capsys.readouterr().err


def test_bare_host_port_is_rejected_with_the_fix_spelled_out(tmp_bs_home, capsys):
    """`host:9222` is the most likely near-miss, so name the correction."""
    assert _new("--attach=localhost:9222") == 1
    assert "http://localhost:9222" in capsys.readouterr().err


# ---- flag combinations -----------------------------------------------------


def test_create_and_attach_together_are_rejected(tmp_bs_home, capsys):
    """They mean opposite things about ownership.

    Previously `create` silently won, so `--create --attach=9222` launched a
    browser on a random free port and ignored 9222 without a word.
    """
    assert _new("--create", "--attach=9222") == 1
    assert reg.list_all() == []
    assert "mutually exclusive" in capsys.readouterr().err


def test_neither_create_nor_attach_is_rejected(tmp_bs_home, capsys):
    """Previously wrote `workspace=None` and deferred the confusion."""
    assert _new() == 1
    assert reg.list_all() == []
    assert "--create" in capsys.readouterr().err


def test_create_still_pins_a_free_port(tmp_bs_home, capsys):
    assert _new("--create") == 0
    row = _only_row()
    assert row["owner"] == "create"
    assert isinstance(row["workspace"]["port"], int)
    assert "url" not in row["workspace"]


# ---- retired backend names -------------------------------------------------


@pytest.mark.parametrize("retired", ["rdp", "env"])
def test_retired_backend_names_say_what_to_write_instead(
    tmp_bs_home, capsys, retired,
):
    assert cli._cmd_session(
        ["new", f"--backend={retired}", "--name=t"]) == 1
    assert reg.list_all() == []
    err = capsys.readouterr().err
    assert "cdp" in err
    # Not merely "invalid choice" — the replacement has to be spelled out.
    assert "--backend=cdp" in err


@pytest.mark.parametrize("retired", ["rdp", "env"])
def test_session_create_rejects_retired_names_too(tmp_bs_home, retired):
    """The CLI is not the only door; the Layer 2 API refuses as well."""
    with pytest.raises(ValueError, match="cdp"):
        session_create.new(backend=retired, name="t")
