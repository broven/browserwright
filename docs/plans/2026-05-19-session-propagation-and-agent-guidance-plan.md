# Session Propagation Fix + Agent Guidance Layer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the subprocess-bound `sessionId` bug in `open_background()` / `close_tab()`, add actionable next-step hints to the daemon's session errors, refuse silent focus-steal on the extension backend, and steer agents away from defaulting to `attach_active()` via SKILL.md.

**Architecture:** Three layers in one PR. Layer 1 routes the two affected primitives through the long-lived ws (`sess.cdp.send("BrowserDaemon.openBackgroundTab"/"closeTab")`) mirroring the v0.5.4 `attach_active()` migration; the daemon-side handlers (`browser-daemon/src/browser_daemon/server/proxy.py:797-894`) already bind per-client correctly. Layer 2 tightens daemon error strings and adds an explicit `NeedsUserConfirm` raise in `_attached_session()` on the extension backend. Layer 3 inserts a "First call: which attach should you reach for?" section into SKILL.md, softens the line 151 boilerplate, and reorders the existing error-path proposals so `open_background` is listed first.

**Tech Stack:** Python 3.11+, pytest, websockets (CDP transport). Repo root: `/Users/metajs/gitRepos/labs/browser`. Run tests via `uv run pytest` from `browser-skill/`.

**Working directory:** `/Users/metajs/gitRepos/labs/browser` (the executor should `cd` here).

**Design doc (reference, not required reading):** `docs/plans/2026-05-19-session-propagation-and-agent-guidance.md`

---

## Task 1: Migrate `open_background()` to the long-lived ws

**Files:**
- Modify: `browser-skill/src/browser_skill/primitives/page.py:395-439` (`open_background` body)
- Test: `browser-skill/tests/test_session_propagation.py` (new file)

**Why:** `open_background()` currently goes through `daemon.open_background()` which spawns a CLI subprocess. The daemon mints a `sessionId` bound to that subprocess's client_id and drops it when the subprocess exits. The main heredoc's `sess.cdp` then can't use the returned sid → `unknown sessionId`. Mirror the v0.5.4 `attach_active()` migration (page.py:36-90).

### Step 1: Write the failing test

Create `browser-skill/tests/test_session_propagation.py`:

```python
"""Layer 1 + 2 regression tests for session-propagation fixes
(docs/plans/2026-05-19-session-propagation-and-agent-guidance-plan.md).

These tests use the same stub-session pattern as
``test_primitives_offline.py`` — no live daemon required.
"""
from __future__ import annotations

import pytest


class _StubCDP:
    """Captures send() calls so we can assert the wire shape."""

    def __init__(self, response: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._response = response or {}
        self._sessions: dict[str, str] = {}
        self._events: dict[str, object] = {}

    def send(self, method: str, *, session: str | None = None, **params) -> dict:
        self.calls.append((method, {"session": session, **params}))
        return self._response

    def attach(self, target_id: str) -> str:
        return self._sessions.setdefault(target_id, "sid-cached")


def _stub_session_for_ws(monkeypatch, *, backend: str = "extension",
                         response: dict | None = None):
    """Drop a Session with a stub CDP onto the singleton so primitives
    operate against our recorder. Mirrors ``test_primitives_offline._stub_session``
    but for the long-lived-ws path."""
    from browser_skill import session as session_mod

    class _StubSession:
        def __init__(self):
            self.cdp = _StubCDP(response=response)
            self.current_target_id = None
            self._backend_name_cache = backend
            self.daemon = None  # No mode_b_client — primitives must NOT touch it

        @property
        def backend_name(self) -> str:
            return self._backend_name_cache

    sess = _StubSession()
    monkeypatch.setattr(session_mod, "_singleton", sess)
    return sess


def test_open_background_uses_long_lived_ws_not_subprocess(monkeypatch):
    """open_background() must dispatch BrowserDaemon.openBackgroundTab over
    sess.cdp.send (the long-lived ws), NOT via daemon.open_background()
    (CLI subprocess that loses the sessionId binding on exit)."""
    from browser_skill.primitives.page import open_background

    sess = _stub_session_for_ws(monkeypatch, response={
        "sessionId": "ws-sid-1",
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    })
    result = open_background("https://example.com", group="Agent-Test")

    # Wire shape: exactly one BrowserDaemon.openBackgroundTab over sess.cdp.
    assert sess.cdp.calls == [
        ("BrowserDaemon.openBackgroundTab",
         {"session": None, "url": "https://example.com",
          "groupName": "Agent-Test"}),
    ], f"unexpected wire calls: {sess.cdp.calls!r}"

    # The sid IS pre-registered in the local session map so a follow-up
    # cdp.attach(target_id) returns the same sid without re-attaching.
    assert sess.cdp._sessions["ext-tab-42"] == "ws-sid-1"

    # Return shape matches the documented contract.
    assert result == {
        "targetId": "ext-tab-42",
        "tabId": 42,
        "url": "https://example.com",
        "title": "Example",
        "groupId": 7,
    }
    assert sess.current_target_id == "ext-tab-42"
```

