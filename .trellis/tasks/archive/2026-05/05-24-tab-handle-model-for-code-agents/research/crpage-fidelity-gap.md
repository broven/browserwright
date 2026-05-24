# Research: CRPage high-level fidelity gap (extension facade)

- **Query**: Why does Playwright's high-level `context.new_page()` / `page.goto()`
  close the freshly-created target during CRPage init on the EXTENSION backend,
  while CDP-level drive (`Page.navigate`/`Runtime.evaluate`) works?
- **Scope**: mixed (live protocol trace + our code + bundled Playwright source)
- **Date**: 2026-05-24
- **Playwright pin**: `playwright>=1.60.0`, uv.lock resolves **1.60.0** (released
  2026-05-18). pyproject.toml line `"playwright>=1.60.0"`. Driver bundle:
  `.venv/lib/python3.11/site-packages/playwright/driver/package/lib/coreBundle.js`.

## How I reproduced

Brought the extension facade up EXACTLY like
`tests/daemon/e2e/test_l1_playwright_facade_extension.py` (CfT harness, relay
port 29989, facade port 29992, isolated `XDG_RUNTIME_DIR` — daily Chrome
untouched). Instead of the L1 raw-CDP drive I called the HIGH-LEVEL path:
`p.chromium.connect_over_cdp(ws)` → `browser.contexts[0]` → `ctx.new_page()` and
captured `DEBUG=pw:protocol`. Full frame trace saved next to this file as
**`pw-protocol-trace-newpage.log`**. (The throwaway repro test was deleted after
capture.) The run hangs/fails: `new_page()` never returns a usable page because
its target is closed mid-init.

## Findings

### The captured CRPage-init frame sequence (the load-bearing part)

`connect_over_cdp` handshake (ids 1-4) succeeds: `Browser.getVersion`,
`Target.setAutoAttach{flatten,waitForDebuggerOnStart:true}` → ack `{}`,
`Browser.setDownloadBehavior` → benign `{}`, `Target.getTargetInfo` → synthetic
browser target. Then `new_page()`:

| id | direction | frame | note |
|---|---|---|---|
| 5  | SEND | `Target.createTarget {url:about:blank}` | Playwright's `doCreateNewPage` |
| —  | RECV | `Target.targetCreated {ext-tab-468981981, type:page, url:about:blank}` | facade synth, **before** the response (correct CDP ordering) |
| —  | RECV | `Target.attachedToTarget {sessionId:ext-sid-…, waitingForDebugger:**false**}` | facade synth |
| 6  | SEND | `Page.enable` (sid) | CRPage `_initialize` promises[] |
| 7  | SEND | `Page.getFrameTree` (sid) | |
| 8  | SEND | `Log.enable` (sid) | |
| 9  | SEND | `Page.setLifecycleEventsEnabled` (sid) | |
| 10 | SEND | `Runtime.enable` (sid) | |
| 11 | SEND | `Page.addScriptToEvaluateOnNewDocument {worldName:__playwright_utility_world_…}` (sid) | utility world |
| 12 | SEND | `Network.enable` (sid) | `networkManager.addSession` |
| 13 | SEND | `Target.setAutoAttach {autoAttach:true, waitForDebuggerOnStart:true, flatten:true}` (sid) | **page-session child auto-attach** |
| 14 | SEND | `Emulation.setFocusEmulationEnabled` (sid) | |
| 15 | SEND | `Emulation.setEmulatedMedia` (sid) | |
| 16 | SEND | `Runtime.runIfWaitingForDebugger` (sid) | |
| —  | RECV | `id:5 → {targetId: ext-tab-468981981}` | createTarget response |
| 6  | RECV | `id:6 {} ` | Page.enable ok |
| 7  | RECV | `id:7 {frameTree:{frame:{id:BBF8…, url:**about:blank**, loaderId:AE7F…, securityOrigin:"://", …}}}` | frame tree |
| **17** | **SEND** | **`Target.closeTarget {targetId: ext-tab-468981981}`** | **← THE CLOSE** |
| —  | RECV | lifecycle commit/DOMContentLoaded/load/networkIdle, `id:9 {}`, `Runtime.executionContextCreated {auxData:{frameId:BBF8…, isDefault:true, type:default}, id:1}`, `id:10/11/12/13/14/15/16 …`, `id:17 {success:true}` | everything else resolves AFTER the close already went out |

