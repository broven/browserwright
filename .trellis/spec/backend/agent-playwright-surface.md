# Agent Surface: Playwright in heredocs (phase C)

> The code-agent surface (`browserwright <<'PY' … PY`). After phase C the agent
> writes **sync Playwright** against an injected `page`/`context` + `snapshot()`;
> the legacy CDP browser-driving primitives are gone. Captured from task
> `05-25-phase-c-…`. Pairs with [Playwright CDP Facade](./playwright-cdp-facade.md)
> (the daemon transport this connects through).

## 1. Scope / Trigger

Cross-layer contract for the skill/repl layer (`repl/_namespace.py`,
`repl/playwright_handle.py`, `repl/snapshot.py`, `repl/inline.py`,
`task_runner.py`, `api.py` EXPORTS). Touch the heredoc namespace, the page
binding, or EXPORTS and these contracts apply.

## 2. Signatures (the agent surface)

- Injected per-heredoc (NOT in EXPORTS): `page` (Playwright sync `Page`),
  `context` (`BrowserContext`), `snapshot(*, interactive_only=True, max_chars=6000) -> str`.
- EXPORTS (kept, non-browser-driving): `http_get`, memory (`remember`,
  `remember_global`, `remember_preference`, `memory_read`), site-skills
  (`list_site_skills`, `load_site_skill`, `run_task`, `run_tasks_concurrent`,
  `bootstrap_site`).
- **Deleted from the agent surface** (phase C): `open/new_tab/open_background/
  goto_url/reload/switch_tab/list_tabs/current_tab/current_page/ensure_real_tab/
  iframe_target/attach_active/attach_readonly/close_tab/click_at_xy/type_text/
  press_key/fill_input/scroll/dispatch_key/upload_file/js/cdp/capture_screenshot/
  describe_page/diff_snapshot/page_info/wait/wait_for_load/wait_for_element/
  wait_for_network_idle/drain_events` + the legacy coordinate `snapshot`.

## 3. Contracts

### Convention: legacy impls survive un-exported for internal glue

The deleted primitives are removed from EXPORTS + the heredoc namespace ONLY.
Their implementations stay in `primitives/*.py` and internal consumers import
them from the submodule, never the agent surface:
`repl/playwright_handle.py` → `primitives.page.current_page`; `cli.py` userscript
`--verify` → `primitives.inspect.capture_screenshot` / `primitives.page.reload`;
`primitives/site.py` → `primitives.page.list_tabs`. Removing a primitive from
EXPORTS must keep these submodule imports working.

### Convention: `page` auto-binds to the session's current tab

On first access each heredoc, `page` binds to the session's daemon-tracked
`current_target_id` (via the internal `current_page()` — reuse-or-open-in-group,
persisted to the ledger). So `page.goto()` reuses one tab and the NEXT heredoc
re-binds the same tab. `context.new_page()` is the explicit (rare) new-tab verb.
This is the tab-explosion fix — there is no agent verb that implicitly spawns tabs.

### Gotcha: map Page→targetId via the agent CDP path, NEVER a Playwright CDP session

> **Warning**: `context.new_cdp_session(page)` and `browser.new_browser_cdp_session()`
> CRASH the Playwright driver over the extension facade (`_CRSession._onMessage`
> assert) because the facade reuses one synthetic sessionId per tab/browser. To
> correlate a Playwright `Page` with our `current_target_id`, use the agent
> `sess.cdp` `Target.getTargets` URL correlation (single-page / most-recent
> tie-break) — do NOT open a Playwright CDP session.

### Gotcha: lazy connect + close disconnects, never closes tabs

> **Warning**: `page`/`context`/`snapshot` are lazy proxies — a heredoc that
> never touches them opens NO browser connection. At heredoc end, `inline.py`'s
> `finally` calls `handle.close()` which ONLY exits `sync_playwright()` (stops
> the driver = disconnects CDP). It must NEVER call `browser/context/page.close()`:
> `page.close()` kills the user's real tab; `browser.close()` over the facade
> hangs. `finally` runs on success, BrowserwrightError, SystemExit, and
> BaseException (cancellation).

### Convention: snapshot is the first-party AI aria snapshot

`snapshot()` = `page.aria_snapshot(mode="ai")` → compact aria tree with `[ref=eN]`.
The agent acts via `page.locator("aria-ref=eN")` (resolves against the page's
last snapshot). `interactive_only` keeps ref'd nodes + their ancestors (valid
tree, never drops a ref'd line); `max_chars` truncates WHOLE lines only (never
splits a `[ref=]` token). Do not screenshot just to see the page; do not invent
selectors.

### Convention: site-skills receive page/context/snapshot injected

`task_runner.run_task` injects lazy `page`/`context`/`snapshot` into the task
module globals AND `ctx` (`ctx.page` etc.), mirroring the heredoc namespace, and
closes the handle in `finally`. Site-skill `run()` drives the injected `page`.

## 4. Validation & Error Matrix

| Condition | Behavior |
|---|---|
| heredoc never touches page/context | no browser connection (lazy) |
| facade unreachable on first `page` access | `FacadeUnavailable(BrowserwrightError)` (actionable) |
| page-state error (detached / nav in flight) | raw Playwright error propagates (transparent `page` surface — intended) |
| parallel daemons (tests) | each MUST use a distinct `--facade-port`; default auto-enable is `DEFAULT_FACADE_PORT=19990`, so two daemons on it collide → `FacadeUnavailable` |

## 5. Good/Base/Bad Cases

- **Good**: heredoc#1 `page.goto(u1)`; heredoc#2 `snapshot()` then `page.locator("aria-ref=e3").click()` — same tab, ref acted.
- **Base**: `context.new_page()` for a deliberate second tab.
- **Bad**: agent calls `open(...)`/`js(...)` → NameError (removed); `browser.close()` in a heredoc (kills/ hangs); `context.new_cdp_session(page)` (crashes driver over facade).

## 6. Tests Required

- `tests/daemon/test_phase_c_foundation_unit.py`: lazy-no-connect, binding, snapshot filter/truncation.
- `tests/daemon/e2e/test_l2_heredoc_playwright_page.py`: cross-heredoc same-tab reuse + `new_page()` grows count + snapshot ref→`aria-ref=` round-trip, on **rdp AND extension**.
- e2e fixtures: distinct facade ports per daemon (conftest).

## 7. Wrong vs Correct

### Wrong
```python
# heredoc — removed primitive, and a tab-spawning footgun
open("https://example.com"); js("return document.title")
# mapping a page to a target via Playwright CDP — crashes the driver over the facade
sess = context.new_cdp_session(page)
```

### Correct
```python
# heredoc — bound page reused across heredocs; act by aria-ref
page.goto("https://example.com")
print(snapshot())                       # [ref=eN] tree
page.locator("aria-ref=e3").click()
```
