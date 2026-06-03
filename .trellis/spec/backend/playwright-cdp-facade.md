# Playwright CDP Facade

> Contracts for the daemon's Playwright-facing CDP facade (`daemon/server/facade.py`,
> `facade_extension.py`) that lets `chromium.connect_over_cdp(ws://daemon)` drive the
> rdp and extension backends. Captured from task `05-24-tab-handle-model-for-code-agents`.

---

## 1. Scope / Trigger

Infra + cross-layer contract: a new browser-level CDP endpoint that multiplexes against
the same `RelayServer` the agent (`BrowserwrightDaemon.*` unix-socket) path uses. Touch
any of `facade.py` / `facade_extension.py` / `relay.py` event delivery and these contracts
are mandatory.

## 2. Signatures

- `PlaywrightFacade(relay_getter, *, port)` — standalone TCP ws+HTTP server; opt-in,
  bound only when `Config.facade_port` (env `BD_FACADE_PORT` / toml `facade_port` /
  `--facade-port`) is set and non-zero. Serves `/json/version`, `/json`, `/json/list`
  (discovery → `webSocketDebuggerUrl: ws://host:port/cdp`) and `/cdp` (CDP ws).
- rdp backend → `_handle_rdp_client`: byte-for-byte CDP passthrough to the daemon-owned
  Chrome (real browser-level CDP, no synthesis).
- extension backend → `ExtensionFacadeBridge` (`_handle_extension_client`): synthesizes
  browser-level CDP from the relay's tab state; forwards page-domain frames to
  `chrome.debugger` via the extension.
- Session-bound facade clients append `?session=<browserwright-session-id>` to the
  `/cdp` websocket URL. HTTP discovery (`/json/version?session=<id>`) must
  preserve that query in `webSocketDebuggerUrl`. The facade must route that
  client through `Daemon.context_for_required(session_id)`, exactly like the
  agent websocket path, so an unknown explicit session fails closed instead of
  silently falling into the shared extension backend.
- Extension facade sessions scope discovery and `Target.createTarget` to the
  session's durable tab group (`ledger.runtime.group_id`, creating/persisting a
  group id when the first page is opened). Only truly sessionless raw facade
  clients keep the legacy unscoped "all attached tabs" view.

## 3. Contracts

### Convention: facade is an additive parallel transport