### Step 2: Run the test, confirm it fails

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_session_propagation.py::test_open_background_uses_long_lived_ws_not_subprocess -v
```

Expected output: FAIL — current code calls `daemon.open_background(...)` which is `None` in the stub session and raises `AttributeError` / `CDPError` ("requires the Mode B daemon client"), or attempts to access methods on the stub's `daemon=None`.

### Step 3: Replace the subprocess call with a `sess.cdp.send`

Edit `browser-skill/src/browser_skill/primitives/page.py`. Replace lines **395-415** (the `sess = current_session()` block through the `if not payload` raise) with:

```python
    sess = current_session()
    try:
        payload = sess.cdp.send(
            "BrowserDaemon.openBackgroundTab",
            url=url, groupName=group,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message=(
                f"open_background failed: {e.cdp_message}. "
                "Requires the extension backend with a running daemon."
            ),
        ) from e
    if not payload:
        raise CDPError(
            method="BrowserDaemon.openBackgroundTab",
            params={"url": url, "groupName": group},
            cdp_message="daemon returned an empty payload",
        )
```

Leave lines 416-439 (target_id/session_id unpacking, `_sessions[target_id] = session_id` pre-cache, return dict) unchanged. The pre-cache write is now defensive idempotency — the sid is already valid on `sess.cdp` because we never left that ws.

### Step 4: Run the test again

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_session_propagation.py::test_open_background_uses_long_lived_ws_not_subprocess -v
```

Expected output: PASS.

### Step 5: Make sure nothing else broke

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_primitives_offline.py tests/test_mode_b_client.py -v
```

Expected output: PASS (or unchanged baseline if any test was already flaky — note that and proceed).

### Step 6: Commit

```bash
cd /Users/metajs/gitRepos/labs/browser
git add browser-skill/src/browser_skill/primitives/page.py \
        browser-skill/tests/test_session_propagation.py
git commit -m "$(cat <<'EOF'
fix(browser-skill): route open_background through long-lived ws

The previous path went through daemon.open_background() — a CLI
subprocess that loses the daemon-minted sessionId binding the moment
the subprocess exits, surfacing as "unknown sessionId" on the next
primitive in the main heredoc.

Mirrors the v0.5.4 attach_active() migration. Daemon-side handler
BrowserDaemon.openBackgroundTab is already implemented and binds the
session to the calling client (see browser-daemon proxy.py:797-894).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Migrate `close_tab()` to the long-lived ws

**Files:**
- Modify: `browser-skill/src/browser_skill/primitives/page.py:460-497` (`close_tab` body up through the `payload =` call)
- Test: `browser-skill/tests/test_session_propagation.py` (append new test)

**Why:** Same bug class as Task 1 — `close_tab()` goes through `daemon.close_tab()` subprocess shim. The daemon's `BrowserDaemon.closeTab` handler is already in `proxy.py:800-894`.

### Step 1: Write the failing test

Append to `browser-skill/tests/test_session_propagation.py`:

