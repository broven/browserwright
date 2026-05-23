"""Session ledger: id allocation, file lock, CRUD, prune."""
import threading

from browserwright import session_registry as reg


def test_allocate_increments_from_one(tmp_bs_home):
    a = reg.allocate(backend="extension", owner="attach")
    b = reg.allocate(backend="rdp", owner="create")
    assert a == "1"
    assert b == "2"
    assert reg.get("1")["backend"] == "extension"
    assert reg.get("2")["owner"] == "create"


def test_concurrent_allocate_unique(tmp_bs_home):
    ids, lock = [], threading.Lock()

    def worker():
        sid = reg.allocate(backend="rdp", owner="create")
        with lock:
            ids.append(sid)

    ts = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(ids)) == 20  # no dupes despite the race


def test_touch_updates_last_seen(tmp_bs_home, monkeypatch):
    sid = reg.allocate(backend="extension", owner="attach")
    monkeypatch.setattr(reg.time, "time", lambda: 9_999.0)
    reg.touch(sid)
    assert reg.get(sid)["last_seen"] == 9_999.0


def test_update_patches_fields(tmp_bs_home):
    sid = reg.allocate(backend="extension", owner="attach")
    reg.update(sid, workspace={"group_id": 7})
    assert reg.get(sid)["workspace"] == {"group_id": 7}


def test_remove_then_get_none(tmp_bs_home):
    sid = reg.allocate(backend="rdp", owner="create")
    assert reg.remove(sid)["id"] == sid
    assert reg.get(sid) is None


def test_list_all_returns_records(tmp_bs_home):
    reg.allocate(backend="extension", owner="attach")
    reg.allocate(backend="rdp", owner="create")
    rows = reg.list_all()
    assert {r["id"] for r in rows} == {"1", "2"}


def test_prune_drops_idle(tmp_bs_home):
    sid = reg.allocate(backend="rdp", owner="create")
    # make last_seen ancient
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))
    pruned = reg.prune(idle_seconds=3600)
    assert [p["id"] for p in pruned] == [sid]
    assert reg.get(sid) is None