### WHERE it breaks (first divergence + Playwright's response)

The `Target.closeTarget` (id 17) is emitted **immediately after the
`Page.getFrameTree` response (id 7) resolves, and BEFORE ids 9-16 return**. That
is the signature of CRPage init **rejecting** (not timing out): every command
that did come back came back `{}`/success, yet Playwright still closed the tab
the same microtask-turn the frame tree landed.

Trace through the bundled 1.60.0 source:

- `CRBrowserContext.newPage` (coreBundle `async newPage(progress, …)`):
  ```js
  page = await progress.race(this.doCreateNewPage());          // createTarget id5
  const pageOrError = await page.waitForInitializedOrError();  // waits _initializedPromise
  if (pageOrError instanceof Page) { … return pageOrError; }
  throw pageOrError;                                            // init failed → throw
  } catch (error) {
    await page?.close(progress, { reason: "Failed to create page" });  // → Target.closeTarget id17
  ```
- `page.close(...)` → `Browser._closePage(crPage)` →
  `this._session.send("Target.closeTarget", { targetId: crPage._targetId })`.
  **This is exactly id 17.**
- `_initializedPromise` is settled by `CRPage … _mainFrameSession._initialize(...)
  .then(() => reportAsNew(opener, void 0), (error) => reportAsNew(opener, error))`.
  `reportAsNew(opener, error)` → `_markInitialized(error)`; a truthy `error`
  resolves `_initializedPromise` with the ERROR object → `newPage` sees
  `pageOrError` is not a `Page` → throws → catch closes the target.

So **the first (and only) divergence that matters is: `_mainFrameSession
._initialize()` REJECTS during the extension-backed init**, and Playwright's
contract on reject is "close the page I just created." It is NOT a Playwright
assert/`_onMessage` drop (the connection stays up), and NOT a hang on
`_firstNonInitialNavigationCommittedPromise`.

### Which promise in `_initialize` rejects

`_initialize` does `await Promise.all(promises)` over the ids 6-16 commands plus
`this._firstNonInitialNavigationCommittedPromise`, and the `getFrameTree.then`
callback runs synchronously when id 7 resolves. Because the close fires in that
same turn — before ids 9-16 return — the reject originates from **work triggered
inside the `Page.getFrameTree.then` callback**, not from a late command error.
Inside that callback `_initialize` runs:

```js
const isInitialEmptyPage = this._isMainFrame() && this._page.mainFrame().url() === ":";
```

Real Chrome reports a brand-new `Target.createTarget({url:"about:blank"})` page's
frame as url **`":"`** (the "initial empty document"), so `isInitialEmptyPage`
is `true` and Playwright takes the benign branch. **Our facade's
`Page.getFrameTree` (answered by the real CfT tab via `chrome.debugger`) returns
`url:"about:blank"`** (the tab has already committed about:blank by the time our
synth/attach runs), so `isInitialEmptyPage` is `false` and Playwright takes the
`_firstNonInitialNavigationCommittedFulfill()` branch — treating our page as
already-navigated.

The reject itself surfaces from the chain that this callback drives together
with the page-session pieces our facade does NOT honor (see deltas): the
`utility_world` / page-session child-attach / runIfWaitingForDebugger story.
Concretely, the three things the trace proves our facade gets WRONG vs real
Chrome, in init order:

1. **`waitingForDebugger:false` on `attachedToTarget`** (facade
   `_announce_target`, `facade_extension.py:494` synthesizes
   `"waitingForDebugger": False`). Real Chrome, when the connecting client did
   `setAutoAttach{waitForDebuggerOnStart:true}` (Playwright did, id 2), attaches
   the new page **paused with `waitingForDebugger:true`** and expects the client
   to release it via `Runtime.runIfWaitingForDebugger` (id 16). We announce it
   already-running, so the page-session contract Playwright set up is violated
   from frame one. `runIfWaitingForDebugger` (id 16) is just forwarded to
   `chrome.debugger`, which is a no-op there (the page was never paused).

