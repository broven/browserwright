# Session Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make multiple code agents drive browsers concurrently without interference (P1), behind a backend-transparent session API (P3), by introducing a registry-backed `session` as the isolation primary key.

**Architecture:** A short opaque incrementing **session id** is the isolation key. A file-locked **ledger** (`$BS_HOME/sessions/`) maps id → `{backend, daemon_endpoint, workspace, owner, name, last_seen}`. Creation is **explicit** (agent picks `extension` / `rdp --create` / `rdp --attach`); usage is **transparent** (every call just carries `--session <id>`). Extension = one shared daemon multiplexing per-session tab groups; RDP = one daemon per session (1:1). The global REPL daemon is removed (the silent cross-talk vector). Ownership rule: who `create`s, closes; `attach` only reminds.

**Tech Stack:** Python 3.11, pytest (+ pytest-asyncio for daemon), `fcntl` file locks, CDP over websockets. Two packages: `browserwright` (Layer 2) and `browserwright-daemon` (Layer 1).

**Source design:** `docs/plans/2026-05-20-session-model-design.md`. Code is the only source of truth; touchpoints cite `path:line`.

---

## Phase map & dependency order

| Phase | Scope | Package | Depends on |
|---|---|---|---|
| 0 | Session ledger (id alloc, file lock, CRUD, prune) | skill | — |
| 1 | `NoSession` error + remove default + `--session`/`BD_SESSION` threading | skill | 0 |
| 2 | CLI `session new\|end\|list\|prune` + `whoami` (extension + rdp creation) | skill | 0,1 |
| 3 | Remove global REPL daemon | skill | 1 |
| 4 | Backend capability interface + 3 bug fixes | daemon | — (parallel-safe) |
| 5 | Extension per-session bucketing (`_sessions`, group ownership, end cleanup) | daemon | 4 |
| 6 | RDP per-session daemon (create/attach launch + ownership/reminder) | daemon | 2,4 |
| 7 | Skill-memory decision layer (hit→auto, miss→ask+record) | skill | 2 |

**Phases 0–3 are fully specified below (bite-sized TDD).** Phases 4–7 are task-level breakdowns (exact files, test names, commands, code sketches, acceptance criteria); **expand each into bite-sized steps with a per-phase `writing-plans` pass before executing it**, re-reading the daemon internals it touches.

Commit after every passing task. Run skill tests with:
`( cd browserwright && .venv/bin/python -m pytest tests/<file>::<test> -v )`

---

## Phase 0 — Session ledger

A pure, daemon-free module. The single source of truth mapping a short id to its session record. File-locked so concurrent `session new` from parallel agents never collide on an id.

**Files:**
- Create: `browserwright/src/browserwright/session_registry.py`
- Test: `browserwright/tests/test_session_registry.py`

Ledger lives at `$BS_HOME/sessions/ledger.json` (BS_HOME default `~/.browserwright`, matching `session.py:63`). Shape:
```json
{"next_id": 3, "sessions": {
  "1": {"id":"1","backend":"extension","daemon_endpoint":"default",
        "workspace":null,"owner":"attach","name":"research",
        "created_at":1716200000.0,"last_seen":1716200500.0}}}
```

### Task 0.1: id allocation increments from 1

**Step 1 — failing test** (`tests/test_session_registry.py`):
```python
from browserwright import session_registry as reg

def test_allocate_increments_from_one(tmp_bs_home):
    a = reg.allocate(backend="extension", daemon_endpoint="default", owner="attach")
    b = reg.allocate(backend="rdp", daemon_endpoint="s-2", owner="create")
    assert a == "1"
    assert b == "2"
    assert reg.get("1")["backend"] == "extension"
    assert reg.get("2")["owner"] == "create"
```
**Step 2 — run, expect fail:** `pytest tests/test_session_registry.py::test_allocate_increments_from_one -v` → FAIL (module/func missing).

