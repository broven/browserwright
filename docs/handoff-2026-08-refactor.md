# Handoff — the 2026-08 refactor branch

Written for whoever picks this up next, including a future me. It records what
changed, what was *learned*, and what is deliberately left undone. The commit
messages carry the per-change detail; this file carries the shape.

Branch: `broven/refact` · base: `1738e76` (`main`)

---

## 1. What this branch is

It started as "the code feels more complex than the requirements, and the agent
randomly hangs". It became an architecture pass plus a bug hunt, and the bug
hunt turned out to matter more.

Six of seven surveyed candidates landed, plus one real hang fixed and 34
defects found by adversarial review of the work itself:

| | |
|---|---|
| `C1a`/`C1b` | in-flight registry — `BrowserwrightDaemon.status`, `daemon ps`, timestamps at four hops, `faulthandler` on SIGUSR1 |
| `C2` | one `Upstream` protocol replacing Router's twelve mutable callback slots; two adapters (`ExtensionUpstream`, `CdpUpstream` — the latter covers **rdp and env**) |
| `C3` | `verbs.py` dispatch table; the backend fork is gone from the verb layer |
| `C6` | `daemon/cli.py` 1228 → 1024 lines; transport, supervision and LaunchAgent moved out |
| `C7` | the re-export chain (`api.py`, `primitives/__init__`, …) collapsed |
| `D` | the safety net: relay reconnect/retry/staleness coverage, wall-clock hang budgets, a verb × backend schema lock |
| — | **the hang**: an infinite `document.title` write loop in the extension's tab marker |

`C1c` (a request id threaded across all five hops) was evaluated at the end and
**not built**: `C1a`/`C1b` made hop correlation work by method + elapsed time,
and `C1c` would have touched the executor wire format for machine correlation
nobody has needed yet. Reconsider it the first time three side-by-side tables
are not enough to line up a stall.

## 2. The hang, since it is the origin story

`chrome-extension/background.js` marks attached tabs by prefixing the title with
`👀 `. The prefix carried a **trailing space**; HTML's `document.title` getter
strips trailing whitespace, so what was written never equalled what was read
back, and the `MutationObserver` re-asserting the prefix rewrote it forever.
Measured in a real Chrome: **5000 writes in 300 ms** on an empty title, **1** on
a non-empty one.

That is the whole "random" part. Only pages with an empty title at marking time
— `about:blank`, `data:` URLs, anything caught before its title is set — pegged
the renderer, after which every renderer-scoped `chrome.debugger` command on
that tab hung forever and surfaced as `relay send failed: TimeoutError()`.

Two things made it survive for months:

- **The forensics were blind.** `_wire_logging` wrote the daemon log under a
  `TMPDIR` the e2e fixture deletes, and only echoed to stderr when stderr was a
  TTY. `_artifacts/daemon.log` had been **0 bytes** for the suite's whole life,
  while `tests/daemon/e2e/README.md` promised it existed. Fixed.
- **The unit test could not have caught it.** Its stub echoed writes back
  verbatim, so the normalization mismatch that causes the loop did not exist in
  the model. The replacement models the getter and queues observer callbacks
  instead of running them inline.

## 3. Invariants — do not quietly undo these

**`Upstream` is called, never probed.** Three sites used
`getattr(upstream, "...", None)` to decide what to do. One of them wrapped an
authorization check, so an adapter missing the method skipped the check
silently — fail open. If you find yourself reaching for `hasattr` on an adapter,
that is the backend fork this protocol removed, wearing a hat.

**Target ownership is three-valued.** `True` owned, `False` denied, `None` "I am
not a session-binding authority". `None` is not permission and not denial: the
caller decides from whether the backend has another boundary — raw-CDP has one
(the browser instance), the shared extension workspace does not, so it fails
closed. A boolean was wrong in both directions, and both wrong versions shipped
briefly before this shape.

**One env session per daemon, enforced at ingress.** "Raw-CDP may proceed when
ownership is unknown" is only sound because a raw-CDP connection is a
per-session boundary — true for rdp, false for env, which routes every session
to the daemon's single shared upstream. Allocation admits one env session, and
`context_for()` re-checks it against `daemon_scope` (the daemon socket path,
which is already the isolation key of the documented N-daemons model). Records
written before scoping existed are claimed by the first daemon to serve them, as
a ledger-locked compare-and-set.

**Known facts outrank inference.** When reconciling uncertain closes by
re-querying group membership, a tab we watched close stays closed even if a
lagging query still lists it. The first version let the re-query overwrite what
was already known, which is the vanished-anchor bug in mirror image.

