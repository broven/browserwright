# Research: Phase B executor — Fork 1 (lifecycle), Fork 2 (transport), Fork 7 (phase C coexistence)

- **Query**: Resolve Fork 1/2/7 of the persistent per-session executor with codebase-backed recommendations.
- **Scope**: internal (browserwright daemon/repl/session), plus prior playwriter research notes.
- **Date**: 2026-05-25

> D1/D2 are settled: executor = independent per-session **sync-Playwright subprocess** holding live page/context/browser + persistent `state` + long-lived facade `connect_over_cdp`. NOT inside the asyncio daemon.

---

## TL;DR recommendations

- **Fork 1 → option (a): the daemon supervises the executor subprocess.** There is direct, working precedent: the daemon already spawns + tracks + SIGTERMs a per-session child process (rdp Chrome). The "single global daemon" philosophy is about *one socket / one identity / no `BD_NAME`*, NOT about avoiding child processes — the daemon is explicitly already a process manager. Reuse the rdp-Chrome lifecycle pattern verbatim.
- **Fork 2 → option (b) with daemon-brokered discovery: executor opens its OWN per-session unix socket; the thin heredoc client connects directly.** The async daemon's CDP-JSON-RPC framing is a poor fit for shipping arbitrary code + streaming stdout + large screenshot payloads, and forwarding would put agent output on the daemon's critical path. But the *discovery* of that socket (and the *spawn*) goes through the daemon (Fork 1a), via a per-session `_ipc`-style discovery file the daemon writes when it spawns the executor. This is a hybrid: daemon owns lifecycle (1a), executor owns the code transport (2b).
- **Fork 7 → coexist, not replace.** Keep `inline.py`'s in-process `exec` path for the lightweight case. A heredoc that never touches `page`/`context` (pure `memory()`/site-skill/`http_get`) must NOT spawn or contact an executor. The decision is made by the SAME lazy trigger that exists today (`_LazyHandleProxy` first-access). `playwright_handle.py`'s connect+bind logic migrates *into* the executor; `inline.py` keeps its lazy-namespace shape but the lazy proxy now forwards to the executor instead of an in-process `PlaywrightHandle`.

---

## Fork 1 — executor lifecycle ownership: **daemon supervises (a)**

### Evidence: the daemon is ALREADY a per-session child-process manager

The "single global daemon" refactor did NOT make the daemon process-averse — it made the daemon the *owner* of per-session Chrome subprocesses. Phase 3 (C2) of that refactor is the exact blueprint Phase B needs:

- **Spawn**: `daemon/server/listener.py:690 _launch_rdp_chrome()` lazily launches a per-session Chrome on first client frame, idempotent on `rdp_pid` (`listener.py:709-710`), pins a free port onto the holder cfg (`listener.py:713-732`). It calls `launch_chrome.launch_chrome` **in-process** specifically "so the spawned Chrome's pid is visible to us for teardown" (`listener.py:705-707`).
- **Track**: the launched pid/profile/port are stored on the per-session holder: `_UpstreamHolder.rdp_pid / rdp_profile_dir / rdp_port` (`listener.py:528-530`, set at `listener.py:739-742`).
- **Spawn mechanics**: `launch_chrome.py:118 subprocess.Popen(..., **_spawn_kwargs())` where `_spawn_kwargs()` returns `{"start_new_session": True}` on POSIX / `CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW` on Windows (`launch_chrome.py:343-347`). pid file written under `runtime_dir()` (`launch_chrome.py:147-150`).
- **Kill on endSession**: `BrowserwrightDaemon.endSession` → `daemon.teardown_rdp_context()` (`daemon.py:194`) → `holder.trigger_close("skill_disconnect")` which calls `_kill_rdp_chrome()` (`listener.py:744-761`, invoked from `trigger_close` at `listener.py:924-925`). Verb wiring at `proxy.py:1050-1051`, `proxy.py:1344-1356`.
- **Kill on idle**: `_idle_watchdog()` (`listener.py:959-994`) closes per-context upstreams after `idle_close_after`, then `drop_rdp_context` (`listener.py:991-992`). The kill happens via the same `trigger_close` path.
- **Kill on shutdown**: `_graceful_shutdown()` iterates `daemon.all_contexts()` and `trigger_close("daemon_shutdown")` each (`listener.py:997-1004`).
- **Crash detection**: `_on_upstream_closed()` translates an upstream drop (Chrome died) into a close + `daemon.drop_rdp_context()` (`listener.py:933-953`).
- **Orphan sweep on restart**: `_cleanup_orphan_rdp_chrome()` (`listener.py:249-297`) SIGTERMs stray Chromes from a prior crash via their `SingletonLock` symlink and removes profile dirs, run at the top of `run_serve` (`listener.py:106`).

