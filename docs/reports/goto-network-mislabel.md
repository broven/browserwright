# `page.goto()` reported everything as "(network)" — what that hid

A 240-page crawl over the extension backend produced 153 identical errors:

```
PageLoadFailed: page load failed: https://x.com/... (network)
  [fix: check the URL and network; use http_get(url) to verify the site is reachable]
```

The operator read that label, concluded the network (and later the sites) was
at fault, and spent a long time eliminating two wrong hypotheses. The label was
never evidence of anything.

## The defect

`repl/_smart_goto.py::_page_load_failed()` ended in two branches that returned
**the same `reason` and the same `fix`**:

```python
if "net::" in msg or "ssl" in lower or "name_not_resolved" in lower:
    return PageLoadFailed(url, "network", fix="check the URL and network; ...")
return PageLoadFailed(url, "network", fix="check the URL and network; ...")
```

So the `if` could not change the outcome, and `reason="network"` did not mean
"a network failure" — it meant **"not a timeout"**. Worse, `msg` and
`type(exc).__name__` were computed and then dropped: the original exception
never reached the caller in any form. A caller could not tell a DNS failure
from a detached frame from a dead relay socket.

## What it was hiding, concretely

The extension caps every `chrome.debugger` command — including the
`Page.navigate` that `goto` depends on — at 9 seconds:

```js
// chrome-extension/background.js
const DEBUGGER_COMMAND_TIMEOUT_MS = 9000;  // daemon send_cdp: 10.0s
...
"chrome.debugger." + op + " timed out after " + timeoutMs + "ms (" + detail +
"); the command may still land in Chrome"
```

Playwright surfaces that as
`Protocol error (Page.navigate): chrome.debugger.sendCommand timed out after 9000ms (...)`.

That string says **"timed out"**, which does *not* contain the substring
`"timeout"` the old classifier tested for. It therefore missed the timeout
branch, fell through both identical branches, and came out as — verbatim, byte
for byte, the error the operator saw:

```
page load failed: https://x.com/i/status/1 (network)
  [fix: check the URL and network; use http_get(url) to verify the site is reachable]
```

Reproduced against the pre-fix code in this repo, not inferred.

This one mechanism also fits every property of the field report that "network"
could not explain:

| field observation | 9s `chrome.debugger` budget |
|---|---|
| fails on x.com / v.douyin.com / zhuanlan.zhihu.com, never on b23.tv | the budget is on **commit latency**; those three are heavy SPAs (service workers, cross-site frames), b23.tv is a bare redirect page that commits in milliseconds |
| a long-lived session degrades to ~100% failure on those sites | a heavily reused tab commits more slowly, so it crosses a fixed 9s wall more often |
| a brand-new session loads the same URL immediately | a new session is a new tab and a fresh renderer, back under the wall |
| single-threaded runs fail too | the budget is per command; concurrency only makes crossing it likelier, it is not required |
| `http_get(url)` and a browser both show the site is fine | nothing about this failure is on the network |

## The fix

`_page_load_failed()` now classifies by actual source, most-specific-first, and
every bucket carries a fix that points at **its own** layer:

| `reason` | meaning | matched on |
|---|---|---|
| `extension-budget` | the extension's 9s `chrome.debugger` budget expired | `chrome.debugger.sendCommand timed out`, `-32001` |
| `navigation-interrupted` | superseded/cancelled navigation, incl. `net::ERR_ABORTED` | — |
| `network` | genuinely the network: `net::`, SSL, DNS | — |
| `target-closed` | the tab/context went away mid-navigation | — |
| `frame-detached` | frame detached mid-navigation | — |
| `cdp-transport` | daemon ⇄ relay ⇄ Chrome transport fault | `relay send failed`, `protocol error`, `ws closed`, … |
| `commit` (timeout) | the site did not respond | `timeout` / `TimeoutError` |
| `unknown` | unrecognised — explicitly *not* "network" | fallback |

Two deliberate ordering decisions:

- **transport buckets sit above the generic timeout bucket.** A relay or
  extension budget expiring is not "the site did not respond"; saying so sends
  the reader to the wrong layer, which is the whole failure this report is
  about.
- **`net::ERR_ABORTED` is not `network`.** Chrome emits it for a *cancelled*
  navigation (superseded goto, a download, a client-side redirect). It is the
  single most common way a real crawl earns a bogus "check your network".

And `PageLoadFailed` now carries `detail` — the original exception type plus
the first line of its message, bounded to 300 chars and stripped of
Playwright's call log — which is surfaced in `str(err)`:

```
page load failed: https://x.com/i/status/1 (extension-budget):
  Error: Protocol error (Page.navigate): chrome.debugger.sendCommand timed out
  after 9000ms (Page.navigate tabId=42); the command may still land in Chrome
  [fix: the browserwright extension's per-command chrome.debugger budget (9s)
  expired — ...]
```

Reproduction assets: the long-lived-session experiment is committed as
`tests/daemon/e2e/test_long_lived_session_degradation.py` (marked `slow`); the
real-site probes above were temporary and are not committed.

Regression coverage: `tests/skill/test_goto_failure_classification.py`
(25 cases; verified red — 22 failed — against the pre-fix code).

---

## What the experiments actually showed

Both were run against the isolated Chrome-for-Testing e2e harness on the
extension backend — never the developer's daily Chrome.

### 1. Site selectivity is commit latency, and it is not subtle

`page.goto()` wall time, one session, isolated Chrome, no login
(temporary probe, not committed):