```python
def test_close_tab_uses_long_lived_ws_not_subprocess(monkeypatch):
    """close_tab() must dispatch BrowserDaemon.closeTab over sess.cdp.send."""
    from browser_skill.primitives.page import close_tab

    sess = _stub_session_for_ws(monkeypatch, response={
        "ok": True, "tabId": 99,
    })
    # Seed a target_id → sid mapping like a prior open_background would have.
    sess.cdp._sessions["ext-tab-99"] = "ws-sid-99"
    sess.current_target_id = "ext-tab-99"

    result = close_tab(target_id="ext-tab-99")

    # The session_id forwarded to the daemon comes from the local cache
    # (since we have one). Both params are sent.
    assert sess.cdp.calls == [
        ("BrowserDaemon.closeTab",
         {"session": None, "sessionId": "ws-sid-99",
          "targetId": "ext-tab-99"}),
    ], f"unexpected wire calls: {sess.cdp.calls!r}"
    assert result == {"ok": True, "tabId": 99}
    # Local state is cleaned up after a successful close.
    assert "ext-tab-99" not in sess.cdp._sessions
    assert sess.current_target_id is None
```

### Step 2: Run the test, confirm it fails

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_session_propagation.py::test_close_tab_uses_long_lived_ws_not_subprocess -v
```

Expected output: FAIL.

### Step 3: Replace the subprocess call with a `sess.cdp.send`

Edit `browser-skill/src/browser_skill/primitives/page.py`. Replace lines **461-497** (from `sess = current_session()` through the `if not payload` raise block) with:

```python
    sess = current_session()
    # Resolve target_id and session_id from local state when not passed,
    # then forward to the daemon over the long-lived ws so the session
    # binding lookup runs against this client's bindings.
    resolved_target_id = target_id
    resolved_session_id = session_id
    if resolved_target_id is None and resolved_session_id is None:
        resolved_target_id = sess.current_target_id
        if not resolved_target_id:
            raise CDPError(
                method="BrowserDaemon.closeTab",
                params={"sessionId": None, "targetId": None},
                cdp_message="close_tab: no current attached tab to close",
            )
        resolved_session_id = sess.cdp._sessions.get(resolved_target_id)
    elif resolved_session_id is None and resolved_target_id is not None:
        # Caller passed target_id; fill in session_id from local cache if we have it.
        resolved_session_id = sess.cdp._sessions.get(resolved_target_id)
    try:
        payload = sess.cdp.send(
            "BrowserDaemon.closeTab",
            sessionId=resolved_session_id,
            targetId=resolved_target_id,
        )
    except CDPError as e:
        raise CDPError(
            method="BrowserDaemon.closeTab",
            params={"sessionId": resolved_session_id,
                    "targetId": resolved_target_id},
            cdp_message=(
                f"close_tab failed: {e.cdp_message}. "
                "Requires the extension backend with a running daemon."
            ),
        ) from e
    # Backfill the local session_id var for the state-cleanup block below.
    session_id = resolved_session_id
    if not payload:
        raise CDPError(
            method="BrowserDaemon.closeTab",
            params={"sessionId": session_id},
            cdp_message="daemon returned an empty close-tab payload",
        )
```

Leave lines 498-509 (the local cleanup block + return) unchanged.

### Step 4: Run the test again

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_session_propagation.py -v
```

Expected output: 2 PASS (Task 1 test + Task 2 test).

### Step 5: Make sure nothing else broke

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_primitives_offline.py tests/test_mode_b_client.py -v
```

Expected: PASS (same baseline as Task 1 Step 5).

### Step 6: Commit

```bash
cd /Users/metajs/gitRepos/labs/browser
git add browser-skill/src/browser_skill/primitives/page.py \
        browser-skill/tests/test_session_propagation.py
git commit -m "$(cat <<'EOF'
fix(browser-skill): route close_tab through long-lived ws

Same subprocess-binding bug class as the previous commit's
open_background fix. Daemon-side BrowserDaemon.closeTab handler is
already implemented (browser-daemon proxy.py:800).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Tighten daemon-side error messages

**Files:**
- Modify: `browser-daemon/src/browser_daemon/server/extension_upstream.py:245,250`
- Test: `browser-daemon/tests/` (locate or create — see Step 1)

**Why:** Current messages tell the agent what's missing but not what to call next. After this change, the daemon-side error itself surfaces the recovery path (still using CDP method names, since daemon doesn't know skill API exists).

### Step 1: Find or create a test file for these errors

```bash
cd /Users/metajs/gitRepos/labs/browser
ls browser-daemon/tests/ | grep -i "extension\|upstream\|error" | head -5
```

If a test file exists that already exercises `extension_upstream.py` error paths, append to it. Otherwise create `browser-daemon/tests/test_extension_upstream_errors.py`.

### Step 2: Write the failing test

