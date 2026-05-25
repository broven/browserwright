# Persistent per-session executor (phase B)

> Resident per-session executor that keeps a live Playwright `page`/`context`
> and a persistent `state` dict alive ACROSS `browserwright <<'PY' … PY`
> heredoc calls. Captured from task `05-25-phase-b-persistent-per-session-executor`.
> Builds on [Agent Surface: Playwright](./agent-playwright-surface.md) (phase C,
> per-heredoc connect) and [Playwright CDP Facade](./playwright-cdp-facade.md).

## 1. Scope / Trigger

Cross-layer + infra contract. Applies when touching: `_executor/*`
(`process.py`, `client.py`, `protocol.py`, `__main__.py`),
`daemon/server/executor_registry.py`, the `ensureExecutor`/`killExecutor` verbs
in `daemon/server/proxy.py`, the executor `_ipc` helpers in `daemon/_ipc.py`,
the heredoc routing in `repl/inline.py`, or `session_create.end()`.

**Why it exists:** phase C reconnected Playwright + re-bound the tab on *every*
heredoc (live objects died with the process; `state` could not exist). Phase B
makes a resident per-session subprocess hold the live objects, so `page`/
`context`/`state` survive across calls (playwriter's `Map<sessionId,executor>`
model, keyed per session to avoid the deleted global-REPL cross-talk).

## 2. Signatures

- Executor process: `python -m browserwright._executor --session <id>` — sync
  Playwright; **one dedicated worker thread** owns the thread-affine live
  `browser`/`context`/`page` + the persistent `state: dict`; an accept loop
  feeds it a FIFO queue (concurrent same-session calls serialize).
- Control plane (mode_b ws verb): `BrowserwrightDaemon.ensureExecutor {session}`
  → `{exec_sock: "<path>"}`. Lazy-spawns via `ExecutorRegistry.ensure(session)`
  (per-session `asyncio.Lock` single-flight; for rdp it `_ensure_upstream()`s
  the Chrome FIRST so the deferred cold-start can connect).
- Control plane: `BrowserwrightDaemon.killExecutor {session}` — reap without
  browser teardown (used by `session_create.end()` for attach sessions).
