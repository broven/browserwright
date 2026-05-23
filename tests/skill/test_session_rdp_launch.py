"""Single-global-daemon session create/teardown (subprocess mocked).

Refactor (docs/refactor-single-daemon.md §P3): there is ONE global daemon on a
fixed socket. ``session_create.new`` no longer launches Chrome or a per-session
``serve --name`` daemon — it allocates the ledger entry (recording the port the
daemon should pin rdp Chrome to) and just ensures the one daemon is up. The
daemon itself launches/owns the per-session rdp Chrome on ``ensureSession`` and
tears it down on ``endSession``. Teardown talks to the daemon via
``browserwright-daemon end-session --session ID`` (no ``--name``, no os.kill).
"""
import pytest

from browserwright import session_create
from browserwright import session_registry as reg


@pytest.fixture
def spawned(monkeypatch):
    """Record _spawn_detached / _run calls instead of running them, and pretend
    no daemon is running yet (so _ensure_daemon_running spawns one)."""
    rec = {"spawn": [], "run": []}
    monkeypatch.setattr(session_create, "_spawn_detached",
                        lambda cmd: (rec["spawn"].append(cmd), 4242)[1])
    monkeypatch.setattr(session_create, "_run",
                        lambda cmd: (rec["run"].append(cmd), 0)[1])
    monkeypatch.setattr(session_create, "_free_port", lambda: 7000)
    # Pretend the global daemon isn't up yet → _ensure_daemon_running spawns it.
    import browserwright.daemon._ipc as _ipc
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout=1.0: None)
    return rec


def test_create_records_port_and_ensures_single_daemon(tmp_bs_home, spawned):
    """rdp --create: allocate the ledger entry with a free port the daemon will
    pin Chrome to, and ensure the ONE global daemon is up. NO launch-chrome and
    NO per-session ``serve --name`` here — the daemon owns the Chrome launch."""
    sid = session_create.new(backend="rdp", create=True, name="job")
    cmds = spawned["spawn"]
    # The only thing spawned is the single global `serve` (no --name, no port).
    assert all("launch-chrome" not in c for c in cmds)
    assert all("--name" not in c for c in cmds)
    serve = [c for c in cmds if "serve" in c]
    assert serve == [["browserwright-daemon", "serve"]]
    record = reg.get(sid)
    assert record["owner"] == "create"
    # No per-session daemon endpoint anymore.
    assert "daemon_endpoint" not in record
    # The port is recorded so the daemon can pin the per-session Chrome to it.
    assert record["workspace"]["port"] == 7000


def test_attach_records_target_and_ensures_single_daemon(tmp_bs_home, spawned):
    """rdp --attach: record the target port and ensure the one daemon; never
    launch Chrome, never spawn a per-session daemon."""
    sid = session_create.new(backend="rdp", attach=9222, name="fp")
    cmds = spawned["spawn"]
    assert all("launch-chrome" not in c for c in cmds)
    serve = [c for c in cmds if "serve" in c]
    assert serve == [["browserwright-daemon", "serve"]]
    record = reg.get(sid)
    assert record["owner"] == "attach"
    assert record["workspace"]["port"] == 9222
    assert record["workspace"]["target"] == 9222


def test_close_browser_tells_daemon_to_end_session(tmp_bs_home, spawned):
    """_close_browser asks the single daemon to end the session — the daemon
    SIGTERMs the Chrome it owns. No os.kill here, no --name."""
    sid = reg.allocate(backend="rdp", owner="create",
                       workspace={"port": 7000})
    session_create._close_browser(reg.get(sid))
    end_cmds = [c for c in spawned["run"] if "end-session" in c]
    assert len(end_cmds) == 1
    assert "--session" in end_cmds[0]
    assert str(sid) in end_cmds[0]
    assert all("--name" not in c for c in spawned["run"])


def test_reap_closes_idle_create_session(tmp_bs_home, spawned):
    sid = reg.allocate(backend="rdp", owner="create",
                       workspace={"port": 7000})
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))
    pruned = session_create.reap(idle_seconds=3600)
    assert [p["id"] for p in pruned] == [sid]
    assert reg.get(sid) is None
    # the create-owned session was torn down via the daemon's end-session
    assert any("end-session" in c and str(sid) in c for c in spawned["run"])


def test_reap_leaves_attach_browser_alone(tmp_bs_home, spawned):
    sid = reg.allocate(backend="rdp", owner="attach",
                       workspace={"target": 9222})
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))
    session_create.reap(idle_seconds=3600)
    # attach: the daemon is never told to end the session (we don't own it)
    assert all("end-session" not in c for c in spawned["run"])


def test_extension_end_closes_owned_tabs_and_reminds(tmp_bs_home, spawned, capsys, monkeypatch):
    """Ending an extension session closes its agent-owned tabs via the single
    daemon (browser stays) and still removes the ledger entry. No --name on the
    wire — there is one daemon."""
    ended = []
    monkeypatch.setattr(session_create, "_run",
                        lambda cmd: (ended.append(cmd), 0)[1])
    sid = reg.allocate(backend="extension",
                       owner="attach", name="research")
    msg = session_create.end(reg.get(sid))
    assert any("end-session" in c and "--session" in c for c in ended)
    assert all("--name" not in c for c in ended)
    assert "still running" in msg.lower()
    assert reg.get(sid) is None
