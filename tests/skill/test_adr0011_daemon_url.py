"""ADR-0011 client addressing: one URL, and what "explicit" buys.

The precedence chain and the `explicit` flag are the two things every
auto-start site keys on, so they are pinned here rather than inferred from the
call sites that consume them.
"""
from __future__ import annotations

import json

import pytest

from browserwright import daemon_lifecycle as lifecycle
from browserwright import daemon_url as du
from browserwright import session_create as _session_create
from browserwright.daemon._ipc import EndpointProbe

#: The genuine `daemon_lifecycle.ensure`, captured at collection time — before
#: the repo-wide wall in `tests/conftest.py` replaces it with a stub. That
#: stub exists so no test accidentally starts a daemon; these tests are ABOUT
#: that function, and reach past the stub safely because they substitute its
#: adapters (`probe`, `run_verb`, `_spawn_detached`) themselves.
_REAL_ENSURE = lifecycle.ensure
#: Likewise the real detached spawn, which the wall replaces with a tripwire.
_REAL_SPAWN_DETACHED = lifecycle._spawn_detached


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.delenv("BD_CONFIG", raising=False)
    # An empty runtime dir: no endpoint state file to fall back to.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    du.set_cli_daemon_url(None)
    du.set_cli_config_path(None)
    yield
    du.set_cli_daemon_url(None)
    du.set_cli_config_path(None)


# ---- precedence ------------------------------------------------------------


def test_default_is_local_loopback_and_not_explicit():
    ep = du.daemon_endpoint()
    assert ep.url == "http://127.0.0.1:19990"
    assert ep.explicit is False
    assert ep.source == "default"


def test_env_beats_the_default_and_is_explicit(monkeypatch):
    monkeypatch.setenv("BW_DAEMON_URL", "http://100.72.20.32:19990")
    ep = du.daemon_endpoint()
    assert (ep.url, ep.explicit, ep.source) == (
        "http://100.72.20.32:19990", True, "env")