**Four test files have gone twelve rounds with zero edits** —
`test_hang_budget.py`, `test_relay_reconnect_paths.py`,
`test_upstream_protocol.py`, `test_verb_schema_lock.py`. They were written to
touch only public surface. If a refactor forces an edit there, that is a signal
about the refactor, not about the test. Do not add unrelated coverage to them
either — the signal only means something while nobody touches them. (I nearly
broke this myself; the new raw-CDP userscript tests live in their own file for
exactly this reason.)

## 4. What the review rounds actually taught

Fourteen rounds in two series, against the same branch:

| series | findings per round |
|---|---|
| `adversarial-review` (directed — I supplied a focus list each time) | 11 · 8 · 7 · 7 · 3 · 2 · 3 |
| `review-loop` (undirected native reviewer) | 3 · 3 · 1 · 5 · 3 · 3 · 2 |

**It never reached zero.** The last round was blocked by a Codex usage limit,
not by a clean result, and the series was not converging monotonically — each
hardening pass established a new invariant, and the next round found the paths
that did not uphold it. Anyone continuing this should expect the same and
budget for it rather than treating a low round as done.

**Eight of the findings were introduced by earlier fixes.** Remote debugging
pointable at the user's daily profile, a permanently blocking `detach`,
raw-CDP `Target.getTargets` silently dropping non-page targets, an authorization
check that failed open, then one that rejected legitimate traffic. Reviewing
only the feature would have shipped every one of them.

**Directed review illuminates exactly where you point it.** The seven adversarial
rounds ran against focus lists I wrote, and they were productive — but the very
first undirected `review-loop` round found three P1s in territory I had never
aimed at, including one that **every existing env user would hit on upgrade**
(legacy records had no `daemon_scope`, so they could neither be used nor
deleted, and they permanently occupied the single env slot). Seven rounds of
"is the new mechanism correct?" never asked "what about the old data?".

**Two defect shapes dominate.** When reviewing this codebase, look for these
first — between them they account for most of the 40-odd findings:

1. *A state the system can enter but not leave, or a success that is not one.*
   False success on close, on teardown, on userscript install; ledger rows
   nothing can prune; a `session end` that tells you to retry along the path
   that just failed.
2. *New strictness that forgot about state which already exists.* Legacy env
   records with no `daemon_scope`, an extension one version behind that omits
   `groupTitle`, an already-running daemon on the wrong backend, Chrome's own
   auto-deletion of an empty group. Four separate findings, all the same
   omission: every round asked "is the new mechanism right?" and none asked
   "what does it do to what is already out there?"

**And check the fixes, not just the feature.** Eight findings were introduced
by earlier fixes in this same series, including two of mine written while the
reviewer was rate-limited: an unconditional `_ensure_daemon_running()` that
made env teardown strictly worse, and an orphan-sweep ownership check that
swapped one unsound test for another and added a prefix-matching bug on the
way.

## 5. Open work

Filed rather than fixed, because each needs a decision rather than a patch:

- **[#29](https://github.com/broven/browserwright/issues/29)** — extension group
  ownership has no durable anchor. Titles are user-editable and non-unique;
  `groupId` is recycled across browser restarts. Layering more heuristics buys
  complexity, not soundness — tightening it once already produced a bug where a
  vanished anchor could wedge `endSession` forever. The limitation is stated in
  `CONTEXT.md`'s `binding` entry where it is enforced.
- **[#30](https://github.com/broven/browserwright/issues/30)** — Playwright never
  surfaces a `Page` for an extension session; **11 e2e tests fail on this**.
  Localized, not root-caused: CRPage runs a complete 16-command init in ~350 ms,
  every command answered OK, and `context.pages` stays empty for 31 s. It waits
  on an *event*, not a response — `isInitialEmptyPage` is the prime suspect.
  `test_agent_first_tab_of_groupless_session_is_announced` is an
  `xfail(strict=True)` reproducer for a *separate* announce race found on the
  way; fixing that alone does not turn the e2e green.
- **[#31](https://github.com/broven/browserwright/issues/31)** —
  `chrome.debugger.sendCommand` has no timeout anywhere in `background.js`. It
  has already caused two incidents and bit a fix (making marker removal
  unconditional turned an optional unbounded call into a mandatory one). Needs a
  decision on per-command budgets and on discarding late completions, not a
  blanket wrapper.
- **[#32](https://github.com/broven/browserwright/issues/32)** — `endSession` is
  a synchronous verb over an operation whose worst case is minutes, against a
  10 s client timeout. The split-state symptom is fixed; the contract is not.

Not filed, but worth knowing:

- `listener.py`'s idle-prune path still probes `getattr(daemon, "terminate_session")`
  through a three-level fallback chain that exists for `SimpleNamespace` test
  doubles. Same smell as §3's first invariant; left alone to avoid rewriting
  fakes during wrap-up.
- The e2e suite still puts **26 headful Chrome windows** on screen per run, all
  from the extension fixture (`e2e_chrome` is function-scoped). rdp is headless
  by default now; the extension path was left alone because #30's investigation
  lives in it. `--window-position` offscreen would help without changing
  headless semantics.
- The extension backend is **genuinely unstable run to run** — 12, 15 and 16
  non-passing across three runs of effectively identical code. Any "failure set
  unchanged" gate on this suite needs a control run or multiple samples; a
  single before/after pair will mislead you. The stable core is 11–12.

## 6. Where the review stopped

Not clean. The last `review-loop` round could not run — Codex hit a usage limit
that resets 2026-08-12 — so this branch ships with the review series open
rather than closed. Everything the reviewer *did* report has been fixed or
filed; nothing was dismissed as noise.

If you pick this up after the limit resets, the next step is literally:

```bash
CODEX_COMPANION=$(ls -d ~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs | sort -V | tail -1)
node "$CODEX_COMPANION" review --base main
```

Given the trend, expect it to find something. Two areas were never reviewed at
all because they are unreachable from a static diff: the real-Chrome e2e paths
(see #30) and anything requiring an actual extension upgrade in a live browser.

## 7. Working notes for the next session

- `mise run test` is the gate: **633 daemon + 1 xfailed, 44 skipped · 133 skill ·
  12 evals**, ~25 s. `mise run lint` must be clean.
- e2e is opt-in and slow, and `tests/daemon/e2e/run.sh` now **refuses** rather
  than kills when a foreign worktree holds `:29989` — two agents running e2e
  concurrently used to silently kill each other's daemons mid-suite.
- Codex's sandbox cannot bind TCP or Unix sockets, so it **cannot run this test
  suite**. If you delegate implementation to it, run the real gate yourself; it
  reported green once for a change that had a genuine regression.
- Read `CONTEXT.md` first. It is the glossary this branch added, and
  `AGENTS.md` points at it.

## 8. Addendum (2026-08-06) — the e2e job's first real CI signal

The Real-Chrome E2E job ran in CI for the first time on this branch and
failed wholesale: every extension fixture errored `Chrome for Testing
exited with code -5` (SIGTRAP) with zero evidence — the fixture DEVNULL'd
Chrome's stderr. Root cause: GitHub's ubuntu-24.04 runner ships
`kernel.apparmor_restrict_unprivileged_userns=1`, and CfT refuses to
start without user namespaces (its zip has no setuid chrome-sandbox;
the runner's preinstalled Google Chrome does — which is why the rdp half
passed). Fixed three ways, all still on the branch:

- `tests/daemon/e2e/conftest.py` — Chrome's stderr is captured to the
  profile dir and its tail rides the launch-failure RuntimeError. (The
  stderr-capturing launch in `_real_browser.py` was dead code; the live
  fixture used its own blind twin.)
- `.github/workflows/test.yml` — `sudo sysctl -w
  kernel.apparmor_restrict_unprivileged_userns=0` before the suite, plus
  a cheap headless+headful smoke probe that names startup crashes in the
  job log instead of 25 identical fixture errors.
- Two real bugs the first CI run then surfaced: the C2 refactor made the
  verb layer pass `session_ids=` to `userscript_request`, which
  `RelayServer` never accepted (any `userscript install` on an extension
  session died with TypeError — fixed by accepting and dropping it:
  the extension backend is extension-global, `background.js` reads no
  session ids); and the userscript e2e died on `userscript logs` with
  1008 "unknown browserwright session" because `helpers.run_skill`
  seeds its session by **overwriting the shared ledger file**, deleting
  the test's own session mid-test. The probes now run under the test's
  session (`extra_env={"BD_SESSION": ...}`), so run_skill writes no
  ledger at all. The helper's destructive overwrite predates this
  branch and remains a latent trap for any test that mixes sessions —
  fix it by merging (see `_seed_ext_session` in
  `test_userscripts_e2e.py` for the pattern) before writing such a test.

Suite on CI now: **12 failed, 34 passed** — the 12 are exactly the #30
family (11× PageBindTimeout + `test_recovery_via_group_id_when_runtime_stale`),
the documented baseline, not new. `continue-on-error` stays on.
