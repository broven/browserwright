# Session Propagation Fix + Agent Guidance Layer

**Date:** 2026-05-19
**Status:** Design — not yet implemented
**Triggered by:** Code agents getting "requires a sessionId" / "unknown sessionId" errors and not knowing how to recover; agents over-defaulting to `attach_active()` (steals user focus) instead of `open_background()`.

## Problem

Two adjacent failure modes surfaced in a recent code-agent session:

1. **Stale-session bug.** `open_background()` and `close_tab()` route through `mode_b_client.*` CLI subprocesses (`browser-skill/src/browser_skill/primitives/page.py:405,483`). The subprocess opens its own ws to the daemon; the daemon mints a sessionId bound to that subprocess's client_id and releases the binding when the subprocess exits. The skill's main heredoc then tries to use the returned sessionId over `sess.cdp` (a different ws / client_id) and the daemon answers `unknown sessionId`. This is the same bug class that `attach_active()` was migrated away from in v0.5.4 (`page.py:36-43` documents the pattern).

2. **Agent doesn't recover from the error.** When the agent sees the `unknown sessionId` / `requires a sessionId` error, there's no hint about how to recover. The daemon's error message says only what's missing, not what to call next. The skill's SKILL.md doesn't tell the agent which attach primitive to default to, so agents over-pick `attach_active()` — which steals the user's focused tab and breaks multi-agent collaboration on a single Chrome.

The user's observed transcript combined both: an `[bash-compound-allow]` hook blocked the first attach call, the agent didn't notice, and the next `type_text` surfaced as `Input.insertText requires a sessionId` — leaving the agent with no path forward.

## Goals

- **G1.** `open_background()` / `close_tab()` work end-to-end in a single heredoc on the extension backend (no `unknown sessionId` from subprocess boundary).
- **G2.** When session is missing, errors point the agent at the right next call (`open_background`, `attach_active`, `switch_tab`) rather than mentioning low-level CDP plumbing.
- **G3.** Agents default to `open_background()` (no focus steal) instead of `attach_active()` for new automation, unless the user explicitly asks for "my current tab".

## Non-goals

- Removing the `mode_b_client.open_background()` / `close_tab()` subprocess shims. They're still used by `browser-daemon open-background` CLI and similar terminal-facing commands. We're just stopping primitives from depending on them.
- Teaching agents the internals of CDP sessionId / client_id binding. The fix is to make those internals invisible behind the primitive surface, not to document them.
- Re-architecting the backend abstraction. The fix follows the v0.5.4 `attach_active` migration verbatim.

## Design

Three layers, all shippable in one PR.

### Layer 1 — Bug fix: route `open_background` / `close_tab` through long-lived ws

**File:** `browser-skill/src/browser_skill/primitives/page.py`

Mirror the v0.5.4 `attach_active()` migration. The daemon-side handlers already exist and bind the session to the calling client correctly (`browser-daemon/src/browser_daemon/server/proxy.py:797-894`); we just stop the skill from going through a transient subprocess.

```python
# open_background — current (page.py:405)
payload = daemon.open_background(url, group=group)   # subprocess → daemon releases binding

# open_background — fixed
result = sess.cdp.send(
    "BrowserDaemon.openBackgroundTab",
    url=url, groupName=group,
)
```

Same shape for `close_tab()` (page.py:483) → `sess.cdp.send("BrowserDaemon.closeTab", sessionId=..., targetId=...)`.

The existing post-call cache write (`cdp._sessions[target_id] = session_id`, page.py:429) stays as defensive idempotency. With the ws-routed path the sid is already valid before the cache write, so this becomes a no-op in the happy path but protects against future refactors.

**Subprocess shims stay:** `mode_b_client.open_background()` / `close_tab()` keep working for CLI users (`browser-daemon open-background ...`). Add a docstring note "do not call from skill primitives — use `BrowserDaemon.openBackgroundTab` directly on the long-lived ws."

**Risk:** Low. Mirrors a stable v0.5.4 migration. Daemon-side handlers are already implemented and covered by schema-lock tests.

### Layer 2 — Errors point the agent at recovery

Two daemon-side messages tightened; one skill-side check added.

**Daemon side (`browser-daemon/src/browser_daemon/server/extension_upstream.py`)** — use CDP method names, not skill API names (daemon shouldn't know skill exists).

```python
# Line ~244: requires sessionId
f"{method!r} requires a sessionId in extension backend — no tab attached. "
f"Attach one first via BrowserDaemon.attachActiveTab (focused tab) or "
f"BrowserDaemon.openBackgroundTab (background tab), then retry."

# Line ~250: unknown sessionId
f"unknown sessionId {session_id!r} — likely from a CLI subprocess (daemon "
f"released the binding when the subprocess exited). Re-attach from the "
f"same ws that will send subsequent commands."
```

**Skill side (`browser-skill/src/browser_skill/primitives/interact.py:_attached_session`)** — refuse the silent focus-steal on extension backend.

Today `_attached_session()` falls back to `current_page()` when `current_target_id` is None; on extension that calls `attach_active()` and grabs the user's focused tab. That's the exact behaviour the user warned us about ("active tab is being used by another code agent"). Change:

```python
def _attached_session() -> str:
    sess = current_session()
    if not sess.current_target_id:
        if sess.backend_name == "extension":
            raise NeedsUserConfirm(
                what="no tab attached on extension backend",
                proposal=(
                    "call `open_background(url, group='Agent')` to spawn a "
                    "fresh background tab (does not steal focus), OR "
                    "`attach_active()` if the task is explicitly 'drive the "
                    "user's current tab'. Then re-run."
                ),
            )
        from .page import current_page
        current_page()   # safe on rdp/env: isolated Chrome
    return sess.cdp.attach(sess.current_target_id)
```

The order matters: `open_background` listed first because it's the new default. `attach_active` second with the explicit "only if the task is X" qualifier.

**Same reordering** applied to two existing error sites in `page.py`:

- `list_tabs` line 135-140
- `current_tab` line 161-166

Both currently list `attach_active` first; flip them.

### Layer 3 — SKILL.md decision tree

**File:** `browser/skill/SKILL.md`

Insert a new section immediately before "Primitives surface (pre-imported in REPL)":

````
## First call: which attach should you reach for?

| Goal | Use | Why |
| --- | --- | --- |
| Reuse a tab opened in an earlier heredoc | `switch_tab("<saved targetId>")` | Deterministic, no popups, no focus steal |
| Spawn a new tab for automation **(default)** | `open_background(url, group="Agent")` | Does **not** steal user focus; isolated; safe for long flows |
| Drive the user's currently-focused tab ("read my email", "what's on my screen now") | `attach_active()` | Extension backend only. **Steals focus** — only when the user literally said "use my current tab" |
| Fresh isolated Chrome (rdp / env backend) | `new_tab(url)` | Standard `Target.createTarget`; not for extension backend |

**Rule of thumb:** Unless the user said "use my current tab" or "what I'm
looking at", default to `open_background()`. Multiple agents (or this
agent + the user) can share one Chrome that way without colliding on a
single focus.