| URL | ms |
|---|---|
| `https://zhuanlan.zhihu.com/p/1` | 1 610 |
| `https://b23.tv/` | 2 164 |
| `https://example.com/` | 2 519 |
| `https://v.douyin.com/` | **11 275** |
| `https://x.com/elonmusk` | **18 148** |

The two sites the field report singled out as failing are the two that are an
order of magnitude slower than the site it singled out as *working*. Against a
fixed 9 000 ms per-command budget in the extension, that is the whole of the
"why only some sites" question. (`zhuanlan.zhihu.com/p/1` is a 404 stub here,
not a real article, so its number says nothing about that host.)

### 2. browserwright does not accumulate per-session rot — negative result

`tests/daemon/e2e/test_long_lived_session_degradation.py`: ONE extension
session, 20 separate `browserwright -s <sid>` batches (a resident executor, so
the same `page` and the same tab throughout), 80 navigations across four page
flavors — plain, four cross-site OOPIF iframes, a registered service worker,
and both — all served locally.

```
failed: 0/80
median ms per quarter: 2097, 2091, 1894, 1868
by flavor: plain 1887 / oopif 2092 / sw 1823 / mixed 2110  (0 failures each)
```

No failures, no upward latency trend, no flavor-specific penalty. So the
"long-lived session degrades" symptom is **not** browserwright accumulating
listeners, targets or CDP state across navigations. That hypothesis is
eliminated, which leaves the fixed 9s wall meeting genuinely slow sites.

### 3. What "the session degrades" actually is: one navigation kills the tab

Stepping ONE session through eight single-navigation batches, logging the
departure URL and `context.pages` before each (temporary probe, not committed):

| # | from | to | ms | pages before |
|---|---|---|---|---|
| 1 | about:blank | example.com | 2 314 | `[about:blank]` |
| 2 | example.com | b23.tv | 2 023 | 1 tab |
| 3 | b23.tv | zhuanlan.zhihu.com/p/1 | 1 238 | 1 tab |
| 4 | zhihu | x.com/elonmusk | 10 588 | 1 tab |
| 5 | x.com/elonmusk | x.com/nasa | 6 580 | 1 tab |
| 6 | x.com/nasa | example.com | 1 682 | 1 tab |
| 7 | example.com | v.douyin.com | 11 346 | 1 tab |
| 8 | www.douyin.com/jingxuan | example.com | **4 (failed)** | **`[]`** |

Two conclusions, both against earlier hypotheses:

- **The tab is genuinely gone, not merely unbound.** `context.pages == []`.
  Navigating to `v.douyin.com/` (which lands on `www.douyin.com/jingxuan`)
  ends with no tab in the session. From that moment every `page.goto()` in
  that session fails in 1–4 ms with `TargetClosedError` — *including*
  `example.com`. That is the entire "this session is now 100% broken" symptom,
  and it takes one bad navigation, not hundreds.
- **A brand-new session recovers instantly.** Same daemon, same Chrome,
  immediately afterwards: 5/5 URLs succeeded (example 1 635 ms, b23.tv
  1 935 ms, x.com 9 707 ms, zhihu 1 942 ms, douyin 4 139 ms). The field's
  "new session works" observation, reproduced end to end in an isolated
  harness.

### 4. The "departure page" hypothesis — refuted

It was proposed that reusing a tab is slow because each navigation *leaves* a
heavy SPA, whereas a fresh tab leaves `about:blank`. The table above tests it
directly and does not support it: leaving x.com for x.com took **6 580 ms**,
*less* than arriving at x.com from lightweight zhihu (**10 588 ms**), and
leaving x.com for example.com took **1 682 ms** — indistinguishable from
`about:blank → example.com` (2 314 ms). Latency tracks the **destination**,
not the departure page.

## Recommendations (not implemented here)

1. **A session whose tab has died should re-bind, not fail forever.** This is
   the highest-value fix on this list: one navigation can leave the session
   with zero tabs, and from then on every call fails in 4 ms with
   `TargetClosedError` until a human notices and recycles the session.
   `resolve_current_target` already knows how to open and bind a tab; the
   resident-executor path needs to re-enter it when the bound page is closed.
2. **`DEBUGGER_COMMAND_TIMEOUT_MS = 9000` is tight for `Page.navigate`.** It is
   a hard wall-clock cap, with no retry, on the one command a page load depends
   on, against sites whose full `goto` measures 10–18 s here. Either give
   navigation its own caller-derived budget or let the caller's `goto` timeout
   be the authority.
3. **`session new` need not warn about concurrency.** Two concurrent extension
   sessions were not the cause here, and the repo's multi-session e2e coverage
   passes.

## Honest limits of this investigation

- The 9 s budget's role in the *field* failures is **inferred, not measured**:
  the numbers above are full `page.goto()` wall time (commit + DOMContentLoaded
  + the settle wait), not the duration of the `Page.navigate` CDP command in
  isolation. What is *proved* is the mislabeling: that error string, produced by
  that budget, comes out of the pre-fix classifier as `(network)` byte for byte.
- Why `v.douyin.com` ends with the tab gone is not established here — a
  renderer crash, a `window.close()`, and an external-protocol handoff all fit.
  What is established is that it happens, that it is reproducible, and that
  browserwright does not recover from it.
- The field's original "two concurrent sessions caused this" and the follow-up
  "a long-lived session accumulates rot" were both retracted by the reporter and
  are both unsupported by the experiments above.