**What**: The facade is a NEW endpoint. It must never alter the agent path (unix socket +
`BrowserwrightDaemon.*` RPC, `Router`/`DaemonState`, the relay's primary `_on_event`, and
`ExtensionUpstream`'s sessionId-stripping). All extension synthesis stays scoped to the
bridge.

**Why**: The agent path depends on `Target.setAutoAttach` being a silent ack and on
sessionId being stripped (the Router re-adds it). Emitting synthetic target events or
echoing sessionId into that path is a regression.

### Convention: backend choice is session-scoped, not facade-global

The daemon is single and global. The session ledger's immutable `backend` field
is the routing source of truth. The skill/executor Playwright handle appends the
bound Browserwright session id to the facade websocket URL; the facade then asks
`Daemon.context_for(session_id)` for the correct `UpstreamContext`.

Without this, a default shared daemon (`extension`) plus an rdp session can
start the rdp upstream correctly through `ensureExecutor`, then have the
executor's later `connect_over_cdp` enter the extension facade because the raw
facade connection was sessionless. That is cross-backend leakage.

### Gotcha: relay fan-out is safe ONLY by await-ordering

> **Warning**: `ExtensionFacadeBridge._rewrite_event_frame_id` mutates the relay event dict
> **in place**, and `relay.py` hands the *same* `msg` object to both the agent path and the
> facade. This is safe **only** because the relay awaits the agent path first
> (`await self._on_event(msg)`), and that consumer (`ExtensionUpstream._handle_extension_event`)
> **synchronously** `json.dumps`-serializes the original frame id before it yields — *then*
> `await self._fanout_listeners(msg)` runs the facade mutation.
>
> If you ever (a) reorder fan-out before the agent path, (b) make delivery concurrent
> (`asyncio.gather`), or (c) add an `await` before the agent consumer serializes, the
> synthetic `ext-tab-<id>` frame id will leak into the agent path. Either preserve the
> await-ordering or switch the facade to deep-copy before mutating.

### Convention: extension CRPage init fidelity (required for high-level Playwright)

Playwright's `CRPage.FrameSession._initialize()` rejects → `Target.closeTarget` →
`new_page()`/`goto()` fail/hang unless the bridge satisfies ALL of:

| Contract | Why |
|---|---|
| synthesized `targetInfo` carries a stable non-empty `browserContextId` (+ `type:"page"`, `waitingForDebugger:false`) | `crBrowser.ts` `assert(targetInfo.browserContextId)` throws before CRPage is built |
| `Runtime.enable` is **event-gated**: `Runtime.disable → sleep(~50ms) → Runtime.enable`, then hold the response until `Runtime.executionContextCreated{auxData.isDefault:true}` is observed for that tab (≈3s timeout) | a blind sleep races; Playwright needs the default context wired before it proceeds |
| page main-frame id == the page's targetId (synthetic `ext-tab-<tab_id>`): rewrite `Page.getFrameTree` main-frame id + forwarded `lifecycleEvent`/`frameNavigated`/`executionContextCreated` to it; rewrite inbound commands back to the real id | CRPage's `_sessionForFrame` resolves the main frame by `frame.id === targetId`; mismatch → "Frame has been detached" |
| `Page.getFrameTree` main-frame url stays the REAL value (`about:blank`) — do **NOT** rewrite it to `":"` | `isInitialEmptyPage` reads the frameTree url; `":"` flips it true, which withholds `_firstNonInitialNavigationCommittedFulfill` while init awaits that promise → `new_page()` hangs |
| after a successful `Target.closeTarget`, emit `Target.detachedFromTarget` + `Target.targetDestroyed` | Playwright hangs waiting for `targetDestroyed` otherwise |
| keep `waitingForDebugger:false` on all synthesized `attachedToTarget` | the tab is already running (chrome.debugger never pauses it); `true` with no honored resume hangs the renderer |

> Note: the playwriter-derived research initially claimed url should be `":"`; the empirical
> pw:protocol trace + Playwright 1.60.0 source proved the opposite for our flow. Empirical
> verification overrides the research note.

### Gotcha: `data:`-scheme navigation aborts over the extension backend

> **Warning**: `page.goto("data:text/html,...")` over the extension backend returns
> `net::ERR_ABORTED` — Chrome aborts `data:` navigations issued via `chrome.debugger`. This
> is a backend transport limitation, NOT a facade bug. `http(s)://` and `about:` navigations
> drive fine. Tests use `page.set_content(...)` for inline HTML.

## 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| `Config.facade_port` unset/0 | facade not bound (existing behavior unchanged) |
| facade port bind failure | logged, non-fatal; daemon continues |
| upstream `Unavailable` on a facade ws | close ws with CDP close code 1011 |
| `Runtime.enable` context event not seen in ≈3s | proceed anyway (bounded), log it |
| `Target.closeTarget` on unknown tab | ack; no destroy events |

## 5. Good/Base/Bad Cases

- **Good**: `connect_over_cdp` → `ctx.new_page()` → `page.set_content`/`goto(http…)` →
  `page.title()` / `locator().text_content()` on the extension backend.
- **Base**: CDP-level drive (`Target.createTarget` → `Page.navigate` → `Runtime.evaluate`)
  on either backend.
- **Bad**: agent-path `_on_event` receiving a synthetic `ext-tab-<id>` frame id (fan-out
  ordering broken); `new_page()` hanging (a CRPage fidelity contract regressed).

## 6. Tests Required

- `tests/daemon/test_facade_extension_unit.py`:
  - main-frame-id rewrite round-trip **and agent-path isolation** (assert agent `_on_event`
    snapshots the REAL frame id while the facade sees the synthetic one).
  - `close_target` emits `detachedFromTarget`+`targetDestroyed`, response-before-destroy
    ordering, per-tab state eviction.
  - `Runtime.enable` disable→enable + context-event gate; page-session `setAutoAttach`
    forwarded while the extension pre-arms target discovery/auto-attach and
    filters/resumes child `Target.*` events; `browserContextId` present.
- `tests/daemon/test_facade_unit.py`: session query routing picks the session's
  `UpstreamContext` and uses its rdp config even when the shared daemon backend is
  extension; a missing session preserves shared-backend behavior.
- `tests/daemon/test_phase_c_foundation_unit.py`: the skill Playwright facade URL
  includes the bound Browserwright session id and preserves existing query params.
- `tests/daemon/e2e/test_l1_playwright_facade_extension.py`: high-level `new_page()` +
  navigation over the extension harness (CDP-level + high-level cases).
- `tests/daemon/e2e/test_l1_playwright_facade.py`: rdp passthrough non-regression.

## 7. Wrong vs Correct

### Wrong
```python
# Fan-out reordered / concurrent → synthetic frame id leaks into agent path
await asyncio.gather(self._on_event(msg), self._fanout_listeners(msg))
# getFrameTree main-frame url rewritten to ":" → new_page() hangs
frame["url"] = ":"
```

### Correct
```python
# Agent path fully awaited (serializes real id) BEFORE facade mutates in place
await self._on_event(msg)
await self._fanout_listeners(msg)
# Keep the real url; rewrite only the main-frame *id* to the synthetic targetId
frame["id"] = f"ext-tab-{tab_id}"   # url stays "about:blank"
```

---

## Appendix: e2e harness gotcha (Chrome-for-Testing)

> **Warning**: A corrupted/incomplete `@puppeteer/browsers` CfT download presents as the
> extension never connecting — `/__status__` stuck at `extensions:0`, `ext_facade_ready`
> 15s timeout — because every Chrome child (GPU/network) crashes with `exit_code=5`
> ("GPU process isn't usable. Goodbye") while the parent still writes `DevToolsActivePort`.
> Diagnose with `codesign --verify <CfT.app>` (reports "code has no resources but signature
> indicates they must be present"). Fix: clean reinstall
> `npx -y @puppeteer/browsers install chrome@<ver> --path /tmp/chrome-for-testing`. This is
> NOT a CfT-148/userScripts incompatibility — 148 loads our unpacked MV3 SW fine.
