# BUG A (loopback bind) and BUG B (extension bind timeout)

Branch: `broven/bw-bind-fix`. Both bugs root-caused separately, both fixed,
each with a regression test proven red before the fix and green after.

---

## BUG A — the daemon listened only on the Tailscale IP

### Root cause

**Not** hostname resolution or "first non-loopback interface" — there is no
such logic anywhere in the tree. The address is *configured*: the installed
LaunchAgent literally passes it.

```xml
<!-- ~/Library/LaunchAgents/com.browserwright-daemon.plist -->
<string>serve</string><string>--facade-host</string><string>100.72.20.32</string>
```

That plist is written by `daemon/launchagent.py:109-110`, i.e. by a past
`browserwright-daemon install --facade-host 100.72.20.32` — the *documented*
remote-access setup.

The defect is the consequence nobody handled: **a bind to a specific address
listens on that address only.** `listener.py:292` passes `cfg.facade_host`
straight to the one listener, so `127.0.0.1:19990` was genuinely dead. The
endpoint state file is supposed to bridge that (`daemon_url.py` ranks it above
the built-in default), but it is a best-effort channel — an unreadable or
differently-rooted `XDG_RUNTIME_DIR` silently falls through to
`DEFAULT_DAEMON_URL = http://127.0.0.1:19990`, and the client gets ECONNREFUSED
while the daemon is demonstrably up.

