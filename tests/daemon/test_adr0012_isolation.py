"""ADR-0012 (development never touches the global daemon), issue #91.

Rule 2: `upgrade-global` no longer passes `--force` (mise.toml, checked here
        by text so a re-introduction fails a test).
Rule 3: the plist is generated; `install --force` without flags carries the
        installed serve args forward; a non-loopback bind publishes loopback
        for local clients.
Rule 4: `browserwright-daemon activity` is the one gate for "would this
        disturb someone" (exit 4 = busy).
Rule 6: `serve` stale-detects on its OWN port; `stop` refuses when an
        implicit endpoint names a different port than the overridden config.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from browserwright.daemon import _ipc, launchagent
from browserwright.daemon.config import Config
from browserwright.daemon.server import listener as listener_mod

REPO = Path(__file__).resolve().parents[2]


# ---- rule 6: serve probes its own port; stop refuses a foreign one ---------


def test_local_probe_host_is_loopback_for_specific_and_wildcard_binds():
    assert listener_mod._local_probe_host("100.72.20.32") == "127.0.0.1"
    assert listener_mod._local_probe_host("0.0.0.0") == "127.0.0.1"
    assert listener_mod._local_probe_host("127.0.0.1") == "127.0.0.1"


@pytest.mark.asyncio
async def test_serve_stale_detect_pings_its_own_port_not_the_resolved_endpoint(
        monkeypatch, capsys):
    """The 2026-09-02 shape: BD_FACADE_PORT=39990 in an isolated shell, no
    BW_DAEMON_URL. The old probe hit the resolved default (the global
    daemon on 19990) and `serve` deferred to it. Now it asks about 39990."""
    seen = {}

    async def fake_ping(timeout=1.0, *, host=None, port=None):
        seen["host"], seen["port"] = host, port
        return _ipc.PongInfo(pid=4207, version="0.17.4")  # "someone is there"

    monkeypatch.setattr(listener_mod._ipc, "ping_status_async", fake_ping)
    monkeypatch.setattr(listener_mod._ipc, "note_already_running",
                        lambda pid: f"already running (pid {pid})")
    cfg = Config(backend="extension")
    cfg.facade_port = 39990
    cfg.facade_host = "100.72.20.32"
    assert await listener_mod.run_serve(cfg) == 1
    assert seen == {"host": "127.0.0.1", "port": 39990}


def test_stop_refuses_when_overridden_port_disagrees_with_resolved_endpoint(
        monkeypatch, capsys):
    from browserwright.daemon_url import DaemonEndpoint
    import browserwright.daemon.cli as climod
    import browserwright.daemon_url as du
    monkeypatch.setattr(du, "daemon_endpoint", lambda **_k: DaemonEndpoint(
        url="http://127.0.0.1:19990", explicit=False, source="default"))
    pinged = []
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: pinged.append(1) or 4207)
    cfg = Config()
    cfg.facade_port = 39990
    assert climod._cmd_stop(SimpleNamespace(timeout=0), cfg) == 3
    err = capsys.readouterr().err
    assert "refusing to stop" in err and "39990" in err and "19990" in err
    assert pinged == []  # never even reached for the pid


def test_stop_proceeds_when_the_url_is_explicit(monkeypatch, capsys):
    """An explicit BW_DAEMON_URL is the operator's word; ports don't gate it."""
    import browserwright.daemon.cli as climod
    import browserwright.daemon_url as du
    from browserwright.daemon_url import DaemonEndpoint

    monkeypatch.setattr(du, "daemon_endpoint", lambda **_k: DaemonEndpoint(
        url="http://127.0.0.1:39990", explicit=True, source="env"))
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda: None)
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    cfg = Config()
    cfg.facade_port = 19990
    # Ports disagree (39990 vs 19990) but the URL is explicit: no port guard.
    # Nothing answers the ping, so stop cleans up and exits 0.
    assert climod._cmd_stop(SimpleNamespace(timeout=0), cfg) == 0
    assert "refusing to stop" not in capsys.readouterr().err


# ---- rule 3: generated plist, carried-forward args, loopback published -----


def test_install_force_without_flags_carries_the_installed_serve_args(
        monkeypatch, tmp_path):
    plist = tmp_path / "com.browserwright-daemon.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: plist)
    monkeypatch.setattr(launchagent, "require_darwin", lambda verb: None)
    monkeypatch.setattr(launchagent, "resolve_daemon_bin",
                        lambda: "/opt/bw/bin/browserwright-daemon")
    monkeypatch.setattr(launchagent, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(launchagent, "launchctl", lambda *a: (0, "", ""))

    first = launchagent.install(extension_port=None, facade_host="100.72.20.32",
                                facade_port=None, force=True)
    assert first["facade_host"] == "100.72.20.32"
    assert launchagent.plist_serve_args(plist) == {"facade_host": "100.72.20.32"}

    again = launchagent.install(extension_port=None, facade_host=None,
                                facade_port=None, force=True)
    assert again["facade_host"] == "100.72.20.32"
    assert again["carried_from_previous_plist"] == {"facade_host": "100.72.20.32"}
    assert "--facade-host" in plist.read_text()

    override = launchagent.install(extension_port=None, facade_host="127.0.0.1",
                                   facade_port=None, force=True)
    assert override["facade_host"] == "127.0.0.1"
    assert "carried_from_previous_plist" not in override

    # the way back to loopback-only: an empty host is a reset, not "keep"
    launchagent.install(extension_port=None, facade_host="100.72.20.32",
                        facade_port=None, force=True)
    reset = launchagent.install(extension_port=None, facade_host="",
                                facade_port=None, force=True)
    assert reset["facade_host"] is None
    assert "--facade-host" not in plist.read_text()


def test_plist_serve_args_survives_a_damaged_plist(tmp_path):
    bad = tmp_path / "x.plist"
    bad.write_text("not a plist")
    assert launchagent.plist_serve_args(bad) == {}


# ---- rule 4: the activity gate --------------------------------------------


def test_activity_verb_exits_4_when_busy_and_0_when_idle(monkeypatch, capsys):
    import browserwright.daemon.cli as climod
    from browserwright.daemon import restart_guard

    monkeypatch.setattr(restart_guard, "probe", lambda cfg, active_within=None:
                        restart_guard.Activity(blocked=True, determinate=True,
                                               reasons=["executor(s) running code right now: 12"]))
    assert climod._cmd_activity(SimpleNamespace(json=True, active_within=None), Config()) == 4
    out = json.loads(capsys.readouterr().out)
    assert out["busy"] is True and out["reasons"]

    monkeypatch.setattr(restart_guard, "probe", lambda cfg, active_within=None:
                        restart_guard.Activity(blocked=False, determinate=True))
    assert climod._cmd_activity(SimpleNamespace(json=False, active_within=None), Config()) == 0
    assert capsys.readouterr().out.strip() == "idle"


# ---- rules 1, 2, 4 as text: the mise tasks and the e2e runner -------------


def test_upgrade_global_no_longer_forces_a_restart():
    import tomllib
    tasks = tomllib.loads((REPO / "mise.toml").read_text())["tasks"]
    run = tasks["upgrade-global"]["run"]
    code = "\n".join(ln for ln in run.splitlines()
                     if not ln.lstrip().startswith("#") and "echo" not in ln)
    assert "restart --force" not in code
    assert 'global_cmd "$global_daemon" restart' in code
    assert code.index("activity 2>&1") < code.index("uv tool install browserwright")
    assert code.index("uv tool install browserwright") < code.index(
        'global_cmd "$global_daemon" restart')


def test_dev_link_never_writes_the_global_binary_names():
    import tomllib
    run = tomllib.loads((REPO / "mise.toml").read_text())["tasks"]["dev-link"]["run"]
    assert 'ln -sf "$src" "$dst"' not in run
    assert "$LOCAL_BIN/$bin_name-dev" in run
    assert "BW_DAEMON_URL=http://127.0.0.1:$DEV_FACADE_PORT" in run
    assert "~/.agents/skills/browserwright" not in run


def test_e2e_runner_consults_the_activity_gate():
    text = (REPO / "tests/daemon/e2e/run.sh").read_text()
    assert "browserwright-daemon activity" in text
    assert "E2E_FORCE" in text