**Every single Phase B lifecycle requirement (lazy spawn / idle reap / endSession kill / crash-drop / restart-sweep) already has a working twin in the rdp-Chrome path.** The executor is "rdp Chrome v2": a different child binary, same supervision contract.

### Does (a) conflict with "single global daemon"?

No. `docs/refactor-single-daemon.md` defines the philosophy precisely (§"Target model", lines 16-33):
1. "One global daemon. Fixed endpoint, no name. `BD_NAME` is deleted." (about identity/discovery, line 16-19)
3. "**the daemon itself launches and owns** the per-session Chrome (independent port + profile `bs-s{id}`), and tears it down on session end." (line 27-28)

Point 3 is an explicit license to spawn+own per-session subprocesses. The P3 lesson the PRD cites (`prd.md:15`) is "don't freeze backend into a **shared singleton** + forward heredocs without their env" — that is an argument *against* one shared in-daemon REPL, and *for* per-session keying. Option (a) keys the executor by `session_id` exactly like `Daemon.contexts: dict[session_id, UpstreamContext]` (`daemon.py:87`), so it satisfies the P3 lesson.

### Why (a) over (b) — first-heredoc self-spawn

Option (b) (heredoc self-spawns a detached executor + per-session discovery file) is *feasible* (the `_ipc` atomic-write pattern at `_ipc.py:95-112` and `mode_b_client._spawn_daemon` at `mode_b_client.py:478-498` show both halves exist) but loses the things (a) gives for free:
- Centralized idle-reap (`_idle_watchdog`) and shutdown-kill (`_graceful_shutdown`) already iterate `all_contexts()` — a daemon-owned executor slots into the same loop. A self-spawned detached executor needs its own idle timer + a SIGTERM-from-nowhere reaper.
- `endSession` is a daemon verb (`proxy.py:1050`); the daemon must kill the executor there anyway, so the daemon must already *know about* the executor — at which point owning the spawn is strictly simpler than discovering a process it didn't start.
- Restart-sweep: orphan executors after a daemon SIGKILL need the same `_cleanup_orphan_rdp_chrome`-style sweep; daemon ownership lets that sweep reuse the existing profiles-root iteration shape.

### Recommended mechanism (sketch)

