# Real-extension E2E harness — design

**Status**: design approved 2026-05-19, ready for implementation plan.
**Author**: brainstorm session with @Randy on 2026-05-19.

## Problem

The repo has solid unit/integration coverage but **no end-to-end test that
exercises the real browser extension against a real Chrome and a real daemon**.
Today everything is mocked:

- `tests/test_extension_upstream.py` — websocket-level mock extension.
- `tests/test_serve_extension.py` — mock extension feeds the relay.
- `tests/test_launch_chrome.py` — `fake_chrome` binary stub.
- `ai-e2e-tests/` — AI-driven flows but still backed by `fake_extension.py`.

That leaves a real-world gap: when an agent edits
`browser-daemon/chrome-extension/background.js`, there is no automated way to
verify the change without manually reloading the extension in the user's
daily Chrome — and the daily Chrome already runs a production copy of the
extension talking to the production daemon on `:19989`, so naive automation
would step on the user's actual workflow.

## Goal

Close the loop. Spin up an **isolated Chrome with the locally-built
extension, an isolated daemon, and drive it through the real
`browser-harness` skill CLI** — same entry point the user (and agents) use
day to day. Verify behaviour with action-level assertions.

This is **v1 of a two-stage plan**:

- **v1 (this doc)**: fixture-style `pytest` E2E. Deterministic, fast feedback
  for the agent dev loop ("I changed the extension — does it still work?").
- **v2 (future)**: Claude Agent SDK sub-agent that reads `SKILL.md` and drives
  the same harness. Tests both the code *and* the skill documentation. Reuses
  v1's fixtures and assertion helpers verbatim.

## Non-goals (v1)

- External-CDP / fingerprint browser scenarios (separate `connect()` path).
- Resilience (kill Chrome → reconnect) — needs careful state design.
- popup-storm regression — too flaky to assert against.
- GH Actions CI — needs headed Chrome + xvfb on Linux runners; macOS-local
  first, CI later.
- Performance benchmarks.

---

## §1 — Architecture & isolation boundary

The hard constraint: the user's daily Chrome **already runs the production
extension** talking to the production daemon on `:19989`. The E2E harness
must not touch either.

### Isolation matrix

Every dimension uses an independent value. No shared state.

| Dimension | Production (daily) | Test (E2E) | Mechanism |
|---|---|---|---|
| daemon extension port | 19989 | 29989 | `--extension-port 29989` (already exists) |
| daemon RDP port | default | 29990 | `--rdp-port` flag |
| daemon `BD_NAME` | `default` | `bd-e2e` | isolates pidfile / socket |
| Chrome `--user-data-dir` | user's daily profile | `bd-e2e-{run-id}` | `launch_chrome` already isolates |
| Chrome extension ID | production install ID | unsigned dev-load ID | different IDs don't clash |
| extension `RELAY_URL` | `ws://127.0.0.1:19989/` (hardcoded) | `ws://127.0.0.1:29989/` | **patch a tmp copy** before `--load-extension` |
| `browser-harness` target | default daemon | test daemon | `BD_PORT=29989 BD_NAME=bd-e2e` env |
| daemon config dir | `~/.config/browser-daemon/` | `tmp_path` | `BS_DAEMON_CONFIG_PATH` env |

All hooks already exist in the codebase. The only production change needed
is letting `launch_chrome` accept extra Chrome args (for `--load-extension=...`).

### Shape

```
test-runner (pytest)
  fixtures:
    e2e_daemon       → spawn `browser-daemon serve --extension-port 29989
                       --rdp-port 29990 --name bd-e2e`
    patched_ext_dir  → copy chrome-extension/ → tmp, replace RELAY_URL
                       19989 → 29989
    e2e_chrome       → launch_chrome(profile="bd-e2e-...",
                       extra_args=["--load-extension=" + patched_ext_dir])
    ext_ready        → poll GET http://127.0.0.1:29989/__status__ until
                       extensions_connected >= 1 (10s timeout)

  test body:
    subprocess.run(["browser-harness"],
                   env={BD_PORT: 29989, BD_NAME: bd-e2e,
                        BD_BACKEND: extension, ...},
                   input=heredoc_script_as_text)
```