**Step 3 — implement** (`session_registry.py`):
```python
"""File-locked session ledger: short id → session record (P1 isolation key)."""
from __future__ import annotations
import fcntl, json, os, time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

def _home() -> Path:
    return Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browserwright")))

def _dir() -> Path:
    d = _home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _ledger_path() -> Path:
    return _dir() / "ledger.json"

@contextmanager
def _locked() -> Iterator[dict]:
    """Exclusive flock around a read-modify-write of the ledger."""
    lock = _dir() / ".lock"
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            p = _ledger_path()
            data = json.loads(p.read_text()) if p.exists() else {"next_id": 1, "sessions": {}}
            yield data
            p.write_text(json.dumps(data))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

def allocate(*, backend: str, daemon_endpoint: str, owner: str,
             workspace: Optional[object] = None, name: Optional[str] = None) -> str:
    now = time.time()
    with _locked() as data:
        sid = str(data["next_id"]); data["next_id"] += 1
        data["sessions"][sid] = {
            "id": sid, "backend": backend, "daemon_endpoint": daemon_endpoint,
            "workspace": workspace, "owner": owner, "name": name,
            "created_at": now, "last_seen": now,
        }
        return sid

def get(session_id: str) -> Optional[dict]:
    p = _ledger_path()
    if not p.exists():
        return None
    return json.loads(p.read_text())["sessions"].get(session_id)
```
**Step 4 — run, expect pass.** **Step 5 — commit:**
`git add browserwright/src/browserwright/session_registry.py browserwright/tests/test_session_registry.py && git commit -m "feat(skill): session ledger id allocation"`

### Task 0.2: concurrent allocate yields unique ids (lock works)
**Test:**
```python
import threading
def test_concurrent_allocate_unique(tmp_bs_home):
    ids, lock = [], threading.Lock()
    def worker():
        sid = reg.allocate(backend="rdp", daemon_endpoint="x", owner="create")
        with lock: ids.append(sid)
    ts = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(ids)) == 20  # no dupes despite the race
```
Run → should PASS already (flock). If flaky, that proves the lock is needed. Commit.

### Task 0.3: touch / update / remove / list_all / prune
**Tests** (one per behavior, bite-sized):
```python
def test_touch_updates_last_seen(tmp_bs_home, monkeypatch):
    sid = reg.allocate(backend="extension", daemon_endpoint="d", owner="attach")
    monkeypatch.setattr(reg.time, "time", lambda: 9_999.0)
    reg.touch(sid)
    assert reg.get(sid)["last_seen"] == 9_999.0

def test_update_patches_fields(tmp_bs_home):
    sid = reg.allocate(backend="extension", daemon_endpoint="d", owner="attach")
    reg.update(sid, workspace={"group_id": 7})
    assert reg.get(sid)["workspace"] == {"group_id": 7}

def test_remove_then_get_none(tmp_bs_home):
    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="create")
    assert reg.remove(sid)["id"] == sid
    assert reg.get(sid) is None

def test_prune_drops_idle(tmp_bs_home, monkeypatch):
    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="create")
    # make last_seen ancient
    reg._with_entry(sid, lambda e: e.update(last_seen=0.0))
    pruned = reg.prune(idle_seconds=3600)
    assert [p["id"] for p in pruned] == [sid]
    assert reg.get(sid) is None
```
**Implement** `touch`, `update`, `remove`, `list_all`, `prune`, and a small `_with_entry` helper, all under `_locked()`. Run each → PASS. Commit once green.

---

## Phase 1 — NoSession error, remove default, thread the id

Kill the silent-default vector: no session id → loud, actionable error. The daemon endpoint a call talks to comes from the **session record**, not the import-time `BD_NAME`.

**Files:**
- Modify: `browserwright/src/browserwright/errors.py` (add `NoSession`)
- Create: `browserwright/src/browserwright/session_ctx.py` (resolve current session)
- Modify: `browserwright/src/browserwright/mode_b_client.py:39` (drop `_DEFAULT_NAME` default)
- Modify: `browserwright/src/browserwright/repl/inline.py` (refuse at entry when no session)
- Test: `browserwright/tests/test_session_ctx.py`, update `tests/test_mode_b_client.py`