`daemon_url.py:126-129` already *documented* this ("a bind to a specific IP does
not listen on 127.0.0.1 at all") as an accepted cost. It should not be one:
remote access must not cost local access.

Two aggravating factors, both real:

- **Security.** `19990` on a tailnet IP is a browser control plane with no
  application-layer auth (ADR-0011 puts the boundary at the network layer),
  reachable by every host that can route there.
- **The `fix` text was a dead end.** `session.py:_unreachable` passed no `fix`
  for a non-explicit endpoint, so `DaemonUnavailable.default_fix` fired: "start
  the single global daemon: `browserwright-daemon serve`" — for a daemon that
  was already running.

The `curl http://127.0.0.1:19990/ → 503` in the original report was **Surge**
answering as an HTTP proxy (curl honours `http_proxy`), not a listener. It made
the loopback side look alive when it was not.

### Fix

1. **Loopback co-bind** (`daemon/server/facade.py`, `daemon/config.py`). New
   `needs_loopback_cobind(host)` classifies the bind host; when it names a
   *specific* non-loopback address the facade binds a **second** listener on
   `127.0.0.1` at the same port, sharing one handler. Wildcards (`0.0.0.0`,
   `::`) and loopback already cover it and get no second listener. The co-bind
   is best-effort — if loopback:port is taken it logs a loud warning and keeps
   the primary bind rather than failing startup. `stop()` closes both.
2. **A security warning** is logged whenever the endpoint binds non-loopback.
3. **Real diagnosis** (`daemon_url.local_unreachable_fix`, used by
   `session._unreachable`): names the published address and the resolved one
   when they disagree, tells you to `export BW_DAEMON_URL=…` or rebind to
   `0.0.0.0`, and only says "start the daemon" when nothing is running at all —
   where it also warns that a proxy can answer on the port with no daemon
   behind it.
4. **Doctor can now see it** (`health._endpoint_reachability_checks`). The old
   `cdp_surface` check only *echoed* the advertised address, which is why
   `doctor` was fully green throughout the outage. The new `endpoint_reachable`
   check dials the resolved endpoint *and* loopback, and fails with an
   actionable fix when the daemon answers on one but not the other.

### Test

`tests/daemon/test_bug_a_loopback_cobind.py` (18 cases) — binds a real
non-loopback IPv4 of the host and dials `127.0.0.1`. Pre-fix that raises
`URLError: Connection refused`; post-fix it answers. Also covers no-double-bind,
non-fatal co-bind failure, both-listeners-closed-on-stop, the `fix` text, and
the doctor check.

---

## BUG B — PageBindTimeout on every bind after the first

### Reproduced in-repo, so it is NOT extension drift

`chrome-extension/` has **zero** changes between `v0.15.0` and `HEAD`
(`git diff v0.15.0..HEAD -- chrome-extension/` is empty), and ADR-0009 /
ADR-0010 shipped in 0.11.0 / 0.15.0. The store extension at 0.15.0 is
functionally the same code. Confirmed empirically: the failure reproduces in
the real-Chrome e2e harness against the repo's own unpacked extension —
`tests/daemon/e2e/test_markdown_command_extension.py` failed 1-2 of 3 tests
across runs with the exact reported error.

**No user action (unpacked extension install) is required.**

### Root cause — two mismatched budgets

The tab is created by the *agent* path (`resolve_current_target` step 4 →
`openBackgroundTab`). For Playwright to see it, the daemon's extension facade
bridge must announce it, which it only does once
`_tab_visible_to_session(tab_id)` confirms live tab-group membership. The
extension emits `attached` *before* the `createTab` response that carries the
group binding, so the first check reliably answers "not mine" and the only
recovery is `_retry_visibility_announce` — a bounded loop that was
**5 × 0.1s = 0.5s** (`facade_extension.py:97-98`).

That round trip goes to the MV3 service worker. When the SW is cold or has just
reconnected to the relay, 0.5s is not enough — and when the window expires the
tab is **never announced for the life of that bridge**. `context.pages` stays
empty, and the client's own budget (a hardcoded `_PAGE_BIND_TIMEOUT_S = 2.0`)
expires into `PageBindTimeout`.

Decisive log evidence from the e2e repro (`daemon.log`): the extension
disconnected and reconnected with a *new* `install_id` between commands, and
`auto-recovered session 5 after extension reconnect` lands ~3s into the failing
session — squarely inside the bind window, far outside the 0.5s announce window.

So: **the daemon quietly stopped trying while the client was still waiting.**

### The missing cleanup on the failure path

**Scope note, stated up front:** no leaked tab was ever *observed*. The
`ext-tab-32700140 → 144 → 148 → …` climb only proves the id allocator advanced;
the user checked their Chrome and saw no stray tabs. This section is a
**code-path finding** — the failure path has no cleanup — not a reproduction of
a user-visible leak, and nothing below should be read as one.

What the code does: `bind_current_page` raised without touching the tab it had
just caused to be created. `PageBindTimeout` advertises `retryable: true` and
its `fix` says retry, so a retry sends `resolve_current_target` down the same
path — it finds no usable tab and opens another. Whether the previous tab
survives depends on whether `session end` later finds it in the session's tab
group, which is exactly the thing that fails when the announce fails. So the
window is real but its user-visible consequence is unconfirmed.

Nothing else cleans up either: the executor turns the error into a response
(`_executor/process.py`), and `PlaywrightHandle.close()` deliberately never
closes pages. `session end` *does* tear the group down correctly (the daemon
closes every member tab), but the leaked tab is only in the group if the
announce/group binding worked — which is the thing that failed.

### Fix

1. **Roll back a tab we opened and could not bind** (defensive: closes the
   window described above rather than fixing an observed leak).
   `resolve_current_target`
   now marks step 4's result `opened: True` — the only step that *creates* a
   tab — and `bind_current_page` closes it (and clears the ledger binding)
   before raising. Strictly best-effort so a cleanup error never masks
   `PageBindTimeout`, and strictly scoped: a **reused** tab (steps 1-3) is never
   closed, because that would destroy the agent's actual working tab.
2. **Make the announce window outlast the bind window.**
   `_VISIBILITY_RETRY_*` 0.5s → **6s** (30 × 0.2s), and
   `_PAGE_BIND_TIMEOUT_S` 2.0s → **10s**, overridable with
   `$BW_PAGE_BIND_TIMEOUT`. Zero cost on the happy path — both loops return on
   first success.
3. **Honest `retryable` contract.** `PageBindTimeout.default_fix` no longer
   sends the user round the `session reset` loop; it names the cold-SW cause,
   the `BW_PAGE_BIND_TIMEOUT` knob, and what to check when *every* retry fails.
4. **`session end` stops lying on extension.** It reused the cdp-attach line
   ("you attached to it, so it was left untouched") while the daemon had just
   closed every tab in the group. Extension sessions now say the tab group was
   closed and only this session's tabs were touched.

### Test

`tests/daemon/test_bug_b_bind_failure_tab_leak.py` (5 cases): the tab a failed
bind opened is closed; a reused tab is never closed; seven consecutive failures
close all seven; a cleanup failure does not mask the bind error; and
`resolve_current_target` marks only the tab it opened. Two are red before the
fix. These assert the **code path**, at the `close_session_tab` call — they do
not, and cannot, prove anything about tabs in a real browser.

E2E: `test_markdown_command_extension.py` went from 1-2 failures per run to
**3 passed** on three consecutive runs, and faster (32-72s vs 53-84s).

---

## Residual, NOT fixed

- **The extension MV3 service worker churns.** It disconnects and reconnects
  with a new `install_id` between commands. The widened budgets absorb it, they
  do not stop it. Two full-suite e2e runs still show occasional
  `extension never connected within 25s` *fixture* timeouts (2 of 69 in one
  run) — a pre-existing flake in the harness's 25s wait, unrelated to these
  changes but the same underlying churn.
- **`--facade-host <tailnet-ip>` still exposes an unauthenticated browser
  control plane** to the whole tailnet. Now warned about at startup; a real fix
  is an auth token on the endpoint, which is an ADR-0011 change and out of
  scope here.

---

## On filing issues

`reporting-issues.md` requires that a GitHub issue be **drafted, shown to the
user, and filed only after they approve** — it is public and posts under their
`gh` identity. Nothing has been filed.

Searched first, as the convention requires: no open duplicate exists.
`gh issue list --repo broven/browserwright --search "PageBindTimeout" --state all`
returns only the CLOSED #30 ("Playwright never surfaces a Page for an
extension-backend session"), which is this bug's ancestor; there is no open
issue for either bug, nor for `--facade-host` and loopback.

Recommendation: **file no issue for A or B** — both are fixed on this branch
with tests, so a PR is the better artifact. The one thing worth an issue is the
residual above: the extension MV3 service worker disconnecting and reconnecting
with a new `install_id` between commands. That is a real defect this branch only
papers over, and it is the reason the e2e harness intermittently times out
waiting for the extension.

---

## What was verified, and what was not

**Verified by direct observation:**
- BUG A, live: an isolated daemon started with `--facade-host <lan-ip>` now
  binds *two* listeners (`lsof` shows `192.168.139.3:19995` and
  `127.0.0.1:19995`) and both answer `/__ping__`. Pre-fix the loopback dial was
  refused. The user's own daemon (pid 31305) was not touched.
- BUG B, the bind failure: reproduced in the real-Chrome e2e harness against the
  repo's own extension, then fixed. `test_markdown_command_extension.py` went
  from 1-2 failures per run to 3 passed on three consecutive runs.
- Unit + skill suites: **1145 passed, 67 skipped**. Lint clean.
- Both regression suites proven red before the fix and green after.

**Verified only at the code/unit level, not against a real browser:**
- The bind-failure cleanup. The tests assert `close_session_tab` is called with
  the right target; nothing here observes a real Chrome tab. See the scope note
  above — the leak itself was never observed.

**Not verified:**
- The end-to-end user workflow (scraping X links through the user's own Chrome).
  That needs their daemon running this code, which was deliberately out of
  scope: nothing installed on their machine and nothing under `~/.browserwright`
  was modified.

**Full e2e suite: 63 passed, 6 errors.** All six are the same fixture timeout
(`extension never connected within 25s`), zero assertion failures. Checked
against a clean HEAD (changes stashed to a patch, tree reverted, re-run): the
same tests error there too, so this is a **pre-existing harness flake**, not a
regression from these changes. It is the same extension service-worker churn
described under "Residual".