Add this test (adapt imports based on existing patterns in `browser-daemon/tests/`):

```python
"""Daemon error messages must point clients at the recovery path."""
import pytest


def test_requires_sessionid_error_mentions_recovery_methods():
    """The 'requires a sessionId' error must name both
    BrowserDaemon.attachActiveTab AND BrowserDaemon.openBackgroundTab so
    the client knows what to call next, NOT just what is missing."""
    # Build the exact error string the upstream emits today.
    from browser_daemon.server.extension_upstream import (
        _build_requires_session_error,  # add this helper in Step 3
    )
    msg = _build_requires_session_error("Input.insertText")
    assert "Input.insertText" in msg
    assert "BrowserDaemon.attachActiveTab" in msg
    assert "BrowserDaemon.openBackgroundTab" in msg


def test_unknown_sessionid_error_mentions_subprocess_cause():
    """'unknown sessionId' must hint that the binding was likely released
    by a transient ws (CLI subprocess) so the client knows to re-attach
    from the same ws."""
    from browser_daemon.server.extension_upstream import (
        _build_unknown_session_error,  # add this helper in Step 3
    )
    msg = _build_unknown_session_error("c110-DEADBEEF")
    assert "c110-DEADBEEF" in msg
    assert "subprocess" in msg.lower() or "transient" in msg.lower()
    assert "re-attach" in msg.lower() or "reattach" in msg.lower()
```

### Step 3: Run the tests, confirm they fail

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-daemon
uv run pytest tests/test_extension_upstream_errors.py -v
```

Expected: FAIL — helpers don't exist yet.

### Step 4: Add the helpers and use them at the error sites

Edit `browser-daemon/src/browser_daemon/server/extension_upstream.py`.

Add two module-level helpers near the top of the file (after the imports, before the first class). Verify your insertion point with `grep -n "^class\|^def " browser-daemon/src/browser_daemon/server/extension_upstream.py | head -5`:

```python
def _build_requires_session_error(method: str) -> str:
    return (
        f"{method!r} requires a sessionId in extension backend — "
        "no tab attached. Attach one first via "
        "BrowserDaemon.attachActiveTab (focused tab) or "
        "BrowserDaemon.openBackgroundTab (background tab), then retry."
    )


def _build_unknown_session_error(session_id: str) -> str:
    return (
        f"unknown sessionId {session_id!r} — likely from a transient ws "
        "(e.g. CLI subprocess) which the daemon has since released. "
        "Re-attach from the same ws that will send subsequent commands."
    )
```

Replace line **245**:

```python
# Before
await self._error(req_id, -32601,
                  f"{method!r} requires a sessionId in extension backend")
# After
await self._error(req_id, -32601,
                  _build_requires_session_error(method or "<unknown>"))
```

Replace line **250**:

```python
# Before
await self._error(req_id, -32602, f"unknown sessionId {session_id!r}")
# After
await self._error(req_id, -32602, _build_unknown_session_error(session_id))
```

### Step 5: Run the tests again

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-daemon
uv run pytest tests/test_extension_upstream_errors.py -v
```

Expected: PASS.

### Step 6: Make sure nothing else broke

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-daemon
uv run pytest -v
```

Expected: PASS overall (or the prior baseline).

### Step 7: Commit

```bash
cd /Users/metajs/gitRepos/labs/browser
git add browser-daemon/src/browser_daemon/server/extension_upstream.py \
        browser-daemon/tests/test_extension_upstream_errors.py
git commit -m "$(cat <<'EOF'
feat(browser-daemon): make session errors point at the recovery path

'requires a sessionId' and 'unknown sessionId' messages now name the
specific BrowserDaemon.* methods the client should call to recover,
instead of leaving the agent stuck. Uses CDP method names (not skill
API names) — the daemon doesn't know which skill is on the other end.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Refuse silent focus-steal in `_attached_session()`

**Files:**
- Modify: `browser-skill/src/browser_skill/primitives/interact.py:20-27`
- Test: `browser-skill/tests/test_session_propagation.py` (append)

**Why:** When no tab is attached on the extension backend, the current code silently calls `current_page()` → `attach_active()` → grabs the user's focused tab. This steals focus from other agents / the user. Better: raise `NeedsUserConfirm` with both `open_background` (listed first, default) and `attach_active` (listed second).