def test_toml_key_is_read_and_is_explicit(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('daemon_url = "http://box.tailnet:19990"\n')
    monkeypatch.setenv("BD_CONFIG", str(cfg))
    ep = du.daemon_endpoint()
    assert (ep.url, ep.explicit, ep.source) == (
        "http://box.tailnet:19990", True, "toml")


def test_cli_config_flag_supplies_the_toml_source(monkeypatch, tmp_path):
    """`--config` must feed the `daemon_url` key, not just $BD_CONFIG.

    `Config.load()` reads the flag first, so a resolver that only looked at
    $BD_CONFIG would configure the daemon from one file and address it from
    another — silently operating on a different daemon.
    """
    cfg = tmp_path / "custom.toml"
    cfg.write_text('daemon_url = "http://box.tailnet:19990"\n')
    monkeypatch.delenv("BD_CONFIG", raising=False)
    du.set_cli_config_path(str(cfg))

    ep = du.daemon_endpoint()
    assert (ep.url, ep.explicit, ep.source) == (
        "http://box.tailnet:19990", True, "toml")


def test_cli_config_flag_tops_bd_config(monkeypatch, tmp_path):
    from_flag = tmp_path / "flag.toml"
    from_flag.write_text('daemon_url = "http://from-flag:19990"\n')
    from_env = tmp_path / "env.toml"
    from_env.write_text('daemon_url = "http://from-bd-config:19990"\n')
    monkeypatch.setenv("BD_CONFIG", str(from_env))
    du.set_cli_config_path(str(from_flag))

    assert du.daemon_endpoint().url == "http://from-flag:19990"


def test_cli_flag_tops_env_and_toml(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('daemon_url = "http://from-toml:19990"\n')
    monkeypatch.setenv("BD_CONFIG", str(cfg))
    monkeypatch.setenv("BW_DAEMON_URL", "http://from-env:19990")
    du.set_cli_daemon_url("http://from-flag:19990")
    ep = du.daemon_endpoint()
    assert (ep.url, ep.explicit, ep.source) == (
        "http://from-flag:19990", True, "cli")


def test_state_file_ranks_below_config_and_is_not_explicit(monkeypatch, tmp_path):
    """The one non-configured source: it exists so a daemon on an ephemeral
    port is findable, and it must never flip a client into the hands-off
    regime — otherwise a locally auto-started daemon would disable auto-start.
    """
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": "http://127.0.0.1:44444"}))

    ep = du.daemon_endpoint()
    assert (ep.url, ep.explicit, ep.source) == (
        "http://127.0.0.1:44444", False, "state_file")

    monkeypatch.setenv("BW_DAEMON_URL", "http://configured:19990")
    assert du.daemon_endpoint().url == "http://configured:19990"


@pytest.mark.parametrize("raw,expected", [
    ("http://1.2.3.4:1234", "http://1.2.3.4:1234"),
    ("http://1.2.3.4:1234/", "http://1.2.3.4:1234"),
    ("ws://1.2.3.4:1234", "http://1.2.3.4:1234"),
    ("1.2.3.4:1234", "http://1.2.3.4:1234"),
    ("http://1.2.3.4", "http://1.2.3.4:19990"),
])
def test_url_forms_normalize_to_one_http_authority(monkeypatch, raw, expected):
    monkeypatch.setenv("BW_DAEMON_URL", raw)
    assert du.daemon_endpoint().url == expected


def test_ws_builds_each_surface_and_drops_empty_query(monkeypatch):
    monkeypatch.setenv("BW_DAEMON_URL", "http://127.0.0.1:19990")
    ep = du.daemon_endpoint()
    assert ep.ws("/cdp") == "ws://127.0.0.1:19990/cdp"
    assert ep.ws("/control", client="cli", session=None) == (
        "ws://127.0.0.1:19990/control?client=cli")
    assert ep.ws("/exec", session="a b") == "ws://127.0.0.1:19990/exec?session=a%20b"


# ---- what `explicit` gates -------------------------------------------------


def _record_adapters(monkeypatch):
    """Substitute ensure's side-effecting adapters; return what they saw."""
    from browserwright.daemon import _ipc

    actions = []
    monkeypatch.setattr(lifecycle, "_spawn_detached",
                        lambda args, *, initiator: actions.append(
                            ("spawn", args, initiator)))
    monkeypatch.setattr(lifecycle, "run_verb",
                        lambda args, **kw: actions.append(
                            ("run", args, kw.get("initiator"))))
    monkeypatch.setattr(_ipc, "request_executor_handoff",
                        lambda pid: actions.append(("handoff", pid)))
    monkeypatch.setattr(_ipc, "log_lifecycle",
                        lambda event, **fields: actions.append(
                            ("log", event, fields)))
    return actions


def test_explicit_endpoint_that_is_down_errors_and_does_not_spawn(monkeypatch):
    from browserwright.errors import DaemonUnavailable

    monkeypatch.setenv("BW_DAEMON_URL", "http://127.0.0.1:19990")
    # The conftest wall answers every probe with "refused".
    actions = _record_adapters(monkeypatch)

    with pytest.raises(DaemonUnavailable) as ei:
        _REAL_ENSURE("session new")
    assert "will not start or restart a daemon" in str(ei.value)
    # Not even a `stop`: the daemon at that URL is someone else's process, and
    # a local `browserwright-daemon stop` would signal the wrong one.
    assert actions == []


def test_explicit_endpoint_with_a_skewed_daemon_warns_and_does_not_replace(
        monkeypatch, capsys):
    monkeypatch.setenv("BW_DAEMON_URL", "http://10.0.0.7:19990")
    monkeypatch.setattr(lifecycle, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="ours", host=h, port=p, pid=7, version="0.0.1-old"))
    actions = _record_adapters(monkeypatch)

    assert _REAL_ENSURE("session new").state == lifecycle.STALE
    assert actions == []
    # The skew is still reported — silently driving a mismatched daemon is
    # the pothole version coherence exists to prevent.
    assert "will not replace it" in capsys.readouterr().err


def test_default_endpoint_still_cold_starts_a_daemon(monkeypatch):
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    actions = _record_adapters(monkeypatch)

    verdict = _REAL_ENSURE("session new")
    assert verdict.state == lifecycle.DOWN
    (log, spawn) = actions
    # ADR-0012 rule 5: the spawn is logged with its reason, both probes, and
    # the initiator the child daemon will report as its own.
    assert log[:2] == ("log", "spawn")
    assert log[2]["reason"] == "session new"
    assert log[2]["probes"] == "refused,refused"
    assert spawn[:2] == ("spawn", ["serve"])
    assert spawn[2] == log[2]["initiator"]
    assert spawn[2].startswith("cli:auto-start (session new) cwd=")


def test_default_endpoint_replaces_a_version_skewed_daemon(monkeypatch):
    """The unconfigured local daemon IS ours, so a skew is repaired — with the
    resident executors handed over to the replacement (ADR-0013)."""
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setattr(lifecycle, "probe", lambda h, p, timeout=1.5: EndpointProbe(
        kind="ours", host=h, port=p, pid=31, version="0.0.1-old"))
    actions = _record_adapters(monkeypatch)

    _REAL_ENSURE("recover session=7")
    assert [a[:2] for a in actions] == [
        ("log", "replace"),
        ("handoff", 31),
        ("run", ["stop"]),
        ("spawn", ["serve"]),
    ]
    assert actions[0][2]["pid_before"] == 31
    assert actions[0][2]["reason"] == "recover session=7"


def test_an_unreachable_endpoint_names_itself_instead_of_errno_61(monkeypatch):
    """A refused connect must not surface as a bare `Connection refused`.

    The URL may name another machine and, when it was configured explicitly, we
    deliberately did not start anything — so "which address, and why is nothing
    running there" is the whole content of the failure.
    """
    import browserwright.session as session_mod
    from browserwright.errors import DaemonUnavailable

    monkeypatch.setenv("BW_DAEMON_URL", "http://100.72.20.32:19990")

    def _refuse(url, *a, **k):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(session_mod, "CDPSession", _refuse)
    sess = session_mod.Session(
        daemon=type("_D", (), {
            "resolve_ws_url": lambda self: "ws://100.72.20.32:19990/control",
        })(),
    )
    with pytest.raises(DaemonUnavailable) as ei:
        _ = sess.cdp
    msg = str(ei.value)
    assert "http://100.72.20.32:19990" in msg
    assert "will not start or restart a daemon" in msg


def test_an_unreachable_default_endpoint_says_so_plainly(monkeypatch, tmp_path):
    """On the default endpoint the hands-off wording would be wrong — nothing
    was configured, and auto-start is still in play."""
    import browserwright.session as session_mod
    from browserwright.errors import DaemonUnavailable

    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def _refuse(url, *a, **k):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(session_mod, "CDPSession", _refuse)
    sess = session_mod.Session(
        daemon=type("_D", (), {
            "resolve_ws_url": lambda self: "ws://127.0.0.1:19990/control",
        })(),
    )
    with pytest.raises(DaemonUnavailable) as ei:
        _ = sess.cdp
    assert "will not start or restart a daemon" not in str(ei.value)


# ---- the endpoint must survive the shell-out boundary ----------------------


def test_child_env_exports_an_explicit_endpoint(monkeypatch):
    """A `--daemon-url` flag lives in memory; a child process cannot see it."""
    du.set_cli_daemon_url("http://100.72.20.32:19990")
    assert du.child_env({})[du.ENV_VAR] == "http://100.72.20.32:19990"

    du.set_cli_daemon_url(None)
    monkeypatch.setenv("BW_DAEMON_URL", "http://box.tailnet:19990")
    assert du.child_env()[du.ENV_VAR] == "http://box.tailnet:19990"


def test_child_env_does_not_export_a_non_explicit_endpoint(tmp_path):
    """Exporting the default would flip the child into the never-auto-start
    regime — exactly wrong for the `serve` we may be spawning."""
    assert du.ENV_VAR not in du.child_env({"XDG_RUNTIME_DIR": str(tmp_path)})


@pytest.mark.parametrize("call,expected_argv0", [
    (lambda sc: sc._end_daemon_session({"id": "7"}), "browserwright-daemon"),
    (lambda sc: sc.reset_executor({"id": "7"}), "browserwright-daemon"),
    (lambda sc: sc.attach_active({"id": "7"}), "browserwright-daemon"),
])
def test_layer2_shellouts_carry_the_cli_endpoint(monkeypatch, call,
                                                 expected_argv0):
    """Without this, a `--daemon-url` run passes the liveness gate against one
    daemon and then tears down a session on a different one."""
    import subprocess

    du.set_cli_daemon_url("http://100.72.20.32:19990")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("env") or {}))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    call(_session_create)

    assert seen, "no browserwright-daemon subprocess was run"
    for cmd, env in seen:
        assert cmd[0] == expected_argv0
        assert env.get("BW_DAEMON_URL") == "http://100.72.20.32:19990", cmd


def test_spawned_daemon_inherits_the_cli_endpoint(monkeypatch):
    du.set_cli_daemon_url("http://127.0.0.1:29990")
    seen = {}

    class _Proc:
        pid = 1234

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env") or {}
        return _Proc()

    monkeypatch.setattr(lifecycle.subprocess, "Popen", fake_popen)
    # The real spawn, not the wall's tripwire — `Popen` above is what actually
    # keeps this test from starting anything.
    _REAL_SPAWN_DETACHED(["serve"], initiator="cli:auto-start (test)")
    assert seen["cmd"] == ["browserwright-daemon", "serve"]
    assert seen["env"]["BW_DAEMON_URL"] == "http://127.0.0.1:29990"
    assert seen["env"]["BW_DAEMON_INITIATOR"] == "cli:auto-start (test)"
