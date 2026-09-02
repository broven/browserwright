# Issue #86 — a session whose tab dies never rebinds

A session whose tab went away answered every later call in 1–4 ms with
`TargetClosedError`, for the life of that session. `example.com` failed as
reliably as the site that had killed the tab. A brand-new session against the
same daemon and the same Chrome ran the identical URL list 5/5.

Closing the tab was never the bug. Not being able to open another one was.

## Reproduction

Deterministic, and deliberately independent of any real site:
`tests/daemon/e2e/test_issue86_dead_tab_rebind.py` navigates a healthy
extension session, then removes the session's tab with `chrome.tabs.remove`
driven through **Chrome's own CDP** against the extension service worker. No
browserwright code path is involved, so nothing clears the binding and nothing
fires the target-changed hook — the field condition exactly, minus the
dependence on whichever site happened to trigger it that day.

Verified red against the pre-fix tree: the next `page.goto()` fails with
`PageLoadFailed … (target-closed)`, and so does the one after it.

## Three defects, not one

The issue named the first. The other two only appeared under the real-Chrome
repro, and either one alone would have kept the session broken.

### 1. The failure never reached the code that heals it

`_execute` already had a target-closed self-heal, guarded by
`_is_target_closed_family` in its raw-exception branch. Navigation never got
there: `repl/_smart_goto.py` **translates** the underlying `TargetClosedError`
into `PageLoadFailed(reason="target-closed")` — a `BrowserwrightError`, which
`_execute` catches in an *earlier* branch. So the single most common way a
session meets a dead tab was also the one way that could not self-heal.

Fixed by classifying on the translated error's own `reason` bucket, and by
routing both branches through one recovery response.

### 2. `page.is_closed()` was still False when we asked

Playwright's sync API dispatches channel events only while a sync call is in
flight. Between commands the resident executor is parked in `queue.get`, so a
tab that died while the session was idle had not been reported to the page
object yet — the pre-call probe saw a healthy page and the caller ate one
`TargetClosedError` before anything could react.

Fixed with a ~1 ms event drain before the probe (`drain_page_events`). The
queued events are already in the driver; the wait only yields to the
dispatcher. With it, the very next call succeeds rather than being spent.

### 3. Recovery handed back the tab that had just died

With the probe working, the rebind still failed — `PageBindTimeout` after the
full 10 s, against `ext-tab-<id>`: **the id that had just been removed**.

`resolve_current_target` step 2 (`ensure_session_target`'s ledger fast path)
treats `cdp.attach(tid)` as the liveness test — its comment says "a
stale/closed tab raises → fall through". Over the extension backend it does
**not** raise, so the fast path re-proposed the dead target and the bind spent
its whole budget waiting for a Playwright page that could never appear.

Fixed by dropping the dead binding — in memory and in the ledger — before
re-resolving. That is not a guess: the page being dead is the precondition of
the rebind. Everything below step 2 then behaves: another live tab of the
session if there is one, else a fresh tab opened in the session's own group.

> This one is worth knowing beyond issue #86: **`cdp.attach()` is not a
> liveness check on the extension backend.** Any other code reasoning "attach
> succeeded, therefore the tab is alive" is making the same mistake.

## The shape of the recovery

- **Through `resolve_current_target`, never `context.new_page()`.** The
  docstring at `playwright_handle.py` says why: an un-grouped tab is invisible
  to the agent path → ledger drift → tab explosion. The e2e asserts the
  session still owns exactly one grouped tab after recovering.
- **One rebind per call.** A second dead tab inside a single call is not a tab
  worth rebinding; it is a browser that cannot hold one.
- **A failed rebind is a different error.** `TabRebindFailed` says "the tab is
  gone AND re-opening one failed" — a different next action from "the tab is
  gone". It escalates to the pre-existing terminal recycle, so the executor
  cold-starts once rather than looping.
- **PR #85's `target-closed` bucket is untouched.** The failure keeps the name
  that PR gave it; what changed is that it now recovers.

## What is verified, and what is not

Verified:

- the deterministic repro fails before the fix and passes after it, in
  isolated Chrome for Testing on the extension backend;
- the very next call after the tab dies succeeds — no lost call;
- the replacement tab lands in the session's own tab group;
- 12 unit tests (`tests/daemon/test_issue86_dead_tab_rebind_unit.py`), 8 of
  which were confirmed red against the pre-fix tree.

Not verified:

- **why `v.douyin.com` killed the tab.** Still open, still unestablished — a
  renderer crash, a `window.close()`, and an external-protocol handoff all
  still fit. The fix deliberately does not depend on knowing: it recovers from
  a dead tab whatever killed it.
- the recovery has not been exercised against a tab that dies from an
  extension service-worker reload or a daemon restart. Those paths keep their
  previous behaviour by construction (the rebind is attempted, and its failure
  falls back to exactly the old terminal recycle), but that fallback was not
  driven end to end here.
