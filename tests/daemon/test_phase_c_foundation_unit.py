"""Unit coverage for the Phase C PR1 foundation (no real browser):

  - `Config.resolved_facade_port()` tri-state (auto-enable / disable / override).
  - the `_ipc` facade discovery file round-trip + cleanup.
  - `browserwright-daemon status --json` surfaces the advertised facade ws.
  - the lazy heredoc `page`/`context` proxies don't connect on construction.

The connect+bind path itself (which needs the daemon facade + a browser) is
covered by `tests/daemon/e2e/test_l2_heredoc_playwright_page.py`.
"""
from __future__ import annotations

import json

import pytest

from browserwright import daemon_lifecycle
from browserwright.daemon import _ipc
from browserwright.daemon.config import (
    DEFAULT_FACADE_HOST,
    DEFAULT_FACADE_PORT,
    Config,
    load,
)


# ---- Config.resolved_facade_port() -----------------------------------------


def test_endpoint_port_defaults_when_unset():
    # None (unset) → the default endpoint port.
    assert Config().resolved_facade_port() == DEFAULT_FACADE_PORT


def test_endpoint_port_zero_is_ephemeral_not_disabled():
    """ADR-0011 retired the "disabled" value.

    The endpoint is the daemon's only client-facing door, so there is nothing
    left to disable — a daemon without it serves nobody. `0` therefore now means
    what it means everywhere else in sockets: let the OS pick, which is how a
    per-test daemon gets an endpoint that cannot collide with the developer's.
    """
    assert Config(facade_port=0).resolved_facade_port() == 0


def test_endpoint_explicit_port_override():
    assert Config(facade_port=29991).resolved_facade_port() == 29991


def test_endpoint_port_load_precedence(monkeypatch):
    from browserwright.daemon.config import load
    cfg = load(env={})
    assert cfg.facade_port is None
    assert cfg.resolved_facade_port() == DEFAULT_FACADE_PORT
    cfg_eph = load(env={"BD_FACADE_PORT": "0"})
    assert cfg_eph.resolved_facade_port() == 0
    cfg_ov = load(env={"BD_FACADE_PORT": "29991"})
    assert cfg_ov.resolved_facade_port() == 29991


# ---- facade_host config precedence -----------------------------------------


def test_facade_host_defaults_to_loopback():
    # Never exposed by accident: the default bind host stays loopback.
    assert Config().facade_host == DEFAULT_FACADE_HOST == "127.0.0.1"
    assert load(env={}).facade_host == "127.0.0.1"


def test_facade_host_precedence_cli_over_env_over_toml(tmp_path):
    # Mirrors facade_port: CLI > env > toml > default.
    cfg_path = tmp_path / "daemon.toml"
    cfg_path.write_text('facade_host = "10.0.0.1"\n')

    # toml only
    cfg_toml = load(cli_config_path=str(cfg_path), env={})
    assert cfg_toml.facade_host == "10.0.0.1"

    # env tops toml
    cfg_env = load(cli_config_path=str(cfg_path), env={"BD_FACADE_HOST": "10.0.0.2"})
    assert cfg_env.facade_host == "10.0.0.2"

    # CLI tops env + toml
    cfg_cli = load(
        cli_config_path=str(cfg_path),
        cli_facade_host="100.72.20.32",
        env={"BD_FACADE_HOST": "10.0.0.2"},
    )
    assert cfg_cli.facade_host == "100.72.20.32"


def test_session_idle_prune_loads_from_toml_and_env(tmp_path):
    from browserwright.daemon.config import load

    cfg_path = tmp_path / "daemon.toml"
    cfg_path.write_text("session_idle_prune = 12.5\n")

    cfg = load(cli_config_path=str(cfg_path), env={})
    assert cfg.session_idle_prune == 12.5

    disabled = load(env={"BD_SESSION_IDLE_PRUNE": "0"})
    assert disabled.session_idle_prune is None

    overridden = load(
        cli_config_path=str(cfg_path),
        env={"BD_SESSION_IDLE_PRUNE": "33"},
    )
    assert overridden.session_idle_prune == 33.0