### Production code changes

Just one: `launch_chrome()` gains an `extra_args: list[str] | None = None`
parameter. Used internally by the E2E fixture to inject
`--load-extension=...`. Everything else is env-var driven.

---

## §2 — Test scenarios (what v1 asserts)

Tiered. Lower tiers gate higher tiers (fail-fast).

### L0 — connection smoke

Run for both backends.

- **extension backend**: `GET http://127.0.0.1:29989/__status__` →
  daemon up. After `ext_ready`: `extensions_connected == 1`.
- **RDP backend**: `browser-daemon url --backend rdp --name bd-e2e` returns
  a non-empty ws URL that accepts a connection.

### L1 — single round-trip via skill CLI

- **extension backend**:
  ```bash
  BD_BACKEND=extension browser-harness <<'PY'
  print(page_info())
  PY
  ```
  Output is JSON with non-empty `url` and `title`.
- **RDP backend**: same script, `BD_BACKEND=rdp`.

### L2 — standard user flows (closest to SKILL.md examples)

For the extension backend (primary path):

- `open_background("data:text/html,<h1>e2e</h1>")` →
  `wait_for_load()` → `page_info()` → assert title.
- `capture_screenshot()` → file > 5KB (guards against black-screen / WS-died
  failure modes that still produce a tiny PNG).
- `js("document.querySelector('h1').textContent")` → `"e2e"`.

### L3 — cross-backend parity

A subset of L1+L2 runs against both backends. Assert that **observable
behaviour is identical**:

- `page_info()` returns the same title and URL.
- `js("document.title")` matches.
- screenshot dimensions match the viewport (both backends).

Diff at the *behaviour* level, not at internal-state level — so v2's
sub-agent can hit the same assertions even when it improvises the route.

### Out of scope for v1

- L4 resilience (Chrome restart → daemon auto-reconnect → continue).
- popup-storm regression.
- Multi-tab / iframe / download flows.
- Performance baselines.

### Time budget

~30-45s for the full v1 suite (Chrome cold-start ×2 + a handful of
navigations + screenshots). Session-scoped daemon amortises daemon startup
across all cases.

---

## §3 — Code organization, diagnostics, integration

### File layout

```
browser-daemon/
  tests/
    ...                       # existing unit/integration (all mocked)
    e2e/                      # NEW: real-Chrome E2E
      conftest.py             # fixtures: e2e_daemon, patched_ext_dir,
                              # e2e_chrome, ext_ready
      helpers.py              # run_skill(script, *, backend, env=...)
                              # subprocess wrapper
      _patch_extension.py     # copy chrome-extension/ + rewrite RELAY_URL
      test_l0_smoke.py
      test_l1_roundtrip.py
      test_l2_user_flows.py
      test_l3_parity.py
      README.md               # dev-loop usage, artifacts, isolation rationale
  pyproject.toml              # register pytest marker `real_chrome`
```

**Why `tests/e2e/` and not split repos**: existing `tests/` runs in ~3s.
E2E takes 30-45s and needs a real Chrome. A subdirectory + marker keeps the
inner loop fast (`pytest tests/` skips E2E by default) while staying close
to the code under test.

### pytest integration

- Default `pytest` skips `tests/e2e/` (filtered by config or by
  marker-required-for-collection — implementation detail of `conftest.py`).
- `pytest tests/e2e/` or `pytest -m real_chrome` runs E2E.
- `e2e_daemon` and `patched_ext_dir` are **session-scoped** (one daemon,
  one patched extension copy for the whole run).
- `e2e_chrome` is **function-scoped** (each test gets a fresh Chrome — no
  state leaks between cases). 2-3s per Chrome cold-start × N cases is the
  dominant cost.

### Failure diagnostics

On test failure, write to `tests/e2e/_artifacts/<test-name>/`:

- `chrome.log` — Chrome stderr.
- `daemon.log` — daemon stderr (run with `--log-level debug`).
- `screenshot.png` — `capture_screenshot()` of the page at failure time.
- `env.txt` — Chrome version, patched `RELAY_URL`, all relevant env vars.

On success, write nothing. Artifacts are gitignored.

### Dev-loop UX

The shape an agent (or human) actually uses:

```bash
# After editing background.js or daemon code:
cd browser-daemon && uv run pytest tests/e2e/ -x --tb=short

# On failure, read:
ls tests/e2e/_artifacts/<failing-test>/
# screenshot.png, daemon.log, chrome.log, env.txt
```

This becomes the **agent's primary signal** that an extension change
didn't regress the harness.

### CI integration (v1: not required)

Local-first. GH Actions needs xvfb on Linux runners (headed Chrome
won't render in headless mode here because we depend on Chrome's full
extension lifecycle). macOS GH runners support headed natively but are
expensive. Defer to v2 — the immediate need is "agent can verify locally".

---

## §4 — Risks, v2 path, delivery plan

### Risks (concrete + mitigations)

1. **Extension SW slow to start / silently not connecting.** Symptom:
   tests time out with no clear reason.
   *Mitigation*: `ext_ready` fixture explicitly polls
   `GET /__status__` until `extensions_connected >= 1`, with a 10s
   timeout. On timeout, fail the test with daemon log attached — never
   sleep + hope.

2. **Test Chrome processes not killed cleanly, orphans accumulate.**
   *Mitigation*: fixture teardown does `proc.terminate() → wait(5s) → kill`.
   At session end, scan for `bd-e2e-*` profile pidfiles and reap orphans.

3. **Patched extension tmpdirs leak across runs.**
   *Mitigation*: `tempfile.mkdtemp(prefix="bd-e2e-ext-")`, removed in
   session teardown (`rmtree`).

### Note on the Chrome 144+ popup storm

Not a risk here. The popup is triggered by
`--remote-debugging-port` on the user's *daily* profile. Both backends
in this harness avoid that:

- **extension backend** doesn't use `--remote-debugging-port` at all —
  the extension uses `chrome.debugger` API internally.
- **RDP backend** uses an isolated `user-data-dir`, which Chrome doesn't
  treat as a user-daily environment, so no prompt.

### v2 sub-agent path (what v1 leaves hooks for)

- `helpers.run_skill(script, env=...)` is a thin subprocess wrapper. v2
  swaps it for a Claude Agent SDK invocation with the same env. No
  fixture changes.
- `e2e_daemon` + `e2e_chrome` are **session-scoped** → v2 sub-agent runs
  many tasks back-to-back without restart.
- v1 assertions are **action-level** (e.g. "page_info returns these
  values") rather than "internal daemon state X". v2 sub-agent can
  improvise the route and the same assertions still apply.
- v2 adds a `verdict.json` protocol: sub-agent reports "task X done,
  evidence is page_info=…, screenshot path=…", harness verifies
  independently.

### v1 delivery plan (ordered)

1. `launch_chrome()` accepts `extra_args: list[str] | None`. Unit test in
   existing `tests/test_launch_chrome.py`.
2. `tests/e2e/conftest.py`, `_patch_extension.py`, `helpers.py`. No tests
   yet — fixtures importable in isolation.
3. **L0 smoke** (extension + RDP). This is the first milestone:
   real Chrome up, real extension connected, real daemon talks to it.
4. **L1 round-trip** via skill CLI.
5. **L2 user flows**. Wire up artifact dumping on failure.
6. **L3 parity**.
7. `tests/e2e/README.md` — dev-loop usage, artifact layout, isolation
   rationale.
8. `CLAUDE.md` / `browser-skill/SKILL.md` short note: "after editing the
   extension, run `pytest tests/e2e/ -x`."

### Estimate

~600-900 lines of Python (fixtures + helpers + 4 test files), plus
~20 lines in `launch_chrome.py`. Roughly 1-2 implementation sessions.

---

## Open questions deferred to implementation plan

- Exact env var contract for `run_skill()` — needs verification against
  current `browser-skill` install (does it read `BD_PORT` directly or
  through a config file?).
- Should `e2e_chrome` reuse one Chrome across all L1/L2 cases within a
  backend (faster, less isolated) or one per case (current proposal,
  slower, more isolated)? Lean: per case for v1, optimise later.
- Where exactly does daemon `--log-level debug` write? Stderr capture is
  the current assumption; verify the daemon doesn't fork its log to a
  hard-coded path.