2. **page-session `Target.setAutoAttach` (id 13) is silently acked, never
   honored.** `facade_extension.py:297` short-circuits session-scoped
   `Target.setAutoAttach`/`setDiscoverTargets` with `{}` and never delivers child
   `attachedToTarget` for OOPIFs/workers. For a plain about:blank page there are
   no children, so this is not the immediate trigger, but it means CRPage's child
   `FrameSession` model is unbacked.

3. **No `Page.createIsolatedWorld`/utility-world wiring.** Playwright creates the
   utility world via `Page.addScriptToEvaluateOnNewDocument{worldName:…}` (id 11)
   AND `_sendMayFail("Page.createIsolatedWorld", …)` inside the getFrameTree
   callback. `_sendMayFail` swallows failure, so this is not the reject either —
   but it is the fidelity our facade does not synthesize.

The dominant, init-aborting delta is **#1 + the `getFrameTree` url mismatch**:
because the target is announced already-running (`waitingForDebugger:false`) AND
the frame tree shows `about:blank` (not `":"`), Playwright's `_initialize`
diverges onto the "already navigated" path on a session whose page was never
debugger-paused, and the init promise chain settles as an error → close. The CDP
trace cannot show the exact rejected promise's message (pw:protocol only logs
SEND/RECV, not internal rejections), but the close-after-getFrameTree timing
isolates it to the `getFrameTree.then` init body, not a command-level error.

### Why raw CDP drive works but high-level fails

The L1 raw-CDP test (`test_connect_over_cdp_drives_extension_page`) never calls
`_initialize`. It manually sends `Page.enable` / `Runtime.enable` /
`Page.navigate` / `Runtime.evaluate` on the synth session and reads results — all
of which our `chrome.debugger`-backed forwarding answers correctly. It does not
care about `waitingForDebugger`, the `url===":"` initial-empty-page heuristic,
utility worlds, child auto-attach, or `_initializedPromise`. High-level
`new_page()` runs the entire CRPage `_initialize` state machine, which encodes
all of those assumptions.

### Transport reality (root constraint)

The extension speaks CDP only through **`chrome.debugger.sendCommand({tabId},
method, params)`** and **`chrome.debugger.onEvent(source, method, params)`**
(`chrome-extension/background.js:705` and `:921`). This API:
- has NO flat-session / `sessionId` concept (events arrive bare; the facade
  re-tags them with the synth sid in `_on_relay_event`,
  `facade_extension.py:580`);
- gives the tab a single auto-managed debugger session — there is no
  "waitingForDebugger" pause for a programmatically created tab, and no
  `Target.attachedToTarget`/`runIfWaitingForDebugger` round-trip;
- cannot emit `Target.*` browser-level events at all (the daemon synthesizes
  them).

So the facade is the ONLY place that can fake the page-session lifecycle
Playwright's CRPage init expects, and today it fakes it as
"already-attached, already-running, already-navigated."

### Files Found (our side)

| File Path | Role in this failure |
|---|---|
| `src/browserwright/daemon/server/facade_extension.py` | `ExtensionFacadeBridge`. `_announce_target` synth `attachedToTarget` with `waitingForDebugger:False` (~L494); `_handle_create_target` (L347); session-scoped `setAutoAttach` silent-ack (L297); `_handle_runtime_enable` 50ms barrier (L401, `_await_default_context` L441); `_on_relay_event` event re-tagging (L536). |
| `src/browserwright/daemon/server/extension_upstream.py` | Reused emulation: `Target.getTargets`/`attachToTarget`/`getVersion`; silent-acks `setAutoAttach`/`setDiscoverTargets` (L335); synth sid `ext-sid-{tab}-{rand}`. |
| `src/browserwright/daemon/server/relay.py` | `send_cdp` → `chrome.debugger.sendCommand` (L464); `create_background_tab` (L380); `close_tab` → `chrome.tabs.remove` (L441); `add_event_listener` fan-out (L208). |
| `chrome-extension/background.js` | `doCommand` → `chrome.debugger.sendCommand` (L703-714); event fan-out `chrome.debugger.onEvent` (L921-929) — bare, no sessionId, no `waitingForDebugger`. |
| `tests/daemon/e2e/test_l1_playwright_facade_extension.py` | Module docstring already names this PR3 edge (L26-32); the raw-CDP drive test is the working baseline. |
| `tests/daemon/e2e/conftest.py` + `run.sh` | CfT harness bring-up (relay 29989, CfT discovery, isolated runtime dir). |
| `.trellis/tasks/…/research/pw-protocol-trace-newpage.log` | The captured `DEBUG=pw:protocol` trace (42 frames). |