# ---- endpoint discovery (ADR-0011) -----------------------------------------
#
# There is no discovery file and no separate facade any more: one endpoint, one
# port, resolved by `browserwright.daemon_url`. The client derives the cdp
# surface from that URL and pings only to learn whether anything is home. These
# tests pin the two answers a client can get, and the derivation in between.


def _pong(**kw):
    """A pong as `ping_status_sync` would return it."""
    return _ipc.PongInfo(pid=kw.pop("pid", 4242),
                         version=kw.pop("version", "0.15.1"), **kw)


def test_pong_roundtrip_carries_pid_and_version():
    pong = _ipc.parse_pong(_ipc.make_pong_body(4242))
    assert pong.pid == 4242
    assert pong.version  # the installed package version

    # Anything not our shape is "not our daemon", never a half-parsed pong.
    assert _ipc.parse_pong(b"nonsense") is _ipc.NO_PONG
    assert _ipc.parse_pong(json.dumps({"pong": True}).encode()) is _ipc.NO_PONG


def _ours(host, port, timeout=1.5):
    """A `daemon_lifecycle.probe` that finds our daemon."""
    return _ipc.EndpointProbe(kind="ours", host=host, port=port, pid=4242,
                              version="0.15.1")


def test_cdp_ws_url_carries_bound_browserwright_session(monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setenv("BW_DAEMON_URL", "http://127.0.0.1:19990")
    monkeypatch.setattr(daemon_lifecycle, "probe", _ours)
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: "cdp 7")

    # The daemon parses the query with parse_qs, which decodes both `+` and
    # `%20` as a space — assert the decoded value, not the exact encoding.
    from urllib.parse import parse_qs, urlsplit

    url = ph._facade_ws_url()
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "ws://127.0.0.1:19990/cdp"
    assert parse_qs(parts.query)["session"] == ["cdp 7"]


def test_cdp_ws_url_follows_a_remote_endpoint(monkeypatch):
    """The point of ADR-0011: point the URL elsewhere and `page` follows."""
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setenv("BW_DAEMON_URL", "http://100.72.20.32:19990")
    monkeypatch.setattr(daemon_lifecycle, "probe", _ours)
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: "s-1")

    assert ph._facade_ws_url() == (
        "ws://100.72.20.32:19990/cdp?session=s-1")


def test_cdp_ws_url_on_an_explicit_endpoint_says_it_will_not_start_one(monkeypatch):
    """An explicitly configured endpoint that is down is an error, not a cue to
    start a local daemon that would serve a different browser entirely."""
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setenv("BW_DAEMON_URL", "http://100.72.20.32:19990")
    # The conftest wall answers every `daemon_lifecycle.probe` with "refused".
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: None)

    with pytest.raises(ph.FacadeUnavailable) as ei:
        ph._facade_ws_url()
    msg = str(ei.value)
    assert "http://100.72.20.32:19990" in msg
    assert "will not start or restart a daemon" in msg


def test_cdp_ws_url_reports_a_dead_default_daemon_as_such(monkeypatch, tmp_path):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: None)

    with pytest.raises(ph.FacadeUnavailable) as ei:
        ph._facade_ws_url()
    assert "no daemon is answering at http://127.0.0.1:19990" in str(ei.value)


# ---- status --json surfaces the endpoint -----------------------------------


def test_status_json_includes_endpoint_and_cdp_surface(monkeypatch, capsys):
    from browserwright.daemon import cli

    monkeypatch.setenv("BW_DAEMON_URL", "http://127.0.0.1:19990")
    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda timeout=1.0: _ipc.PongInfo(pid=4242, version="0.15.1"))

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is True
    assert out["endpoint"]["url"] == "http://127.0.0.1:19990"
    assert out["endpoint"]["transport"] == "tcp"
    assert out["cdp_surface"] == {"ws": "ws://127.0.0.1:19990/cdp",
                                  "port": 19990}
    # Pre-ADR-0011 key, kept so `status --json` consumers don't break.
    assert out["facade"] == out["cdp_surface"]


def test_status_json_cdp_surface_null_when_dead(tmp_path, monkeypatch, capsys):
    from browserwright.daemon import cli

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout=1.0: _ipc.NO_PONG)

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is False
    # A dead daemon has no surface, and `alive: false` is the whole reason.
    assert out["cdp_surface"] is None