1. **Registry**: add an executor handle to the per-session unit. Cleanest: a parallel `dict[session_id, ExecutorHandle]` on `Daemon` (mirrors `Daemon.contexts`, `daemon.py:87`), since the executor is needed for BOTH extension sessions (shared context, no per-session `UpstreamContext`) and rdp sessions. Do **not** hang it off `_UpstreamHolder` — extension sessions multiplex onto one shared holder (`daemon.py:119-128`), so a per-session executor cannot live on the shared holder.
2. **Lazy spawn**: triggered when the executor is first needed (see Fork 2 — the thin client asks the daemon to `ensureExecutor(session)` before it has a socket to talk to). Spawn with `subprocess.Popen(..., start_new_session=True)` copying the `launch_chrome` pattern; the child is `python -m browserwright._executor --session <id>` (new module).
3. **Discovery**: the daemon (or the executor itself, then daemon reads it) writes a per-session discovery file `{_PREFIX}-exec-{session}.sock` / `.json` under `_ipc._runtime_dir()` (extend `_ipc.py` with `executor_sock_path(session)` / `read_executor_file(session)` mirroring `facade_path`/`read_facade_file` at `_ipc.py:84-112`). **NOTE the AF_UNIX 104-byte budget** (`_ipc.py:46-50`): `_runtime_dir()` is `/tmp` on macOS for exactly this reason — keep the per-session socket name short (`bw-exec-<shortid>.sock`).
4. **Idle reap**: extend `_idle_watchdog` (`listener.py:959`) or add a sibling watchdog that SIGTERMs executors idle past a threshold; track `last_execute_at` on the handle.
5. **endSession kill**: in `_handle_end_session` (`proxy.py:1318`), before/after the existing rdp teardown, SIGTERM the session's executor and `pop` it from the registry. Symmetric for the extension branch (which currently goes through `_end_session` callback at `proxy.py:1359-1388`).
6. **Crash-drop**: the daemon reaps the child (poll `proc.poll()` or `os.waitpid` on a reaper task); a dead executor is removed from the registry so the next heredoc cold-starts a fresh one.
7. **Restart-sweep**: add executor pidfiles to the same orphan sweep as `_cleanup_orphan_rdp_chrome` (`listener.py:249`).

### New risk surfaced

- **Spawn race / double-spawn**: two concurrent first-heredocs for the same session could both trigger `ensureExecutor`. The rdp path guards this with `if self.rdp_pid is not None: return` under the holder's `_open_lock` (`listener.py:582,709`). The executor spawn needs the same single-flight lock keyed per session (an `asyncio.Lock` per registry slot).

---

## Fork 2 — executor⇆client transport: **executor's own per-session unix socket (b), daemon-brokered discovery**

### How mode_b transport works today (and why forwarding code through it is awkward)

The existing CLI↔daemon channel is a **CDP JSON-RPC websocket** over a unix socket:
- Discovery: `mode_b_client.discover()` (`mode_b_client.py:70-107`) → `ws+unix://<sock>?client=...&session=<id>` (`mode_b_client.py:189-198`).
- It is fundamentally a **CDP command tunnel**: "Standard CDP commands are tunnelled through. `BrowserwrightDaemon.*` RPCs ... are answered by the daemon itself" (`mode_b_client.py:5-9`). The daemon's `Router.route_from_client` (`listener.py:483`) expects CDP-shaped frames; `BrowserwrightDaemon.*` verbs are self-answered in `proxy.py:810-1057`.
- The daemon is **asyncio** (`listener.py:run_serve`), and every client frame is processed on the daemon event loop (`listener.py:478-483`).

Adding a `BrowserwrightDaemon.executeCode` verb (option a) means:
- Arbitrary agent code (potentially large) rides as a JSON-RPC param through the daemon's event loop.
- The **response** must carry the full playwriter-style output block (console + `[return value]` + `[WARNING]` + per-screenshot path/a11y blocks + truncation) — screenshots are large; `max_size=100*1024*1024` is already configured (`listener.py:338,357`) but pumping multi-MB image payloads through the privileged daemon's loop is exactly the kind of head-of-line/timeout coupling D1 wanted to avoid.
- The daemon would have to **forward** to the executor and relay the streamed result back — a second hop that re-frames everything. The daemon would become a proxy for content it has no reason to inspect.

### Why (b) — executor's own socket — is cleaner

- **One less hop on the hot path**: thin heredoc client → executor socket directly. The daemon stays a pure CDP proxy + the facade provider; agent code/output never touches its event loop.
- **Protocol freedom**: the executor socket speaks a *simple line/length-framed request-response* of OUR design (ship `code` + `timeout`; receive the structured output block). It does NOT need to pretend to be CDP. This matches playwriter's model: "CLI is a thin HTTP client: `playwriter -s <id> -e <code>` POST `/cli/execute` to relay" (`playwriter-exposure.md:32`). We use a unix socket instead of HTTP, but the shape is identical: thin client → resident per-session executor → run → return output.
- **Sync executor, sync client**: the executor runs sync Playwright (D1). A plain blocking unix socket server in the executor pairs naturally with the heredoc's already-sync world (`mode_b_client` is "plain sockets, not asyncio" per `playwright_handle.py:30-32`). No sync-in-async bridge.

