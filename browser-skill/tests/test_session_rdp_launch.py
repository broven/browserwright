"""P6: rdp per-session daemon launch/attach + close + reap (subprocess mocked)."""
import pytest

from browser_skill import session_create
from browser_skill import session_registry as reg


@pytest.fixture
def spawned(monkeypatch):
    """Record _spawn_detached / _run / os.kill calls instead of running them."""
    rec = {"spawn": [], "run": [], "kill": []}
    monkeypatch.setattr(session_create, "_spawn_detached",
                        lambda cmd: (rec["spawn"].append(cmd), 4242)[1])
    monkeypatch.setattr(session_create, "_run",
                        lambda cmd: (rec["run"].append(cmd), 0)[1])
    monkeypatch.setattr(session_create, "_free_port", lambda: 7000)
    monkeypatch.setattr(session_create.os, "kill",
                        lambda pid, sig: rec["kill"].append((pid, sig)))
    return rec


def test_create_launches_chrome_and_serve(tmp_bs_home, spawned):
    sid = session_create.new(backend="rdp", create=True, name="job")
    cmds = spawned["spawn"]
    assert any("launch-chrome" in c and "--port" in c and "7000" in c for c in cmds)
    assert any("serve" in c and "browser-daemon-s" + sid in c for c in cmds)
    record = reg.get(sid)
    assert record["owner"] == "create"
    assert record["daemon_endpoint"] == f"browser-daemon-s{sid}"
    assert record["workspace"]["port"] == 7000
    assert record["workspace"]["chrome_pid"] == 4242


def test_attach_points_daemon_at_port(tmp_bs_home, spawned):
    sid = session_create.new(backend="rdp", attach=9222)
    cmds = spawned["spawn"]
    # exactly one serve, pointed at the given port; no launch-chrome
    assert all("launch-chrome" not in c for c in cmds)
    serve = [c for c in cmds if "serve" in c]
    assert len(serve) == 1
    assert "9222" in serve[0] and f"browser-daemon-s{sid}" in serve[0]
    assert reg.get(sid)["owner"] == "attach"


def test_close_browser_stops_daemon_and_kills_chrome(tmp_bs_home, spawned):
    sid = reg.allocate(backend="rdp", daemon_endpoint="browser-daemon-s1",
                       owner="create", workspace={"port": 7000, "chrome_pid": 4242})
    session_create._close_browser(reg.get(sid))
    assert any("stop" in c and "browser-daemon-s1" in c for c in spawned["run"])
    assert spawned["kill"] == [(4242, session_create.signal.SIGTERM)]


def test_reap_closes_idle_create_session(tmp_bs_home, spawned):
    sid = reg.allocate(backend="rdp", daemon_endpoint="browser-daemon-s1",
                       owner="create", workspace={"port": 7000, "chrome_pid": 4242})
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))
    pruned = session_create.reap(idle_seconds=3600)
    assert [p["id"] for p in pruned] == [sid]
    assert reg.get(sid) is None
    # the create-owned browser/daemon was torn down
    assert spawned["kill"] == [(4242, session_create.signal.SIGTERM)]


def test_reap_leaves_attach_browser_alone(tmp_bs_home, spawned):
    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="attach",
                       workspace={"target": 9222})
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))
    session_create.reap(idle_seconds=3600)
    assert spawned["kill"] == []  # attach: never killed


def test_extension_end_closes_owned_tabs_and_reminds(tmp_bs_home, spawned, capsys, monkeypatch):
    """P5: ending an extension session closes its agent-owned tabs via the
    daemon (browser stays) and still removes the ledger entry."""
    ended = []
    monkeypatch.setattr(session_create, "_run",
                        lambda cmd: (ended.append(cmd), 0)[1])
    sid = reg.allocate(backend="extension", daemon_endpoint="default",
                       owner="attach", name="research")
    msg = session_create.end(reg.get(sid))
    assert any("end-session" in c and "--session" in c for c in ended)
    assert "still running" in msg.lower()
    assert reg.get(sid) is None