### Task 1.1: `NoSession` error
**Test** (`tests/test_session_ctx.py`):
```python
import pytest
from browserwright.errors import NoSession

def test_nosession_message_is_actionable():
    e = NoSession()
    assert e.exit_code == 2
    assert "session new" in str(e)
```
**Implement** in `errors.py` (mirror `DaemonUnavailable` style):
```python
class NoSession(BrowserSkillError):
    """No BD_SESSION provided. Refuse rather than silently sharing a browser (P1)."""
    exit_code = 2
    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(
            "no session: run `browserwright session new --backend <extension|rdp> ...` "
            "first, then pass --session <id> (or BD_SESSION=<id>) on every call. " + detail
        )
```
Run → PASS. Commit.

### Task 1.2: `resolve_session()` — env/arg → ledger record, else NoSession
**Test:**
```python
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
    monkeypatch.setenv("BD_SESSION", sid)
    rec = session_ctx.resolve_session()
    assert rec["id"] == sid and rec["backend"] == "extension"
```
**Implement** `session_ctx.py`: read explicit arg → fallback `os.environ["BD_SESSION"]`; `reg.get`; raise `NoSession` if absent/unknown; `reg.touch(sid)` on success; return record.

Run → PASS. Commit.

### Task 1.3: drop the import-time default in `mode_b_client.py`
**Context:** `mode_b_client.py:39` `_DEFAULT_NAME = os.environ.get("BD_NAME", "default")` freezes identity at import — the cross-talk root.

**Test** (update `tests/test_mode_b_client.py`): assert that constructing the client for a session uses the session record's `daemon_endpoint`, and that no `"default"` fallback exists.
```python
def test_client_uses_session_daemon_endpoint(tmp_bs_home, monkeypatch):
    from browserwright import session_registry as reg, mode_b_client
    sid = reg.allocate(backend="rdp", daemon_endpoint="browserwright-daemon-s7.sock", owner="create")
    c = mode_b_client.client_for_session(reg.get(sid))
    assert c.name == "browserwright-daemon-s7.sock"  # not "default"
```
**Implement:** add `client_for_session(record) -> ModeBClient` that builds the endpoint from `record["daemon_endpoint"]`; remove the module-level `_DEFAULT_NAME` default (callers must pass a name/endpoint). Update `auto_client()` callers accordingly (grep usages first). Run → PASS. Commit.

> NOTE for executor: `Session.__init__` (`session.py:50-52`) calls `auto_client()`. Rework so a Session is constructed from a resolved session record (thread `record` into `Session`). Re-read `session.py` + `daemon_client.py` at task time; this is the one structural seam in Phase 1.

### Task 1.4: entrypoint refuses with no session
**Test:** invoking inline run with no `BD_SESSION` exits 2 and prints the NoSession guidance. Use a subprocess or call `inline.run` with a patched env.
**Implement:** in `repl/inline.py` `run()`, call `session_ctx.resolve_session()` early; on `NoSession`, print to stderr and return `exc.exit_code`. Run → PASS. Commit.

---

## Phase 2 — CLI `session` subcommands + `whoami`

**Files:**
- Modify: `browserwright/src/browserwright/cli.py` (add `_cmd_session`, `_cmd_whoami`, dispatch + HELP)
- Create: `browserwright/src/browserwright/session_create.py` (creation logic per mode)
- Test: update `browserwright/tests/test_cli.py`

### Task 2.1: `session new --backend extension` registers an attach session
**Test** (`tests/test_cli.py`): call `cli.main(["session","new","--backend=extension","--name=research"])`, capture stdout, assert it prints a bare id (`1`), and the ledger has one extension/attach entry named research.
**Implement** `session_create.new(backend, mode, name, target)`:
- extension → `reg.allocate(backend="extension", daemon_endpoint=<shared default>, owner="attach", name=name)`; workspace (group) is lazily created on first `new_page`/`attach_active` (Phase 5), so leave `workspace=None`.
- print `sid` to stdout (token-frugal: just the number).
`_cmd_session` parses `new|end|list|prune`. Run → PASS. Commit.