- Data plane (executor's own unix socket): length-framed JSON
  `ExecuteRequest{code, timeout_ms}` → `ExecuteResponse{console, return_value,
  warnings, screenshots, truncated, error, exit_code}`. Bulk code/output NEVER
  crosses the daemon event loop.
- Injected executor namespace (superset of phase C): live `page`/`context`/
  `snapshot` + persistent `state: dict` (by reference each call) + `reset()`.

## 3. Contracts

### Discovery files (`daemon/_ipc.py`)
- `executor_sock_path(sid)` = `_runtime_dir()/bw-exec-<sha256(sid)[:12]>.sock`,
  `executor_file_path(sid)` = same stem `.json` (`{sock,pid,session}`).
- **Short hash name is mandatory** — AF_UNIX `sun_path` 104-byte budget; raw
  session ids blow it. `_runtime_dir()` returns `$XDG_RUNTIME_DIR` (POSIX) or
  `/tmp` (NOT `tempfile.gettempdir()` → `/var/folders/...` on macOS, too long).
- Executor inherits the daemon's env (Popen has no `env=`), so daemon and
  executor resolve the SAME `_runtime_dir()` → cleanup matches the written file.

### DECOUPLED readiness (the load-bearing contract)
Executor `main()` order: start worker (idle) → `make_executor_socket` +
`write_executor_file` (socket LISTENING, discovery published) → SIGTERM cleanup
handler → accept loop. **Cold-start (`connect_over_cdp` + bind, with retry) is
deferred to the FIRST execute on the worker**, NOT done before publishing the
file. So `ensureExecutor` / `_await_ready` returns in ~sub-second (process
start + bind), and the slow 10–35s cold-start is absorbed by the data-plane
first call (a plain blocking socket, no keepalive). Client
(`run_on_executor`) adds cold-start slack to the first recv.

### Lifecycle (mirror the rdp-Chrome supervisor — "rdp Chrome v2")
Registry keyed by `session_id` on `Daemon.executors` (NOT on the shared
holder — extension sessions share one holder). Idle-reap via discovery-file
**mtime** (`_touch_discovery` after each served call; the data plane bypasses
the daemon so mtime is the only idle signal). `endSession`/`killExecutor` →
`registry.kill` (SIGTERM the process group, async SIGKILL escalation + zombie
reap, `_ipc.cleanup_executor`). Crash-reap (`reap_dead` via `proc.poll()`),
shutdown `kill_all`, startup `cleanup_orphan_executors` (only signals pids read
from our own `bw-exec-*.json`). `ensure()` treats a discovery file whose pid is
dead as ABSENT and purges it before respawn.

### Two `state`-loss paths (document for the agent)
1. `reset()` — rebuilds the connection (REUSING the live `sync_playwright`
   driver; disarm facade-death → drop browser/context/page refs → re-`connect_
   over_cdp` on the SAME driver → re-arm → `state.clear()`) .
2. Daemon restart / facade death — executor self-exits (`os._exit(0)` from the
   Playwright `disconnected` handler); next heredoc cold-starts fresh + rebinds
   the session tab via the ledger fast-path. `state` is gone (honest, documented).

### Coexistence with phase C (`repl/inline.py`)
Static `co_names` pre-check (scans nested code objects): a heredoc referencing
none of `{page, context, snapshot, state, reset}` runs IN-PROCESS unchanged
(pure `memory()`/`http_get`/site-skill never spawns an executor). Otherwise the
WHOLE body ships to the executor (a live cross-process `Page` can't return into
a local `exec`).

## 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| pure-memory heredoc (no surface names) | in-process exec, no executor spawn |
| concurrent first-heredocs, same session | single-flight `asyncio.Lock` → exactly one spawn |
| executor exits during startup (before bind) | `ensure` raises `RuntimeError` → `-32603` envelope (never crashes client ws) |
| cold-start fails on first execute | actionable `ExecuteResponse.error` (+ traceback), `_connected` stays False, next execute retries |
| per-call `timeout_ms` exceeded | client-side timeout error; worker finishes on its thread; later calls queue FIFO; process+state survive (hard wedge → `endSession`) |
| facade transport drops (daemon restart) | executor `os._exit(0)`; daemon crash-reaps; next ensure cold-starts fresh |
| stale `bw-exec-*.json` (dead pid) after restart | `_discovery_alive` False → purged before respawn |

## 5. Good / Base / Bad Cases

- **Good**: heredoc A `state['x']=1; page.goto(u)`; heredoc B reads `state['x']==1`
  and `page.url==u` on the SAME live page (no reconnect, same tab).
- **Base**: N browser heredocs → 1 executor, 1 tab, 1 facade connection; page
  count STABLE (rdp Chrome's built-in `about:blank` means `len(context.pages)`
  is ≥1 baseline — assert no GROWTH, not `==1`).
- **Bad**: shipping arbitrary code/output through the daemon's CDP-JSON-RPC loop
  (head-of-line + ws-keepalive risk); holding `ensureExecutor` open across the
  full cold-start; using a Playwright CDP session to map Page→targetId over the
  extension facade (fatal driver assert — use agent `Target.getTargets`).

## 6. Tests Required

- Unit (`tests/daemon/test_phase_b_{executor,registry,supervision}_unit.py`):
  routing pre-check (memory→in-process, page→ship); `ensure` ordering
  (`_ensure_upstream` before spawn; returns once socket listening WITHOUT
  cold-start); single-flight; idle/crash/shutdown/orphan reap + discovery
  unlink; lazy cold-start on first execute + reuse; cold-start failure →
  actionable error with retry; driver entered once (no re-enter on retry/reset);
  `reset()` disarm-old/re-arm-new WITHOUT `os._exit`; `endSession`/`killExecutor`
  pass `browser_session` to `_rpc_via_ws`.
- E2E (`tests/daemon/e2e/test_l2_phase_b_executor.py`, rdp + extension, CfT
  harness): cross-heredoc state/page persist; single-executor/single-tab;
  `reset()` clears state + page still works (process survives); `endSession`
  reaps executor (discovery file gone); daemon-restart cold-start rebinds;
  memory-only does not spawn.

## 7. Wrong vs Correct

### Wrong
```python
# In a test/helper: cli.py has NO `if __name__ == "__main__"` guard, so this
# runs NOTHING (rc 0, empty output) — `session end` silently never executes.
subprocess.run([sys.executable, "-m", "browserwright.cli", "session", "end", "--session", sid])

# ensureExecutor holding the client RPC open across the whole cold-start:
async def _handle_ensure_executor(...):
    await self._ensure_upstream()
    sock = await registry.ensure(session)   # waits 10–35s for connect+bind → ws keepalive trips
```

### Correct
```python
# Package __main__.py routes to cli.main(); use the package, not the submodule.
subprocess.run([sys.executable, "-m", "browserwright", "session", "end", "--session", sid])

# Executor publishes its socket immediately; cold-start is lazy on first execute.
# ensureExecutor returns ~sub-second; the data-plane first call absorbs cold-start.

# endSession/killExecutor MUST carry the ws ?session or _require_browser_session
# rejects them (-32602) before the kill runs:
await _rpc_via_ws(cfg, "BrowserwrightDaemon.endSession", {"session": sid},
                  client_label="cli-end-session", browser_session=args.session)
```

> **Gotcha**: `browser_session=` (not `params["session"]`) is what puts
> `?session=<id>` on `_rpc_via_ws`'s transient ws. The daemon verb's
> `_require_browser_session` keys off the ws query, not the JSON param.