⚠️ **Always read the return value of an attach call before chaining.** If
`attach_active()` / `open_background()` failed (a hook blocked the
command, daemon refused, etc.), the next `type_text` / `click_at_xy`
will surface as "requires sessionId" or "unknown sessionId" — that's the
symptom, not the cause. The cause is the silent failure two lines up.
````

**Soften the "boilerplate attach_active is fine" line (SKILL.md:151):**

```
# Current
For ad-hoc "just drive my current tab" usage, the boilerplate
`attach_active()` at the top of every heredoc is still fine — but for
multi-step automation, handle-passing is deterministic.

# Replace with
`attach_active()` steals the user's focus — only use when the task is
literally "drive my current tab". For everything else default to
`open_background(url)` (new tab, no focus steal) or
`switch_tab(<saved targetId>)` (heredoc continuity). See "First call:
which attach should you reach for?" above.
```

**Add a Gotchas entry:**

```
- **`sessionId` is daemon-internal plumbing — agents don't pass it.** If
  you see "unknown sessionId" or "requires a sessionId", the prior
  attach failed. Don't try to "look up" the sessionId; re-call
  `attach_active()` / `open_background()` / `switch_tab()` and verify
  the return value before the next primitive.
```

## Implementation order

1. **Layer 1 first** — bug fix is well-scoped and unblocks the rest. Lands with regression test in same commit.
2. **Layer 2 second** — error tightening. Two small daemon-side string edits + one skill-side branch. Independent of Layer 1 once tests pass.
3. **Layer 3 last** — docs change. No code risk; lands once Layers 1+2 are in.

## Tests

```python
# browser-skill/tests/test_session_propagation.py — extension backend, requires
# the daemon + extension to be live (skip with a marker otherwise).

import pytest
from browser_skill import open_background, capture_screenshot, type_text, close_tab
from browser_skill.errors import NeedsUserConfirm


@pytest.mark.extension_required
def test_open_background_session_persists_across_primitives():
    """Repro for the subprocess-binding bug. capture_screenshot used to
    raise 'unknown sessionId' right after open_background."""
    r = open_background("https://example.com", group="Agent-Test")
    try:
        capture_screenshot()
        type_text("hello")          # extension's text input is the original failure mode
    finally:
        close_tab(target_id=r["targetId"])


@pytest.mark.extension_required
def test_extension_no_attach_raises_named_options():
    """_attached_session must refuse to silently steal focus, and the
    exception must name both open_background and attach_active so the
    agent has a clear next step."""
    # No prior attach in this test session.
    with pytest.raises(NeedsUserConfirm) as exc:
        type_text("x")
    msg = str(exc.value)
    assert "open_background" in msg
    assert "attach_active" in msg
    # open_background should be listed before attach_active (default rule).
    assert msg.index("open_background") < msg.index("attach_active")
```

Plus a daemon-side smoke test that the new error messages contain the BrowserDaemon method names (so refactors don't drop them).

## Verification

After implementation:

- `pytest browser-skill/tests/test_session_propagation.py` passes.
- Manual: in an inline heredoc on extension backend, `open_background(url)` followed by `capture_screenshot()` succeeds with no `unknown sessionId`.
- Manual: in an inline heredoc, `type_text("hi")` with no prior attach raises a `NeedsUserConfirm` whose proposal lists `open_background` first and `attach_active` second.
- Eyeball SKILL.md: the new decision-tree section sits above "Primitives surface"; the "boilerplate" paragraph is the softened version; the Gotchas entry is present.

## Open questions

- Should `NeedsUserConfirm.proposal` be a structured list (so future tooling can present options) instead of free text? Out of scope for this fix — punt to a future "structured agent guidance" pass.
- The two `error_path` reorderings in `page.py` (Layer 2) also affect the `list_tabs` / `current_tab` behaviour. Worth a separate test that just asserts proposal-string ordering. Folded into the smoke test above.

## Out of scope

- Cleaning up `mode_b_client.py` further (the CLI shims remain by design).
- Reviewing whether `attach_active()` could itself be deprecated in favor of an explicit "drive focused tab" mode. The user does want this primitive — just not as the default.
- Adding telemetry on which primitive agents call first (would help validate Layer 3 actually moves the needle).