### Task 2.2: `session new --backend rdp --create` and `--attach`
**Tests:** `--create` allocates owner=`create`; `--attach <port>` allocates owner=`attach` with the target recorded. (Daemon launch is stubbed/monkeypatched here; real launch lands in Phase 6.)
**Implement:** in `session_create.new`, branch rdp create vs attach; record `owner` accordingly; stash `target` (port/recipe) in `workspace`. Run → PASS. Commit.

### Task 2.3: `session end` honors ownership + reminder
**Tests:**
- create-owned session → `session end` calls the (stubbed) browser-close path and removes the ledger entry, exit 0.
- attach session → `session end` does **not** close, prints a reminder mentioning the browser is still running, removes the ledger entry, exit 0.
```python
def test_session_end_attach_emits_reminder(tmp_bs_home, capsys, monkeypatch):
    from browserwright import session_registry as reg
    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="attach", name="fp")
    cli.main(["session","end",f"--session={sid}"])
    out = capsys.readouterr().out
    assert "still running" in out.lower()
    assert reg.get(sid) is None
```
**Implement** `session_create.end(record)`: if `owner=="create"` → close browser/daemon (stub now, Phase 6 wires real); else print reminder. Always `reg.remove`. Run → PASS. Commit.

### Task 2.4: `session list` / `session prune` / `whoami`
**Tests:** `list` prints rows from `reg.list_all()`; `prune` drops idle and reports count; `whoami --session <id>` prints `{id, backend, owner, name, daemon_endpoint}` (the live-browser fields — group/tab count/sample URL — are filled in Phase 5/6 via a daemon round-trip; for now print the ledger view + a `TODO: live` marker is NOT acceptable — print only ledger-known fields).
**Implement** `_cmd_whoami` reading the record; `_cmd_session` list/prune. Update `HELP`. Run → PASS. Commit.

---

## Phase 3 — Remove the global REPL daemon

The cross-process REPL daemon froze `BD_NAME`/backend into a shared singleton and forwarded heredocs without env (`repl/inline.py:36`, `repl/client.py:59`, `repl/server.py:174`) — the documented cross-talk accident. Remove it; keep process-local execution within one heredoc.

**Files:**
- Modify: `repl/inline.py` (drop the "REPL daemon running → forward" branch; always run in-process)
- Delete/retire: `repl/server.py`, `repl/client.py`, `repl/_proto.py` (and `_cmd_repl` start/stop/status/exec in `cli.py`)
- Modify: `install.py:578/581/602/626` (remove `repl start` recommendation)
- Tests: retire `tests/test_repl_protocol.py`; update `tests/test_cli.py` (repl subcommands gone)

### Tasks (bite-sized):
1. **Test:** `inline.run` never consults a REPL socket (assert no socket connect; monkeypatch to fail if `is_repl_running` referenced). Implement: delete the forwarding branch at `inline.py:36`. Commit.
2. **Test:** `cli.main(["repl","start"])` returns a clear "removed" message + nonzero, or the subcommand is gone (decide: hard-remove). Implement: remove `_cmd_repl` and its dispatch; update HELP. Commit.
3. **Test:** `install.run()` output/recommendation no longer mentions `repl start` (grep the generated text). Implement: edit `install.py` lines. Commit.
4. Delete `repl/server.py`, `repl/client.py`, `repl/_proto.py`, `tests/test_repl_protocol.py`; run full skill suite green. Commit.

> Keep `repl/inline.py` + `repl/_namespace.py` (process-local exec path is the supported heredoc runtime).

---

## Phase 4 — Backend capability interface + 3 bug fixes (daemon) — TASK-LEVEL

> Expand into bite-sized steps with a `writing-plans` pass after re-reading `server/proxy.py`, `backends/extension.py`, `active_tab.py`, `server/extension_upstream.py`.