### Step 1: Write the failing test

Append to `browser-skill/tests/test_session_propagation.py`:

```python
def test_attached_session_raises_on_extension_without_attach(monkeypatch):
    """On extension backend, _attached_session() must refuse to silently
    auto-attach the user's focused tab — raise NeedsUserConfirm with both
    open_background AND attach_active named, with open_background listed
    FIRST (the new default rule)."""
    from browser_skill.errors import NeedsUserConfirm
    from browser_skill.primitives.interact import _attached_session

    _stub_session_for_ws(monkeypatch, backend="extension")  # no target attached
    with pytest.raises(NeedsUserConfirm) as exc_info:
        _attached_session()
    proposal = exc_info.value.proposal or ""
    assert "open_background" in proposal
    assert "attach_active" in proposal
    # Default rule: open_background listed before attach_active.
    assert proposal.index("open_background") < proposal.index("attach_active")


def test_attached_session_auto_attaches_on_rdp(monkeypatch):
    """On rdp/env backends (isolated Chrome), _attached_session() may still
    auto-attach via current_page() — no user collision there."""
    from browser_skill.primitives.interact import _attached_session

    sess = _stub_session_for_ws(monkeypatch, backend="rdp")
    # Pre-seed a current_target_id so current_page() short-circuits and
    # _attached_session returns the cached sid. (We're asserting the
    # extension-only branch does NOT fire here, not full rdp behaviour.)
    sess.current_target_id = "rdp-target-1"
    sid = _attached_session()
    assert sid == "sid-cached"  # from _StubCDP.attach default
```

### Step 2: Run the tests, confirm the new one fails

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_session_propagation.py -v
```

Expected: 4 tests, the two new ones FAIL (the first because today the code silently auto-attaches via `current_page()`, the second because the existing fallthrough path may also misbehave with the stub).

### Step 3: Update `_attached_session()`

Edit `browser-skill/src/browser_skill/primitives/interact.py`. Replace lines **20-27** (the entire `_attached_session` function) with:

```python
def _attached_session() -> str:
    sess = current_session()
    if not sess.current_target_id:
        # Extension backend: do NOT silently steal the user's focused tab
        # (current_page() would call attach_active() and grab it). Raise
        # with named next steps; open_background listed first (default).
        if sess.backend_name == "extension":
            from ..errors import NeedsUserConfirm
            raise NeedsUserConfirm(
                what="no tab attached on extension backend",
                proposal=(
                    "call `open_background(url, group='Agent')` to spawn a "
                    "fresh background tab (does not steal user focus), "
                    "OR `attach_active()` if the task is explicitly "
                    "'drive the user's current tab'. Then re-run."
                ),
            )
        # rdp/env: safe to auto-fallback — isolated Chrome, no user collision.
        from .page import current_page
        current_page()
    return sess.cdp.attach(sess.current_target_id)
```

The existing `from ..errors import CDPError, ElementNotFound` at line 16 stays. `NeedsUserConfirm` is imported lazily inside the function to avoid changing the module's top-of-file import order (keeps the diff small and stays consistent with the existing lazy `from .page import current_page`).

### Step 4: Run the tests again

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_session_propagation.py -v
```

Expected: 4 PASS.

### Step 5: Make sure nothing else broke

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest -v
```

Expected: full suite passes (or matches the prior baseline).

### Step 6: Commit

```bash
cd /Users/metajs/gitRepos/labs/browser
git add browser-skill/src/browser_skill/primitives/interact.py \
        browser-skill/tests/test_session_propagation.py
git commit -m "$(cat <<'EOF'
feat(browser-skill): refuse silent focus-steal on extension backend

_attached_session() previously auto-called current_page() → attach_active()
when no tab was attached, silently grabbing the user's focused tab.
That collides with other agents / the user driving the same Chrome.
Raise NeedsUserConfirm instead, listing open_background first (default)
and attach_active second (only when explicitly wanted).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Reorder existing error-path proposals (open_background first)

**Files:**
- Modify: `browser-skill/src/browser_skill/primitives/page.py:135-140` (list_tabs)
- Modify: `browser-skill/src/browser_skill/primitives/page.py:161-166` (current_tab)
- Test: `browser-skill/tests/test_primitives_offline.py:81-92` (already exists — update assertion) + append a new ordering test