### The hybrid: daemon brokers spawn + discovery, executor owns the code channel

The thin client cannot connect to a socket that doesn't exist yet. Sequence:
1. Heredoc client needs the executor → sends `BrowserwrightDaemon.ensureExecutor {session}` over the EXISTING mode_b socket (cheap control-plane RPC, tiny payload). Daemon (Fork 1a) spawns the executor if absent, waits for it to bind + write its discovery file, returns `{exec_sock: "<path>"}` (or the client reads `_ipc.read_executor_file(session)` itself after a ready signal).
2. Client connects DIRECTLY to `<exec_sock>` and sends `{code, timeout}`.
3. Executor runs the code in its persistent namespace, returns the output block.

So: **control plane (spawn/discover/kill) = daemon over mode_b (option-a flavor); data plane (code+output) = executor's own socket (option-b flavor).** This keeps the daemon authoritative over lifecycle (Fork 1) while keeping bulk/streaming data off its loop.

### Output protocol (what our execute response carries)

Mirror playwriter (`playwriter-exposure.md:7`): a single response object with
- `console`: captured stdout/stderr (the executor wraps exec in `redirect_stdout` like `inline.py:47`).
- `return_value`: repr of the last expression / explicit return (playwriter's `[return value]`).
- `warnings`: list (e.g. popup-became-tab notices — `[WARNING]`).
- `screenshots`: list of `{path, a11y_snapshot?}` blocks (each large; this is the payload that argues hardest for option b).
- `truncated`: bool + a truncation cap (playwriter truncates the text block at 10000 chars).
- `error`: serialized exception (reuse `errors.serialize`, `inline.py:18,51`) with a hint to call `reset()` on connection/page failure (`playwriter-exposure.md:7`).
- Per-call **timeout** (playwriter default 10000ms) enforced executor-side; on timeout the executor returns a timeout error but the process + `state` survive (the queue, see below, must not wedge).

### New risk surfaced

- **Socket auth**: the mode_b unix socket relies on 0600 perms (`_ipc.py:316-322`). The per-session executor socket must bind with the same `umask(0o077)` discipline (`make_unix_socket` at `_ipc.py:306-324` is reusable). On Windows there is no AF_UNIX in the mode_b path either (it uses TCP+token, `_ipc.py:327-339`); the executor will need the same TCP+token fallback on Windows — non-trivial, flag for the implementer.

---

## Fork 7 — phase C lazy-connect coexistence: **coexist; migrate connect/bind into executor; keep in-process path for non-browser heredocs**

### What `inline.py` does today (the path that must stay for lightweight heredocs)

`repl/inline.py:run()`:
- Reads code, resolves+sets the session (`inline.py:36-41`).
- `globals_ = _namespace.build_globals()` (`inline.py:45`).
- `exec(..., globals_)` under `redirect_stdout` (`inline.py:47-48`).
- `finally`: tears down the Playwright handle IFF it connected — `handle = globals_.get("__bw_playwright_handle__"); handle.close()` (`inline.py:60-71`). The comment is explicit: "A no-op when `page`/`context` were never accessed (nothing connected)."

`_namespace.build_globals()` (`_namespace.py:74-106`):
- Injects all `browserwright.EXPORTS` + stdlib + agent helpers.
- Injects the lazy Playwright surface: `PlaywrightHandle()` + two `_LazyHandleProxy(handle, "page"|"context")` + `__bw_playwright_handle__` + `snapshot = make_snapshot(handle)` (`_namespace.py:92-103`).

`_LazyHandleProxy` (`playwright_handle.py:283-319`) is a transparent proxy that triggers the real connect on FIRST attribute access. The design comment is the whole point: **"a pure `memory()` heredoc that never touches them never triggers the connect"** (`playwright_handle.py:288-290`, echoed `_namespace.py:86-88`).

### Recommendation: keep the split, move the boundary

**The lazy-trigger boundary that today separates "no browser" from "connect facade" becomes the boundary that separates "no executor" from "talk to executor."** Concretely:

1. **`inline.py` keeps its shape**: still `build_globals()` + in-process `exec` + `finally` teardown. A pure `memory()`/site-skill/`http_get` heredoc runs entirely in the heredoc subprocess, never contacts the executor, never spawns one. This satisfies the PRD requirement "纯 memory()/site-skill heredoc 不应被迫拉起 executor" (`prd.md:47`).
2. **The lazy proxy now forwards to the executor, not an in-process `PlaywrightHandle`.** On first `page`/`context` access, instead of `PlaywrightHandle._ensure_connected()` doing `connect_over_cdp` locally (`playwright_handle.py:117-145`), the proxy performs the Fork-2 dance: `ensureExecutor(session)` → connect to exec socket. But here is the key architectural shift:

   **The agent's code does NOT run in `inline.py`'s process anymore once it touches `page`.** You cannot return a live cross-process `Page` object into the local `exec`. So the model is: if a heredoc uses `page`/`context`/`snapshot`/`state`, the WHOLE heredoc body must be shipped to the executor and run THERE (where the live objects live), exactly like playwriter ships `code` to the resident executor.

   This means `inline.py` needs a routing decision it cannot make purely lazily (it can't know mid-exec that line 5 touches `page`). Two viable resolutions:
   - **(7a) Always ship to executor, executor decides laziness.** `inline.py` becomes the thin client: it sends the entire code blob to the executor; the executor runs it in a namespace where `page`/`context`/`state` are the live persistent objects and `memory()`/`http_get`/etc. are also available. The executor is spawned lazily by the daemon, but a heredoc that only calls `memory()` would still round-trip to the executor. To honor `prd.md:47` (pure-memory heredocs stay lightweight), add a **cheap static pre-check** in `inline.py`: if the code references none of `{page, context, snapshot, state, reset}` (simple `re`/`tokenize` scan, or compile + inspect `co_names`), run it in-process via today's `build_globals` path and skip the executor entirely. Otherwise ship to the executor. This is the recommended split: deterministic, no executor for pure-memory, and the executor namespace is a superset so shipped code still has the full surface.
   - **(7b) Lazy spawn but local-then-handoff** — rejected: you cannot migrate a half-run local namespace into a subprocess.

3. **Migration of `playwright_handle.py` internals INTO the executor:**
   - `_ensure_connected` / `_facade_ws_url` / `connect_over_cdp` (`playwright_handle.py:55-145`) → run ONCE at executor cold-start (not per heredoc). The executor holds `self._browser/_context/_page` for its lifetime.
   - `_bind_current_page` + `_page_for_target` + `_agent_page_targets` (`playwright_handle.py:147-227`) → run at executor cold-start AND on `reset()`/recovery, NOT per heredoc. The fatal "never use a Playwright CDP session over the extension facade" constraint (`playwright_handle.py:73-97,166-171`) moves verbatim into the executor; the executor uses the agent CDP path (`sess.cdp.send("Target.getTargets")`) for target enumeration just as today.
   - The ledger fast-path `ensure_session_target` (`session_runtime.py:86-137`) — `current_target_id` reuse → `recoverSession` by groupId — is used by the executor ONLY at cold-start/recovery to bind the session's current tab, then the live `Page` is held in-process. Per-heredoc reuse is now "same live object", not "re-resolve from ledger" (the whole Phase B win, `prd.md:41`).
   - `close()` (`playwright_handle.py:243-269`) — its "disconnect transport, never close user tabs" discipline moves to executor SHUTDOWN (endSession/idle/crash), not heredoc end.

4. **`inline.py`'s `finally` teardown changes**: when code is shipped to the executor, `inline.py` no longer owns a `PlaywrightHandle`, so the `handle.close()` finally (`inline.py:60-71`) becomes a no-op for the shipped path (the executor owns teardown). For the in-process pure-memory path it stays exactly as-is. Net: the `finally` block guards `globals_.get("__bw_playwright_handle__")` which is only injected on the in-process path — so it naturally degrades to a no-op when the executor path is taken and no handle was built locally.

5. **`build_globals()` injection in the EXECUTOR**: the executor builds its namespace ONCE (or refreshes per call by reference). Reuse `_namespace.build_globals()` but with two changes (per `prd.md:35` Fork 5): replace the lazy `_LazyHandleProxy` `page`/`context` with the executor's LIVE held objects, and add the persistent `state` dict injected by reference each call (playwriter parity, `playwriter-exposure.md:13`). `snapshot = make_snapshot(handle)` (`_namespace.py:102-103`) rebinds to the executor's live page/handle.

### `state` footgun note

Phase C deliberately did NOT inject `state` "怕空 dict footgun" (`prd.md:12`). Phase B injecting `state` is safe specifically BECAUSE it persists across calls in the resident executor — the footgun was "looks persistent but isn't" in the ephemeral process. Document this flip in `skill_runtime.md` (a DoD item, `prd.md:63`).

---

## Supporting: playwriter model (from prior notes — source not vendored locally)

Playwriter source is NOT in this repo (only the research notes). Per `playwriter-exposure.md:29-34`:
- `ExecutorManager: Map<sessionId, PlaywrightExecutor>` lives in a **resident relay process**; each session has its own executor with its own `userState` + own CDP connection → "state isolated per session, pages shared."
- CLI is a thin HTTP client: `POST /cli/execute` → relay runs code in the persistent executor → returns result. (Our analogue: thin heredoc client → per-session unix socket → executor.)
- `reset()` (`playwriter-exposure.md:8`): rebuilds the CDP connection, resets browser/page/context, **clears `state`**. Used when "connection broke / page closed." → Phase B `reset()` = re-run the executor cold-start bind (`_ensure_connected` + `_bind_current_page` equivalents) + `state.clear()`. Form: an injected callable in the executor namespace (Fork 6, `prd.md:36`), since it must act on the executor's live objects — a daemon verb can't reach them.

Difference to respect: playwriter shares ONE browser across sessions; browserwright isolates per session (extension = tab group, rdp = own Chrome). So our executors do NOT share a browser — each executor's `connect_over_cdp` is scoped to its session's tabs via the facade + agent target path (`playwright_handle.py:73-97`).

## Supporting: daemon-restart recovery (Fork 4)

When the daemon restarts, the facade ws dies (the facade is a daemon-internal server, `listener.py:189-213`; its discovery file is unlinked on shutdown via `_ipc.cleanup_endpoint`, `_ipc.py:150-159`). The executor's long-lived `connect_over_cdp` transport drops.

**Recommendation: executor detects the dead transport and self-exits (cold-start next heredoc) rather than trying to live-reconnect.** Rationale:
- A new daemon writes a NEW facade port (`facade_path` content changes, `_ipc.py:95-101`), and an extension-backed session's tab group / `current_target_id` must be re-resolved via `recoverSession` (`proxy.py:1180`, `session_runtime.py:116-137`) — which is exactly the cold-start bind logic. Re-running cold-start is simpler and already correct than patching a live connection.
- This matches the existing rdp philosophy: when the upstream dies, `_on_upstream_closed` DROPS the context (`listener.py:946-953`) rather than reconnecting; "a later ensureSession recreates a fresh context."
- So: executor's facade transport closes → executor process exits (clean) → daemon reaps it → next heredoc's `ensureExecutor` spawns a fresh executor that cold-start-binds to the session's current tab via the ledger fast-path (`session_runtime.ensure_session_target`). `state` is lost on this path — acceptable and honest (same as `reset()`); document it. PRD acceptance criterion already expects this: "daemon 重启后下个 heredoc 冷启新 executor 并重绑到 session 原 tab" (`prd.md:56`).

## Supporting: concurrency (Fork 3) — serial queue is feasible

Sync Playwright + a single shared `page` is not concurrency-safe (PRD `prd.md:33`). The executor holds ONE socket-server; two concurrent heredocs for the same session → two client connections to the executor's socket. **Recommendation: serial queue inside the executor** — a single worker thread (or just a `threading.Lock` around the run-code section) draining requests one at a time. This is trivially feasible because the executor is a dedicated single-purpose process; the socket accept loop can enqueue and a single executor thread runs them FIFO. Queue (not reject-busy) is the better default for agent ergonomics; bound the queue + per-call timeout (Fork 2 output protocol) so a wedged call can't pile up unboundedly. The sync-Playwright driver must be touched from ONE thread, so the run-thread must be the same thread that did `connect_over_cdp` (Playwright sync API is thread-affine) — i.e. one dedicated executor thread owns the browser objects and the accept loop hands it work via a queue.

---

## File/line index (for the implementer)

| Concern | File:line |
|---|---|
| Daemon spawns per-session child (rdp Chrome), lazy + idempotent | `daemon/server/listener.py:690-742` |
| Child pid/profile tracked on holder | `daemon/server/listener.py:528-530`, `739-742` |
| `subprocess.Popen(..., start_new_session=True)` detach pattern | `daemon/launch_chrome.py:118-123`, `343-347` |
| Kill child on endSession | `daemon/server/listener.py:744-761`, `924-925`; `daemon.py:194-213`; `proxy.py:1318-1356` |
| Idle reap child | `daemon/server/listener.py:959-994` |
| Shutdown kill all children | `daemon/server/listener.py:997-1004` |
| Crash-drop child context | `daemon/server/listener.py:933-953` |
| Orphan child sweep on restart | `daemon/server/listener.py:249-297` (called `:106`) |
| Per-session registry keyed by session_id | `daemon/server/daemon.py:87`, `105-157` |
| `_ipc` atomic discovery-file pattern (copy for executor socket) | `daemon/_ipc.py:84-112`, `132-147` |
| AF_UNIX 104-byte budget (short socket names!) | `daemon/_ipc.py:44-55` |
| `make_unix_socket` 0600 / Windows TCP+token | `daemon/_ipc.py:306-339` |
| Detached daemon respawn (self-spawn precedent, Fork 1b) | `mode_b_client.py:478-498` |
| mode_b transport = CDP JSON-RPC tunnel | `mode_b_client.py:1-26`, `174-203` |
| Daemon verb self-answer dispatch (add executeCode/ensureExecutor here) | `daemon/server/proxy.py:276-1057` |
| heredoc entrypoint | `cli.py:582-585` → `repl/inline.py:run()` |
| in-process exec + lazy teardown finally | `repl/inline.py:22-73` |
| lazy namespace injection (page/context/snapshot) | `repl/_namespace.py:74-106` |
| facade connect + current-tab bind (migrates into executor) | `repl/playwright_handle.py:55-227` |
| FATAL: no Playwright CDP session over extension facade | `repl/playwright_handle.py:73-97`, `166-171` |
| `_LazyHandleProxy` first-access trigger | `repl/playwright_handle.py:283-319` |
| ledger fast-path tab recovery (executor cold-start uses this) | `session_runtime.py:45-137` |
| facade is daemon-internal; discovery file dies on restart | `daemon/server/listener.py:189-213`; `_ipc.py:150-159` |

## Caveats / Not found

- Playwriter source is NOT vendored in this repo; all playwriter claims derive from `.../05-24-tab-handle-model-for-code-agents/research/playwriter-exposure.md`. The HTTP `/cli/execute` shape and `reset()` semantics are quoted from those notes, not re-verified against upstream source.
- Windows transport for the executor socket is unsolved here — the mode_b path uses TCP+token on Windows (`_ipc.py:327-339`); the executor will need the same, which is extra surface beyond the POSIX unix-socket happy path. Flagged, not designed.
- The static "does this heredoc touch page/context/state?" pre-check (Fork 7, 7a) is proposed but not prototyped; `co_names` inspection after `compile()` is the suggested mechanism but edge cases (the code does `g = globals(); g['page']`) would evade it — acceptable since the fallback (shipping to executor) is always correct, just less lightweight.