**Capability interface.** Extend the existing structural `Backend` Protocol (`backends/base.py:69`) with the session-model verbs (keep it a Protocol — no ABC, matches house style):
```python
# additions to base.py
def caps(self) -> dict: ...        # {"owns_browser": bool, "supports_browser_context": bool}
async def workspace_create(self, session_id: str) -> dict: ...   # extension: tab group; rdp: noop/launch
async def workspace_attach(self, session_id: str, target) -> dict: ...
async def page_new(self, session_id: str, url: str) -> dict: ...
async def page_attach_active(self, session_id: str) -> dict: ...
```
extension and rdp each implement; this is what Phases 5/6 fill in.

**Bug fixes (each its own test + commit):**
- **4a** `server/proxy.py:676-681` `getBackendInfo` returns hardcoded `kind:"UPSTREAM_WS"`. Test (daemon, `tests/test_extension_upstream.py` style): under extension backend, `getBackendInfo` reports `kind` = the real backend kind (`LOCAL_RELAY`). Fix the handler to read the live backend.
- **4b** `backends/extension.py` `resolve()` always raises `Unavailable`, and `active_tab.py` calls it → `active-tab` always fails under extension. Test (`tests/test_active_tab.py`): extension `active-tab` routes through the relay path, not `resolve()`. Fix `active_tab.py` to branch to relay for extension.
- **4c** `server/extension_upstream.py:258-262`: unsupported `Target.createTarget` returns misleading `-32601 "requires a sessionId"`. Test (`tests/test_extension_upstream_errors.py`): `Target.createTarget` fast-fails with a message naming `new_page`/`openBackgroundTab`. Fix the error branch.

Run: `( cd browserwright-daemon && .venv/bin/python -m pytest tests/test_extension_upstream.py tests/test_active_tab.py tests/test_extension_upstream_errors.py -v )`.

---

## Phase 5 — Extension per-session bucketing (daemon) — TASK-LEVEL

> Expand after re-reading `server/extension_upstream.py` (esp. `_sessions` at :119) and `server/relay.py` (`create_background_tab` :310, `attach_active_tab` :214, `_extension_for_tab` :401).

**Goal:** `_sessions` and tab/group ownership become **per session_id**; cross-session tabs are invisible/undetachable (P1). Each session lazily owns one tab group; end cleans only its own tabs.

**Tasks:**
1. **Bucket `_sessions` by session_id.** Change `ExtensionUpstream._sessions: dict[str,int]` → `dict[session_id, dict[sid,int]]` (or a `(session_id, sid)` keyspace). Test: session A's fabricated sid is not resolvable from session B; detach from B can't reach A's tab.
2. **Group ownership.** `create_background_tab(group_name=...)` (relay.py:310) → key the created group by session_id; persist `group_id` back into the ledger via the daemon→skill response (skill calls `reg.update(sid, workspace={"group_id":...})`). Test: two sessions get distinct group ids; tabs land in the right group.
3. **`attach_active` into session group + borrow flag.** `attach_active_tab` (relay.py:214) pulls the focused tab into the caller session's group and marks it **borrowed**. Test: borrowed tab recorded as borrowed, not owned.
4. **End cleanup.** New daemon verb `BrowserDaemon.endSession(session_id)`: close session-owned tabs, **ungroup** (not close) borrowed tabs, drop the group. Test: owned tabs closed, borrowed tabs survive + ungrouped.
5. **whoami live fields.** Daemon answers group id, owned-tab count, a sample URL for a session; `whoami` (Phase 2.4) now fills these. Test: round-trip shape.

Tests live in `tests/test_extension_upstream.py`, `tests/test_multiclient.py`, new `tests/test_session_isolation.py`. Use the existing fake-extension fixtures (`ai-e2e-tests/fake_extension.py` / daemon `tests/conftest.py`).

---

## Phase 6 — RDP per-session daemon: create/attach launch (daemon+skill) — TASK-LEVEL

> Expand after re-reading `launch_chrome.py`, `backends/rdp.py`, `server/listener.py` (single-upstream holder).

