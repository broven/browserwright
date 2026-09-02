# Issue #79 — the extension service worker: two real defects, one false premise

Branch: `broven/bw-sw-churn`. Investigation of the residual left open by #78:
*"the extension MV3 service worker disconnects and reconnects with a new
`install_id` between commands."*

**Short version.** The premise is half right and half an artifact of how the
evidence was read. `install_id` is stable — it is persisted and it survives
service-worker restarts; the changing ids in the log came from the e2e harness
launching a **fresh Chrome with a throwaway profile per test** against a
**session-scoped** daemon. But the same logs do contain a real reconnect defect,
just not that one: a single service worker could open **two** relay connections
carrying the **same** `install_id`, one second apart. That is fixed here, on both
sides of the wire, with tests proven red before and green after.

A second pass then root-caused the residual this one left open — the
`extension never connected within 25s` cold start — and found an independent
defect in the same file: `hello` was awaited behind a `chrome.storage` call
that, during service-worker startup, can never settle.

**The two halves of this branch, for a reader of the PR:**

| | defect | where |
|---|---|---|
| part 1 | one service worker, two relay connections, same `install_id` | below |
| part 2 | an OPEN relay socket that never sends `hello` (cold start) | [The cold start](#the-cold-start--extension-never-connected-within-25s) |

---

## What the evidence actually shows

`chrome-extension/background.js:74-86` persists the id in
`chrome.storage.local`:

```js
async function getInstallId() {
  if (installId) return installId;
  const v = await chrome.storage.local.get(["installId"]);
  if (v.installId) { installId = v.installId; return installId; }
  installId = "bd-" + <random>;
  await chrome.storage.local.set({ installId });
  return installId;
}
```

`chrome.storage.local` survives service-worker suspension, `chrome.runtime
.reload()`, extension updates and browser restarts. A genuinely new id therefore
means a **new profile or a reinstall** — not a churning SW. `tests/daemon/e2e/
test_l2_extension_idle_recovery.py` already asserts exactly this: it kills the
socket and waits for *the same* `install_id` to come back.

The three ingredients that made a stable id look unstable:

1. `e2e_daemon` is `scope="session"`; `e2e_chrome` is **function-scoped** and
   `shutil.rmtree`s its `--user-data-dir` after every test
   (`tests/daemon/e2e/conftest.py:598-609`). One daemon log, ~20 different
   browser profiles. A different `install_id` per test is the *correct* result.
2. `listener.py`'s recovery line read `auto-recovered session %s after extension
   reconnect` — unconditionally. It fires on **every** hello, including the very
   first one from a browser that has never connected before. Nothing in the log
   distinguished "first connect" from "reconnect".
3. #78 read that combination as one extension reconnecting with a new identity.

Measured on this branch (2026-09-01, full e2e run, pre-fix), the daemon log is
one hello per test, ~10-30s apart, each a different id — i.e. one per Chrome —
with **no** within-test churn:

```
20:38:04 extension hello: install_id=bd-ad3e69bf4772ca3a
20:38:16 extension hello: install_id=bd-60bd7de9f6356268
20:38:46 extension hello: install_id=bd-e8c748b9998c9aed
```

## The real defect the same log contains

Twice in one run, two hellos arrived with the **same** `install_id`, 1.0s apart —
the `maintainLoop` tick — with no close in between:

```
20:42:06,103 connection open
20:42:06,130 extension hello: install_id=bd-bd54d63fec2ce7bb
20:42:07,104 connection open
20:42:07,105 extension hello: install_id=bd-bd54d63fec2ce7bb
```

### Root cause: the ws handlers were bound to the global, not to their socket

`connect()` assigned `ws = new WebSocket(...)` and then attached `onopen`,
`onmessage` and `onclose` that read and wrote the **module-level** `ws`,
`lastPongTs` and `lastInboundFrameTs` unconditionally. A socket can be
superseded while its events are still in flight — `forceReconnect()` swaps `ws`
immediately, and a socket that was still `CONNECTING` when it lost the race
opens anyway. So:

- a **stale `onclose`** ran `ws = null` while `ws` already pointed at the *new*
  socket. One `maintainLoop` tick later (1000 ms) the loop saw "no socket" and
  dialled a **duplicate** — the exact 1.0s spacing above.
- a **superseded `onopen`** still sent its own `hello`, so the daemon got a
  second live connection for one `install_id`.
- a **superseded `onmessage`** kept refreshing `lastInboundFrameTs`, so
  `wsLooksHealthy()` vouched for the live socket on the strength of frames
  arriving on a socket nobody read. A genuinely dead connection could be
  reported healthy and never replaced.

The daemon's `_extensions` dict is keyed by `install_id`, so the second hello
**silently evicted** the first entry while its TCP connection stayed
ESTABLISHED: a ghost the daemon kept app-pinging and could never route to.

This is a real instance of the class #79 describes ("disconnects and reconnects
between commands") — it just has nothing to do with `install_id` changing.

## Fix

1. **`chrome-extension/background.js` — socket identity.** `connect()` now keeps
   its socket in a local `sock`, and every handler is a no-op (plus a hang-up)
   once `ws !== sock`. A superseded socket cannot null the live `ws`, cannot
   send a second `hello`, and cannot refresh the live socket's liveness clocks.
2. **`daemon/server/relay.py` — one live connection per `install_id`.** A hello
   for an `install_id` that already has a live connection force-closes the older
   one instead of orphaning it. This half matters on its own: the fix in (1)
   only reaches a user when a new build ships to the Chrome Web Store, and the
   store extension is currently 0.15.0.
3. **Honest diagnostics.** The relay logs `extension hello (first connect)` vs
   `(reconnect)` — it now tracks which `install_ids` have been seen — and passes
   that through to `listener._on_extension_hello`, so the recovery line reads
   `auto-recovered session <id> after extension first connect (install_id=…)`
   instead of claiming a reconnect that never happened. This is the line that
   produced the misdiagnosis; it is now load-bearing evidence rather than noise.

## Tests

- `tests/daemon/test_issue79_ws_socket_identity.py` (3 cases) — extracts the
  real `connect` / `forceReconnect` / `wsLooksHealthy` / `safeSend` out of
  `background.js` and runs them in Node against a fake WebSocket, in the style
  of `test_extension_title_marker_unit.py`. Asserts a superseded socket cannot
  null the live `ws`, cannot send a second `hello`, and cannot refresh liveness.
- `tests/daemon/test_issue79_duplicate_install_connection.py` (5 cases) — drives
  a real `RelayServer` over real websockets: a second hello for the same
  `install_id` closes the first; two *different* installs are never superseded
  (multi-profile still works); an ordinary reconnect after a clean close is not
  treated as a duplicate; and the hello log distinguishes first connect from
  reconnect.

**6 of the 8 are red on the pre-fix tree and green after** (the other two are
guards that must stay green in both directions — verified by reverting the three
changed files, running, and restoring).

## Directions from the issue that were ruled out

- **"Keep the SW alive across commands."** Already implemented and working:
  `pingLoop` sends an app frame every 20s, the daemon pings every 5s
  (`APP_PING_INTERVAL`), and `chrome.alarms` respawns the worker every 30s if
  Chrome kills it anyway. Nothing in the measured logs shows an idle-death
  between commands on a live connection. There is nothing left to add here that
  Chrome would honour.
- **"Make `install_id` stable across SW restarts."** It already is — see above.
  Implementing this would have been a fix for a bug that does not exist.
- **"Have the daemon wait for the SW explicitly."** Partly what the widened
  budgets in #78 already do, and the remaining case (a cold Chrome that has
  never connected) is bounded by Chrome's own extension-startup latency, which
  the daemon cannot shorten by waiting differently. Not pursued; see below for
  the part that is genuinely still open.

## Still open after the first pass — since root-caused

The `extension never connected within 25s` fixture errors. At the time of the
first pass these were only known to be a *cold-start* problem with no trace on
the daemon side, and the diagnosis added for them was explicitly unverified.
They have since been reproduced, root-caused and fixed; see
**The cold start** below, which supersedes this section.

## What was measured, and what was not

- The fast gate (`mise run test`): **883 passed / 67 skipped** (daemon) plus
  **270 passed** (skill) plus evals and the pi tests. `mise run lint`: clean.
- Red-before / green-after: verified by reverting the three changed source files
  to `HEAD`, running the two new suites (**6 failed, 2 passed**), restoring, and
  running again (**8 passed**).
- The duplicate-hello defect was observed **in a real run** (the two same-id
  hellos 1.0s apart quoted above) and fixed at the level the fix addresses. It
  was **not** re-observed post-fix as a controlled before/after in a real
  browser — a duplicate dial needs a lost socket race that the suite does not
  provoke on demand, so the proof it is gone is the Node-level and relay-level
  tests, not a live run.
- **Live confirmation of both fixes, in a real browser.** Running
  `test_extension_reconnect_auto_recovers_sessions` (which makes the service
  worker call `forceReconnect()` over CDP) produces, in order:

  ```
  extension hello (first connect): install_id=bd-864b25a79bbfd0d6
  superseding older relay connection for install_id=bd-864b25a79bbfd0d6
  extension hello (reconnect): install_id=bd-864b25a79bbfd0d6
  force-closing stale extension relay connection: … superseded by a newer
    connection from the same install_id
  auto-recovered session auto-recover after extension reconnect (install_id=…)
  ```

  The same `install_id` across the reconnect (not a new one), the supersede
  actually firing on a real overlap — the extension closes the old socket but
  the close is asynchronous, so the daemon still holds it when the new hello
  lands, which is precisely the ghost this half prevents — and both log lines
  now naming which of the two events they describe.

- **The e2e error counts do not support any comparison, in either direction.**
  The #78 report's baseline was 63 passed / 6 errors. A **pre-fix** run of the
  same suite on the same worktree on 2026-09-01 gave **67 passed / 1 failed /
  1 error** (13:48); the **post-fix** run gave **61 passed / 1 failed /
  7 errors** (16:04). All seven errors are the same cold-start fixture timeout,
  the post-fix run overlapped with a concurrent `mise run test` on the same
  machine, and the count has now been measured at 6, 1 and 7 for three runs of
  the same code path. The one differing failure
  (`test_extension_reconnect_auto_recovers_sessions`, a 40s recovery budget)
  passes **4 out of 4** in isolation on the post-fix tree. Treat the e2e error
  count as a load measurement, not a regression signal, until the cold-start
  problem above is fixed.

---

# The cold start — `extension never connected within 25s`

The residual above, root-caused. It is **not** the same bug and it is **not**
"Chrome is just slow to launch": it is a second, independent defect in
`background.js`, plus a genuine browser-side cost underneath it.

## Reproduced, then measured

An isolated harness (own daemon, own ports, throwaway profiles, nothing
installed touched) launches Chrome for Testing with the unpacked extension N
times and records how long the daemon takes to see a `hello`. It reproduced on
the first attempt — **2 of 3, then 1 of 4, on an idle machine**. Load raises the
rate; it is not the cause, which is why the e2e error count moved between 1, 6
and 7 for the same code.

At the 25s mark, over CDP into the service worker itself:

```json
{"sw_target": true,
 "sw_state": {"hasWs": true, "readyState": 1, "installId": null,
              "lastPongTs": 1788269202598},
 "sw_probe2": {"timerOk": true, "timerMs": 65,
               "storageMs": 2, "store": {"ok": true, "keys": ["installId"]}}}
```

Read that carefully, because every field kills a hypothesis:

- `sw_target: true` — the worker **did** start. Not "the worker never ran".
- `readyState: 1` — its relay websocket is **OPEN**. Not "it could not dial".
- `timerMs: 65` — its event loop is healthy. Not "the worker is frozen".
- `storageMs: 2`, `keys: ["installId"]` — a **fresh** `chrome.storage.local.get`
  answers in 2ms **and the key is already there**.
- `installId: null` — and yet the module global is unset, while `lastPongTs`
  (set as the first statement of `ws.onopen`) is not.

So `ws.onopen` had run as far as `await getInstallId()` and stopped there. An
MV3 `chrome.storage.local` call issued during service-worker startup can simply
**never settle** — not reject, hang — while a later call answers immediately.

`background.js` anticipated the wrong failure. Its own comment on that `catch`
says what happens "if `getInstallId()` … rejects", and a hang is not a
rejection: nothing threw, `hello` was never sent, and the daemon — whose only
evidence of an extension is `hello` — saw no extension at all behind a socket
that was alive the whole time. Directly observed: `/__status__` reporting
`extensions: 0` for the whole window. By consequence, everything downstream of
that count is blind too — `wait_ready`, `doctor`, and the fixture's
"extension never connected within 25s".

The recovery that did eventually fire is the 45s `LEGACY_PONG_STALE_MS`
staleness sweep (no `hello` means no daemon pings, so `lastInboundFrameTs` never
advances, `wsLooksHealthy()` goes false, `maintainLoop` reconnects, and the
*second* `getInstallId()` resolves instantly). That is exactly where the
measured 26s and 64s connect times came from.

Direct confirmation, pre-fix `getInstallId` verbatim against a stub whose first
`get` never settles and with an id already persisted:

```
$ node prove_old_hangs.js
{"hung":true}
```

## Fix

`chrome-extension/background.js`:

1. **Every storage read on the connect path is bounded and retried**
   (`STORAGE_CALL_TIMEOUT_MS` 1s × `STORAGE_GET_ATTEMPTS` 5). A retry is safe
   *and* effective precisely because a later call answers in milliseconds.
2. **An unreadable store never mints a competing id.** `readStoredInstallId`
   returns a `STORAGE_UNAVAILABLE` sentinel that is distinct from "answered, no
   id yet", because only the second case makes generating an id safe.
   Overwriting an id we merely could not *read* would create the unstable
   `install_id` that #79 wrongly reported as already existing.
3. **`storage.set` is no longer awaited** — it is the same API that just hung,
   and losing the write costs a fresh id on the next cold start, which is far
   cheaper than never connecting.
4. **`hello` is sent regardless.** With no id it goes out with `installId: ""`
   (the relay accepts that and keys the connection by the socket), and
   `reannounceInstallIdWhenReadable` re-announces the *persisted* id as soon as
   storage answers — restoring reconnect matching without ever inventing a
   second identity.

## Tests

`tests/daemon/test_issue79_cold_start_install_id.py` (6 cases) — extracts the
real `getInstallId` / `readStoredInstallId` / `connect` / `sendHello` and runs
them in Node against a `chrome.storage.local` stub that can hang on the first
call, on every call, or not at all. Covers: a hanging read does not block the
id; `hello` is still sent when storage never answers; an unreadable store is
never overwritten; the ordinary first-run mint-and-persist still works;
`storage.set` does not block `hello`; and a late-readable store is re-announced
with the persisted id (`writes == 0`). **All 6 red before the fix, green after**;
the three #79 suites together are **14 green**.

## Measured before and after

Same harness, same idle machine, time from `Chrome launch` to the daemon seeing
`hello`:

| | connects (s) | over 25s |
|---|---|---|
| before | 4.2, 26.8, 64.4 / 4.4, 10.3, 10.3, 26.4 | 3 of 7 |
| after | 3.9, 5.2, 6.4, 6.5, 6.6, 7.2 | 0 of 6 |

The 45s and 64s tails are gone, which is exactly what the fix predicts: they
were the staleness sweep recovering from the hang, not the browser being slow.

## What is left underneath, and why it is not a browserwright bug

The same code, measured while the machine was also doing other work, still
produced 17.9, 26.5 and 37.7s connects. Those are **not** the storage hang —
the CDP forensics for them show `installId` already set and `hello` following
within ~0.4s of `onopen`. The time goes somewhere we do not control:

- the service-worker realm starts **3-8s** after Chrome launch
  (`performance.timeOrigin` minus launch), of which 1.3-8.2s is Chrome itself
  reaching `DevToolsActivePort`;
- its loopback websocket then takes a further **8-19s** to open
  (`lastPongTs`, set as the first statement of `onopen`, minus `timeOrigin`) —
  and the daemon's own `connection open` line lands in the same instant, so the
  wait is in Chrome's network stack, not in a frame we failed to send.

There is no browserwright defect left in that path: `connect()` is called at
service-worker top level, on the first statement after the userscript sync
kick-off, and nothing in the extension is between it and the socket.

So the harness budget was the thing that was wrong. `ext_ready`'s 25s did not
bound a defect, it truncated Chrome's own cold-start distribution — and every
time it fired it produced a message that read like a browserwright failure. It
is now `EXT_READY_TIMEOUT_S = 60.0`, overridable with
`BW_E2E_EXT_READY_TIMEOUT`, with the measurement recorded at the constant, and
the same budget is used by the second waiter in
`test_l1_playwright_facade_extension.py` instead of its own hardcoded 25s. The
budget costs nothing on the happy path — the poll returns on first success — so
it is only paid when something is genuinely broken, and the failure now names
which thing (see below).

**Production timeouts were deliberately left alone.** The user-facing wait is
`RelayServer.wait_ready(timeout=30.0)`, which already covers the measured
distribution now that the hang is gone; widening it would be the absorb-first
mistake #78 made, and there is no evidence for it.

## The diagnosis, now validated against a real failure

Last round's `_sw_diagnosis` was written blind and honestly labelled as
unverified. Real cold-start failures have now been captured — by the isolated
harness's probe, which makes the same CDP calls, not by the fixture path itself
— and they show the old text was wrong about the interesting case: it split
"worker never started" from "worker started but did not dial", and the failure
that actually happens is neither. The worker started **and** its socket is
OPEN. `_sw_diagnosis` now reports the service worker's own relay state and
names four cases, including the one that matters:

> the service worker's relay socket IS OPEN but it never sent `hello`
> ({...'readyState': 1, 'installId': None...}): `onopen` is parked on
> chrome.storage — the GH#79 cold-start hang

`_e2e_dump_artifacts_on_failure` also fires on `rep_setup.failed` now, so a
fixture timeout finally dumps the daemon log it needs.

## What was measured this round, and what was not

**Measured:**
- The hang, in a real browser, with the field-by-field CDP snapshot quoted
  above; and the pre-fix `getInstallId` proven to never return under a
  first-call hang in a standalone Node repro.
- Before/after cold-start distributions on the same harness and machine
  (table above), plus the browser-side breakdown (`performance.timeOrigin`,
  `lastPongTs`) that attributes the residual latency to Chrome.
- `mise run test` and `mise run lint`.
- 6 new tests red before the fix, green after; 14 green across the three #79
  suites.

**Not measured:**
- **No e2e run this round.** The cold-start fix has not been observed through
  the real fixture, only through the isolated harness that reproduces the same
  cold start. The e2e error count was established last round to be a load
  measurement rather than a regression signal, so it was not used as a gate —
  but that also means the new 60s budget and the new `_sw_diagnosis` text have
  not been exercised by a real fixture timeout. The diagnosis *logic* has: the
  probe in the isolated harness makes the same CDP calls and produced exactly
  the `readyState: 1, installId: None` state the new branch reports.
- The re-announce path (`reannounceInstallIdWhenReadable`) is covered by tests
  but was never seen to fire in a browser: in every measured run storage
  answered on the second attempt, well inside the first hello.

## One thing found on the way out, and it is not ours

`tests/daemon/test_facade_extension_unit.py::test_session_bound_create_target_uses_and_persists_group`
fails intermittently in whole-suite runs with
`frame not seen within 2.0s; sent=[]`, and passes 14/14 in isolation, including
under artificial CPU load. Because it sits on the relay hello path that this
branch touches, it was A/B'd at suite level, alternating trees:

| tree | target test failed |
|---|---|
| this branch | 1 of 3 |
| `HEAD`, none of this branch's source changes | 1 of 2 |

**It fails on `HEAD` too.** Pre-existing flake — a 2s `wait_for` budget that a
loaded machine can miss — not a regression here, and worth its own issue rather
than a fix smuggled into this PR. (The A/B was stopped after five runs; the two
trees had by then shown the same behaviour and more runs were only buying
precision on someone else's bug.)

## Recommendation for the issue

`#79` as written asks for a fix to a defect that is not there: `install_id` does
not change between commands, and the "possible directions" it lists are either
already implemented (SW keepalive) or fixes for the non-existent instability.
But the churn it *correctly* smelled is real, and this branch fixes two
independent defects behind it:

1. one service worker holding **two** relay connections with the same
   `install_id` (the reconnect churn);
2. `hello` awaited behind a `chrome.storage` call that never settles (the cold
   start).

Both belong to the same subsystem and ship together, so **rewriting #79's body
to describe (1) and (2) and closing it with this PR** is the right disposition —
better than the close-and-refile split suggested after the first pass, which
was written when (2) was still unexplained. Suggested replacement wording is
below.

Per `reporting-issues.md`, nothing has been posted to GitHub.

### Suggested replacement wording for the issue title and body

Title:

> A single MV3 service worker can hold two relay connections with the same `install_id`

Body (the defect as it actually is):

> `chrome-extension/background.js` keeps the live relay socket in a module-level
> `ws` and attaches `onopen` / `onmessage` / `onclose` handlers that read and
> write that global unconditionally. A socket can be superseded while its events
> are still in flight: `forceReconnect()` swaps `ws` immediately, and a socket
> that was still `CONNECTING` when it lost the race opens anyway.
>
> A stale `onclose` therefore ran `ws = null` while `ws` already pointed at the
> new socket, and one `maintainLoop` tick later (1000 ms) the loop saw "no
> socket" and dialled a duplicate. The daemon shows this as two
> `extension hello` lines **1.0s apart carrying the same `install_id`**, with no
> close in between. Because `RelayServer._extensions` is keyed by `install_id`,
> the second hello silently evicted the first entry while its TCP connection
> stayed ESTABLISHED — a ghost the daemon kept app-pinging and could never route
> to. A superseded `onmessage` also kept refreshing `lastInboundFrameTs`, so
> `wsLooksHealthy()` could vouch for a dead connection.
>
> Note what this is **not**: `install_id` does not change. It is persisted in
> `chrome.storage.local` and survives service-worker restarts, extension
> reloads and browser restarts. Ids that differ between two `extension hello`
> lines mean two different browser profiles, which is the normal state of the
> e2e suite (session-scoped daemon, function-scoped Chrome with a throwaway
> `--user-data-dir`).

And the second defect, which shares the subsystem and the PR:

> On a cold start the same service worker could hold an OPEN relay socket and
> never announce itself. `ws.onopen` awaited `getInstallId()`, and an MV3
> `chrome.storage.local` call issued during service-worker startup can never
> settle — not reject, hang. Measured 25s after launch on a fresh profile: the
> worker exists, `ws.readyState === 1`, `setTimeout` fires in 65ms and a fresh
> `storage.local.get` answers in 2ms with the key already present, while the
> module-level `installId` is still null. `background.js` guarded against that
> promise *rejecting*, and a hang is not a rejection, so `hello` was never sent
> and the daemon — whose only evidence of an extension is `hello` — saw
> nothing. The sole recovery was the 45s `LEGACY_PONG_STALE_MS` sweep, which is
> where the measured 26s and 64s connect times came from.
