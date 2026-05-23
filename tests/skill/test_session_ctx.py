"""NoSession error + resolve_session(): env/arg → ledger record, else refuse."""
import pytest

from browserwright import session_ctx
from browserwright.errors import NoSession


def test_nosession_message_is_actionable():
    e = NoSession()
    assert e.exit_code == 2
    assert "session new" in str(e)


def test_resolve_session_missing_raises(tmp_bs_home, monkeypatch):
    monkeypatch.delenv("BD_SESSION", raising=False)
    with pytest.raises(NoSession):
        session_ctx.resolve_session()


def test_resolve_session_unknown_id_raises(tmp_bs_home, monkeypatch):
    monkeypatch.setenv("BD_SESSION", "999")
    with pytest.raises(NoSession):
        session_ctx.resolve_session()


def test_resolve_session_returns_record_and_touches(tmp_bs_home, monkeypatch):
    from browserwright import session_registry as reg
    sid = reg.allocate(backend="extension", daemon_endpoint="default", owner="attach")
    monkeypatch.setattr(reg.time, "time", lambda: 5_000.0)
    monkeypatch.setenv("BD_SESSION", sid)
    rec = session_ctx.resolve_session()
    assert rec["id"] == sid and rec["backend"] == "extension"
    # resolve touches last_seen
    assert reg.get(sid)["last_seen"] == 5_000.0


def test_resolve_session_explicit_arg_beats_env(tmp_bs_home, monkeypatch):
    from browserwright import session_registry as reg
    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="create")
    monkeypatch.setenv("BD_SESSION", "999")  # bogus env
    rec = session_ctx.resolve_session(sid)
    assert rec["id"] == sid


def test_inline_run_refuses_without_session(tmp_bs_home, monkeypatch, capsys):
    import io

    from browserwright.repl import inline

    monkeypatch.delenv("BD_SESSION", raising=False)
    rc = inline.run(io.StringIO("print(1)\n"))
    assert rc == 2
    assert "session new" in capsys.readouterr().err


def test_inline_run_allows_pure_python_with_session(tmp_bs_home, monkeypatch, capsys):
    import io

    from browserwright import session_registry as reg
    from browserwright.repl import inline

    sid = reg.allocate(backend="extension", daemon_endpoint="default", owner="attach")
    monkeypatch.setenv("BD_SESSION", sid)
    rc = inline.run(io.StringIO("print(2 + 3)\n"))
    assert rc == 0
    assert "5" in capsys.readouterr().out