**Goal:** `session new --backend rdp` binds a session 1:1 to a daemon+browser. `--create` launches an isolated Chrome (reuse `launch-chrome`) + a daemon named for the session; `--attach <port|recipe>` attaches to an already-running browser (fingerprint). `session end` closes only create-owned browsers; attach emits the reminder (wired in Phase 2.3, now real).

**Tasks:**
1. `session_create.new` (rdp create): call `browserwright-daemon launch-chrome --port <p> --profile <sid>` + start/point a daemon; record its socket as `daemon_endpoint`, `owner="create"`. Test (subprocess mocked): ledger endpoint set, owner=create.
2. (rdp attach): given a port/recipe, point a daemon at it; `owner="attach"`. For fingerprint recipes, the launch recipe comes from skill memory (Phase 7). Test: owner=attach, target recorded.
3. `session end` (create): stop daemon + kill the launched Chrome. Test: close path invoked. (attach: reminder only — already covered 2.3.)
4. Reaper: `session prune` also stops orphaned create-owned daemons whose ledger entry is idle past timeout. Test: idle create session → browser/daemon stopped.

---

## Phase 7 — Skill-memory decision layer — TASK-LEVEL

> Expand after re-reading `memory/global_mem.py` + `memory/_md.py`.

**Goal:** A `session-decisions` memory namespace records "situation → how to start a session" (incl. fingerprint launch recipes/ports). Interaction: **hit → auto-start; miss → ask user, then record**.

**Tasks:**
1. New memory accessor `memory/session_decisions.py` (mirror `global_mem.py`): `lookup(situation) -> decision|None`, `record(situation, decision)`. Test (tmp_bs_home): record then lookup round-trips.
2. `session_create.choose(situation) -> decision`: on hit return it; on miss raise `NeedsUserConfirm` (reuse `errors.py:99`) with a proposal listing the three modes. Test: miss raises NeedsUserConfirm naming extension/rdp-create/rdp-attach.
3. Wire `choose` into the agent flow + SKILL.md guidance ("before session new, consult decision memory; if absent, ask which browser and record"). Update `skill/SKILL.md` + `browserwright/SKILL.md`. Test: guidance text present (grep) — and add an `ai-e2e-tests` case mirroring the existing Case-pattern for ask-first.

---

## Testing infrastructure notes (for the executor)

- Skill unit tests use the **stub-session** pattern (`tests/test_session_propagation.py`): monkeypatch `session._singleton` with a stub exposing `.cdp` (a recorder) + `.backend_name`. No live daemon.
- `tmp_bs_home` fixture (`tests/conftest.py:8`) gives a temp `$BS_HOME` and resets memory singletons — use it for every ledger/memory test.
- Daemon tests use `pytest-asyncio` (`asyncio_mode=auto`) + fake extension/relay fixtures. `RelayServer(port=0)` binds ephemeral (relay.py:70).
- Run a package's full suite before declaring a phase done:
  `( cd browserwright && .venv/bin/python -m pytest -q )` / `( cd browserwright-daemon && .venv/bin/python -m pytest -q )`.

## Risks / watch-items

- **Phase 1 seam:** `Session` bootstrap (`session.py`) and `auto_client()` currently derive identity from `BD_NAME`. Rerouting through a session record is the riskiest refactor; do it behind tests and grep all `auto_client()` / `current_session()` callers first.
- **id reuse:** ids are monotonic and not reused while the ledger persists (avoids stale-handle aliasing). Optional: reset `next_id` to 1 when `sessions` empties — only if tests pin it.
- **Ledger vs daemon truth:** the ledger is skill-side bookkeeping; the daemon owns live tab/group truth. `whoami` must read live fields from the daemon, not trust stale ledger `workspace`.
- **Existing tests encode old behavior:** `test_session_propagation.py`, `test_session_concurrency.py`, `test_p1_coverage_gaps.py`, `test_multitask.py`, `test_repl_protocol.py` will need updates/retirement — treat their changes as part of the owning phase, not afterthoughts.
