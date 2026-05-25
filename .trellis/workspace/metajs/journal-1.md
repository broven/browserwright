# Journal - metajs (Part 1)

> AI development session journal
> Started: 2026-05-24

---



## Session 1: Playwright CDP facade (phase A): real Playwright over rdp + extension backends

**Date**: 2026-05-25
**Task**: Playwright CDP facade (phase A): real Playwright over rdp + extension backends
**Branch**: `feat/playwright-cdp-facade`

### Summary

Adopted the playwriter model: expose real Playwright to code agents to root-cause tab explosion. Phase A (MVP) done + verified: daemon CDP facade lets chromium.connect_over_cdp drive both rdp (PR1) and the extension/daily-Chrome backend (PR2 event synthesis), with full CRPage fidelity so high-level new_page()/goto() work over extension (PR3: main-frame-id==targetId rewrite, close->detach+destroy events, keep real about:blank not ':'). Fan-out stays agent-path-safe via await-ordering. Contracts captured in spec/backend/playwright-cdp-facade.md. Branch feat/playwright-cdp-facade, unmerged. Next: phase C (execute(code)+page/state+snapshot locators) then phase B (persistent executor).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `5bcfc85` | (see git log) |
| `94953bc` | (see git log) |
| `253be00` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: Phase C: Playwright-only agent surface (page/context + aria-ref snapshot)

**Date**: 2026-05-25
**Task**: Phase C: Playwright-only agent surface (page/context + aria-ref snapshot)
**Branch**: `feat/phase-c-playwright-surface`

### Summary

Phase C of the playwriter-model adoption: replaced the agent surface with real sync Playwright. PR1 injects lazy page/context into heredocs auto-bound to the session's daemon-tracked current tab (reuse across heredocs; new_page() explicit) — the tab-explosion fix. PR2 snapshot()=page.aria_snapshot(mode=ai) with [ref=eN], agent acts via page.locator('aria-ref=eN'). PR3 deletes the ~32 legacy CDP primitives from EXPORTS (impls kept un-exported for internal glue), rewrites the 5 site-skills + agent docs + evals to the Playwright surface. Page->targetId mapping uses the agent CDP path (Playwright CDP sessions crash the driver over the facade); handle.close() only disconnects, never closes real tabs. Verified: non-e2e 322 + evals 12/12; rdp e2e green; extension e2e via CfT harness 8 passed (cross-heredoc reuse + snapshot-ref roundtrip). Contracts: spec/backend/agent-playwright-surface.md. Branch feat/phase-c-playwright-surface (stacked on feat/playwright-cdp-facade), unmerged. Phase B (persistent state/executor) not started.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `a89c3eb` | (see git log) |
| `338b070` | (see git log) |
| `f383385` | (see git log) |
| `eb29912` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: Phase B: persistent per-session executor

**Date**: 2026-05-25
**Task**: Phase B: persistent per-session executor
**Branch**: `feat/phase-b-persistent-executor`

### Summary

Implemented phase B (PR1 executor process skeleton + data plane, PR2 lifecycle supervision, PR3 reset()+output protocol+docs): a resident per-session executor subprocess holding live Playwright page/context + persistent state across heredoc calls. Control plane = ensureExecutor verb (lazy single-flight spawn, rdp upstream pre-launch); data plane = executor's own unix socket (length-framed JSON). Executor readiness decoupled from the control-plane RPC (publish socket on bind, defer cold-start to first execute) to avoid ws-keepalive timeouts. Daemon supervises like rdp-Chrome (idle/endSession/crash/shutdown/orphan-sweep); reset() + facade-death self-exit are the two state-loss paths. inline.py routes only page/context/state/snapshot/reset-touching heredocs to the executor via a co_names pre-check; pure-memory stays in-process. Captured contracts in .trellis/spec/backend/agent-executor-model.md. Verified: unit 424 full / 375 fast green; full e2e suite 36/36 green (rdp + extension, CfT harness) after 4 e2e fix rounds (reset driver-reuse, endSession browser_session, ensureExecutor upstream pre-launch + BD_RDP_PORT, decoupled readiness) plus fixing 2 pre-existing phase-C extension-harness gaps surfaced by the full run (userscript session seeding; autofacade extension fixtures reusing the session daemon instead of colliding on the fixed relay port 29989).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `8b59d81` | (see git log) |
| `b21b906` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