**Why:** Two existing `NeedsUserConfirm` raises list `attach_active` first. Reverse to match the new default rule.

### Step 1: Update the existing test assertion

Edit `browser-skill/tests/test_primitives_offline.py`. Find the test at line 81 (`test_list_tabs_raises_on_extension_with_zero_ghosts`). Currently asserts:

```python
assert "attach_active" in (exc_info.value.proposal or "")
```

Replace that line with:

```python
proposal = exc_info.value.proposal or ""
assert "open_background" in proposal
assert "attach_active" in proposal
# Default rule: open_background listed before attach_active.
assert proposal.index("open_background") < proposal.index("attach_active")
```

### Step 2: Run the updated test, confirm it fails

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_primitives_offline.py::test_list_tabs_raises_on_extension_with_zero_ghosts -v
```

Expected: FAIL — current proposal lists `attach_active` first.

### Step 3: Reorder the two proposal strings

Edit `browser-skill/src/browser_skill/primitives/page.py`.

**Lines 133-140** (`list_tabs` proposal) — replace:

```python
# Before
raise NeedsUserConfirm(
    what="extension backend has zero attached tabs",
    proposal=(
        "call `attach_active()` to drive the focused-window tab, "
        "or `open_background(url, group='Agent')` to spawn a new "
        "background tab in the Agent group"
    ),
)
# After
raise NeedsUserConfirm(
    what="extension backend has zero attached tabs",
    proposal=(
        "call `open_background(url, group='Agent')` to spawn a new "
        "background tab in the Agent group (does not steal user focus), "
        "or `attach_active()` to drive the focused-window tab if the "
        "task is explicitly 'use my current tab'"
    ),
)
```

**Lines 161-167** (`current_tab` proposal) — same shape:

```python
# Before
raise NeedsUserConfirm(
    what="no tab attached on extension backend",
    proposal=(
        "call `attach_active()` to attach the focused-window tab, "
        "or `open_background(url)` to spawn a background tab"
    ),
)
# After
raise NeedsUserConfirm(
    what="no tab attached on extension backend",
    proposal=(
        "call `open_background(url, group='Agent')` to spawn a "
        "background tab (does not steal user focus), or "
        "`attach_active()` to attach the focused-window tab if "
        "the task is explicitly 'use my current tab'"
    ),
)
```

### Step 4: Run the test again

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest tests/test_primitives_offline.py -v
```

Expected: PASS.

### Step 5: Make sure the rest of the suite is still green

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill
uv run pytest -v
```

Expected: PASS (baseline preserved).

### Step 6: Commit

```bash
cd /Users/metajs/gitRepos/labs/browser
git add browser-skill/src/browser_skill/primitives/page.py \
        browser-skill/tests/test_primitives_offline.py
git commit -m "$(cat <<'EOF'
feat(browser-skill): list open_background before attach_active in error hints

