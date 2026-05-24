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