### Bundled Playwright 1.60.0 source anchors (read-only, in `coreBundle.js`)

- `CRBrowserContext.newPage` — try/catch that closes target "Failed to create page".
- `Browser._closePage` — `send("Target.closeTarget", {targetId})` (== id 17).
- `Page.reportAsNew` / `_markInitialized(error)` — resolves `_initializedPromise`
  with the error on init failure.
- `FrameSession._initialize(hasUIWindow)` — the `Promise.all` over Page.enable /
  getFrameTree / Runtime.enable / utility-world script / Network.enable /
  page-session `Target.setAutoAttach` / `runIfWaitingForDebugger` /
  `_firstNonInitialNavigationCommittedPromise`; the `isInitialEmptyPage =
  mainFrame().url() === ":"` heuristic lives at the top of the getFrameTree
  callback.

## Fidelity deltas to close (ranked by likelihood of triggering the close)

1. **`attachedToTarget.waitingForDebugger` must honor the connecting client's
   `setAutoAttach.waitForDebuggerOnStart`.** Playwright connects with
   `waitForDebuggerOnStart:true`, so the facade should announce new
   page-session targets with `waitingForDebugger:true`, withhold "running" until
   the client sends `Runtime.runIfWaitingForDebugger` (id 16), and treat that as
   the release. Today it is hard-coded `false` (`facade_extension.py:~494`). This
   is the single biggest CRPage-init contract violation in the trace.
2. **`Page.getFrameTree` should report the initial document as `url:":"` for a
   just-created blank target**, not `about:blank`, so Playwright's
   `isInitialEmptyPage` heuristic matches real Chrome. Because the real CfT tab
   has already committed `about:blank` by the time we attach, the facade must
   either create the tab and attach the debugger BEFORE it commits, or normalize
   the frame-tree/navigation url back to `":"` for the freshly-created,
   not-yet-navigated case. (Pairs with #1: the "already running + already
   navigated" combination is what flips init onto the wrong branch.)
3. **Page-session `Target.setAutoAttach` must be honored, not silent-acked**
   (`facade_extension.py:297`): track child sessions (OOPIF/worker) and deliver
   their `attachedToTarget`/`detachedFromTarget` with `flatten` routing.
   Low-trigger for plain about:blank, required for real pages with iframes.
4. **`Runtime.enable` execution-context fidelity / barrier.** The extension's
   `Runtime.enable` does replay `executionContextCreated` with correct
   `auxData{frameId, isDefault:true, type:default}` (seen at trace line 34), so
   auxData itself is OK — but the facade's "barrier" is a 50 ms sleep
   (`_await_default_context`, L441), not an actual wait for that event. Make it a
   real event-gated barrier so the default context is guaranteed present before
   the ack.
5. **`Page.createIsolatedWorld` / utility-world support.** Playwright issues it
   via `_sendMayFail` (failure-tolerant), so it does not abort init, but the
   utility world won't exist, breaking later high-level evaluate/locator calls.
   Synthesize it through the extension where possible.

## Caveats / Not Found

- pw:protocol logs only SEND/RECV, never the internal rejected-promise message,
  so the exact Error string that `_initialize`'s `Promise.all` rejected with is
  not directly visible. The close-immediately-after-`getFrameTree` timing plus
  the 1.60.0 source pin the failure to the `getFrameTree.then` init body and to
  the `newPage` "close on init error" contract — but the precise rejecting
  sub-promise is inferred (ranked deltas #1/#2), not printed in the trace.
- The live repro run hung after the close in this sandbox (CfT processes had to
  be killed manually); the 42-frame trace was fully captured before the hang and
  is the authoritative artifact. If a clean re-run is wanted, add a temporary
  `try/except` around `ctx.new_page()` that prints the exception and surround the
  whole test with a hard pytest timeout.
- A parallel investigation is extracting playwriter's `background.ts` solution
  (see `playwriter-exposure.md` / `playwright-over-extension-bridge.md`); this
  doc is strictly the OUR-SIDE failure trace + delta list.