list_tabs() and current_tab() NeedsUserConfirm proposals now list
open_background first (the new default — no focus steal) and
attach_active second (only when explicitly driving the user's tab).
Matches the same ordering used in _attached_session() and SKILL.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: SKILL.md decision tree + soften boilerplate + sessionId footgun note

**Files:**
- Modify: `skill/SKILL.md` (insert new section before line 58, replace line 151 paragraph)

**Why:** The agent reads SKILL.md every invocation. Currently nothing tells it to prefer `open_background()` over `attach_active()`. Line 151 ("the boilerplate `attach_active()` at the top of every heredoc is still fine") actively encourages the wrong default.

### Step 1: Insert the new section before line 58

Use the Edit tool. Find the exact line `## Primitives surface (pre-imported in REPL)` (line 58) and insert the following BEFORE it:

```markdown
## First call: which attach should you reach for?

| Goal | Use | Why |
| --- | --- | --- |
| Reuse a tab opened in an earlier heredoc | `switch_tab("<saved targetId>")` | Deterministic, no popups, no focus steal |
| Spawn a new tab for automation **(default)** | `open_background(url, group="Agent")` | Does **not** steal user focus; isolated; safe for long flows |
| Drive the user's currently-focused tab ("read my email", "what's on my screen now") | `attach_active()` | Extension backend only. **Steals focus** — only when the user literally said "use my current tab" |
| Fresh isolated Chrome (rdp / env backend) | `new_tab(url)` | Standard `Target.createTarget`; not for extension backend |

**Rule of thumb:** Unless the user said "use my current tab" or "what I'm looking at", default to `open_background()`. Multiple agents (or this agent + the user) can share one Chrome that way without colliding on a single focus.

⚠️ **Always read the return value of an attach call before chaining.** If `attach_active()` / `open_background()` failed (a hook blocked the command, daemon refused, etc.), the next `type_text` / `click_at_xy` will surface as "requires sessionId" or "unknown sessionId" — that's the symptom, not the cause. The cause is the silent failure two lines up.

⚠️ **`sessionId` is daemon-internal plumbing — agents don't pass it.** If you see "unknown sessionId" or "requires a sessionId", the prior attach failed. Don't try to "look up" the sessionId; re-call `attach_active()` / `open_background()` / `switch_tab()` and verify the return value before the next primitive.

```

(Note the trailing blank line — that keeps the visual separation from the existing `## Primitives surface` heading.)

### Step 2: Soften the line-151 paragraph

Use the Edit tool. The exact old_string to replace is the entire line 151:

```
For ad-hoc "just drive my current tab" usage, the boilerplate `attach_active()` at the top of every heredoc is still fine — but for multi-step automation, handle-passing is deterministic.
```

Replace with:

```
`attach_active()` steals the user's focus — only use when the task is literally "drive my current tab". For everything else default to `open_background(url)` (new tab, no focus steal) or `switch_tab(<saved targetId>)` (heredoc continuity). See "First call: which attach should you reach for?" above.
```

### Step 3: Verify the edits stuck

```bash
grep -n "First call: which attach\|steals the user's focus\|sessionId.*is daemon-internal" /Users/metajs/gitRepos/labs/browser/skill/SKILL.md
```

Expected: at least three matching lines (one per new anchor phrase).

### Step 4: No test runs needed (docs-only). Commit.

```bash
cd /Users/metajs/gitRepos/labs/browser
git add skill/SKILL.md
git commit -m "$(cat <<'EOF'
docs(skill): steer agents toward open_background as the default attach

SKILL.md previously called the attach_active() boilerplate "fine", which
biased agents toward stealing the user's focused tab. New "First call"
section makes open_background the default, with attach_active reserved
for explicit "drive my current tab" intent. Adds two warning blocks
about sessionId-related errors so agents recover instead of stopping.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Full-suite smoke + plan-complete sentinel

### Step 1: Run the full test suite from both packages

```bash
cd /Users/metajs/gitRepos/labs/browser/browser-skill && uv run pytest -v 2>&1 | tail -40
cd /Users/metajs/gitRepos/labs/browser/browser-daemon && uv run pytest -v 2>&1 | tail -40
```

Expected: all tests pass, including the four new ones in `test_session_propagation.py` and the two new ones in `test_extension_upstream_errors.py`.

### Step 2: Confirm the six commits are present

```bash
cd /Users/metajs/gitRepos/labs/browser
git log --oneline -10
```

Expected (most recent first):

```
<hash> docs(skill): steer agents toward open_background as the default attach
<hash> feat(browser-skill): list open_background before attach_active in error hints
<hash> feat(browser-skill): refuse silent focus-steal on extension backend
<hash> feat(browser-daemon): make session errors point at the recovery path
<hash> fix(browser-skill): route close_tab through long-lived ws
<hash> fix(browser-skill): route open_background through long-lived ws
```

If any commit is missing or out of order, do NOT amend or rebase — note it for the reviewer and move on.

### Step 3: Done

No further action. The reviewer (next agent in the pipeline) will pick up from here.

---

## Notes for the executor

- **Working directory:** always `/Users/metajs/gitRepos/labs/browser`. Use absolute paths in tool calls; do not `cd` between files.
- **uv vs python:** tests use `uv run pytest`. If `uv` isn't on PATH, fall back to `python -m pytest` from the package directory — but try `uv` first, that's the project's convention.
- **Skip flaky pre-existing tests, don't fix them.** If a test was already failing before Task 1 began, note it in the final summary and proceed.
- **Don't restructure things outside the listed lines.** No drive-by cleanup. No renaming. No unrelated typo fixes. The PR is one focused change.
- **If a step's "expected" output doesn't match reality, STOP and report.** Don't guess your way past a failing test — the design might have a gap.
