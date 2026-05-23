# AI-Authored Userscripts — Design

**Date**: 2026-05-21
**Branch**: `scipt-inject`
**Status**: Design approved, ready for implementation planning

## Summary

Add a **new, parallel capability** to the browser project: a Code Agent authors
Tampermonkey-style userscripts, and the Chrome extension runs them
**autonomously and persistently** on matching sites during the user's normal
browsing — no agent or daemon needs to be present at runtime.

This is **purely additive**. The existing CDP session-driven browser control
(`chrome.debugger` relay, sessions, `browserwright` heredoc primitives) is kept
untouched. The two legs run side by side and do not interfere.

## Decisions (locked)

1. **Positioning** — Additive. Existing CDP session-driven control unchanged.
2. **Runtime model** — True Tampermonkey: scripts live in the extension, inject
   autonomously on matching sites during normal browsing, no agent/daemon
   required at runtime. Runtime mechanism: **`chrome.userScripts`** (MV3 API
   built for script managers; `USER_SCRIPT` world).
3. **Delivery** — Reuse the existing daemon↔extension relay ws. New
   `userscript.*` message types (not mixed into CDP frames). Extension stores
   scripts in `chrome.storage.local` and registers via `chrome.userScripts`.
4. **Activation** — Install takes effect **immediately, fully automatic, no
   confirmation gate**.
5. **Permissions** — Extension declares `host_permissions: ["<all_urls>"]` plus
   the `userScripts` permission.
6. **Capability surface** — **Plain page JS only** (`USER_SCRIPT` world).
   No `GM_*` APIs in v1. (Greasemonkey shim is a future level-b upgrade.)
7. **Verification** — No mandatory assertion. Expose the **existing CDP
   primitives** (screenshot / DOM read / `eval` / data extraction) as the
   verification toolbox; a dedicated doc strongly prompts the agent to verify
   per the script's intent and pick the right method.
8. **Popup (v1)** — On the current site, list scripts that match this site, each
   with a Switch toggle (on/off). Plus a global master switch. That is the whole
   v1 popup scope.
9. **Docs** — A dedicated "writing userscripts" Markdown, linked from the main
   `SKILL.md`.
10. **Source of truth** — Local `.user.js` files the agent edits; the extension
    store is a projection synced from them. Sync is an **explicit `push`** (save
    does not auto-go-live).
11. **Script format** — Self-contained `.user.js` with a standard
    `==UserScript==` metadata header (Tampermonkey-compatible). We parse the
    header; no CLI flags for metadata. Identity = `@namespace + @name`.

## Backstops (zero-friction, default-on)

Even with full-auto activation:
- Popup shows the **resident-scripts list for the current site** + per-script
  Switch + a **global master switch**.
- Each injection appends an **audit log** entry `{ts, scriptId, tabId, url}` to
  `chrome.storage.local` (ring buffer, ~500 cap). Surfaced via
  `browserwright userscript logs` for the agent to debug "did it inject?".

## Architecture

### 1. Extension side — runtime & storage

**Storage** (`chrome.storage.local`, key `userscripts`): one record per script:
```
{ id, name, namespace, version, description,
  code, matches: [...], excludeMatches: [...], runAt,
  enabled, createdAt, updatedAt }
```
- Identity is `@namespace + @name` (Tampermonkey convention) — re-installing the
  same script **updates** rather than duplicates. `id` may be a short hash of
  the identity for convenience.
- `matches`/`excludeMatches` are Chrome match patterns from `@match`/`@include`/
  `@exclude`. `runAt ∈ document_start | document_end | document_idle`.

**Registration**: on SW wake, read all `enabled` scripts and
`chrome.userScripts.register([...])` into the `USER_SCRIPT` world.
install/update/remove/toggle do incremental `register`/`update`/`unregister`.
Requires the extension to be in developer mode (already satisfied by unpacked
load) and a `"userScripts"` permission entry in the manifest.

**Isolation from CDP path**: userScripts registration is an independent
chrome-API call chain in the SW; it never touches the `chrome.debugger` relay. A
new `userscript.*` branch sits beside the CDP branch in the message dispatcher.

**Popup (v1)**: on open, get the active tab URL → in the SW, match it against
each script's `matches` → return the list of scripts hitting this site → render
name + Switch each. Flipping a Switch updates `enabled` →
re-register/unregister → live. Master switch is one flag; when off, skip all
registration.

**Audit log**: each injection appends `{ts, scriptId, tabId, url}` (ring buffer,
~500). Not necessarily shown in popup; for post-hoc debugging.

### 2. Daemon channel + `browserwright userscript` CLI

**Channel reuse**: same relay ws, new message types (not CDP frames):
```
{ type: "userscript.install", payload: {...}, reqId }
→ { type: "userscript.result", reqId, ok, data }
```
Extension dispatcher gains a `userscript.*` branch beside CDP. Daemon gains a
small reqId-keyed RPC wrapper.

**CLI** (`browserwright userscript ...`) — **does NOT require `BD_SESSION`**
(it manages the persistent store, not an agent session). Still needs the daemon
running + extension connected; loud-fails with "load the extension first"
otherwise.
- `install <file|->` / inline blob — accept a full `.user.js` (with header);
  parse header for metadata; register.
- `push <name>` — read the local `.user.js`, parse, sync into store, re-register.
- `list [--site=<url>]` — all scripts, or those matching a site
  (id/name/matches/enabled).
- `remove <name>` — unregister + delete record.
- `logs [--id=<id>] [--limit=N]` — pull audit log.

### Input model — file-first, blob also accepted

The extension store is the **runtime** source of truth; the **editing** source
of truth is local `.user.js` files. CLI doesn't care where code comes from.

- **First creation**: agent `Write`s a complete `.user.js` (one shot), or pipes
  an inline blob. Good for one-off small scripts and dropping in existing
  userscripts.
- **Iteration (primary path)**: agent uses native **Read + Edit** on the local
  file — surgical string replacement, **zero full-content retransmission**.
  This is the token-efficient, Agent-native loop.
  - (Verified: Claude Code's `Edit` is exact `old_string`→`new_string`
    replacement, not line-number based; `Read` shows `cat -n` numbers for
    display only. So editing a file beats re-piping a blob for every change.)
- **Sync**: explicit `userscript push <name>` → daemon reads file, parses
  header, pushes to store, re-registers → live immediately.

Local files live at `~/.browserwright/userscripts/<name>.user.js`.

### Script format — self-contained `.user.js`

```js
// ==UserScript==
// @name         HN Tidy
// @namespace    bd.userscripts
// @match        https://news.ycombinator.com/*
// @run-at       document-idle
// @version      1.0
// @description  Collapse HN sidebar noise
// ==/UserScript==
(function () { /* … */ })();
```

**v1 parses**: `@match` / `@include` / `@exclude` → match patterns;
`@run-at` → runAt; `@name @namespace @version @description` → identity/display.

**Graceful degradation**: unsupported directives (`@grant`, `@require`,
`@resource`, `@connect`, …) are parsed but **warned, not hard-failed** — old
userscripts paste in without crashing; advanced powers simply inactive until a
future level-b/c upgrade. Warnings returned to the caller.

## End-to-end workflow

```
Write hn-tidy.user.js  →  userscript push hn-tidy
   →  BD_SESSION=$sid browserwright <<'PY'   (open HN, screenshot/DOM assert)
   →  green: done, resident;  red: Edit file → push → re-verify
```

The "install" leg and the "open a page and verify" leg are separate commands;
the doc stitches them into one recommended flow.

## Documentation

New `skill/userscripts.md` (linked one line from main `SKILL.md`):
- **Mental model**: resident-Tampermonkey leg, parallel to CDP session-driven;
  `.user.js` files are truth, `push` to go live, `<all_urls>` + full-auto.
- **How to write**: `==UserScript==` header spec (v1-supported directives +
  degradation), capability-a boundary (plain page JS, no `GM_*`).
- **Golden workflow** (strong prompt): `Write/Edit .user.js → push → open target
  site via CDP heredoc and verify → red ⇒ Edit → push → re-verify`.
- **Verification menu** (pick by intent): UI change → DOM snapshot/screenshot;
  data pull → extract and judge correctness; pure injection → read a
  shared DOM sentinel such as `document.documentElement.dataset.us*` (not `window.__us_*`, which is isolated in `USER_SCRIPT`). Emphasize: **always verify after writing**.
- **Command cheat-sheet**: `install / push / list / remove / logs`; popup shows
  this-site scripts + Switch.

## Testing strategy

Aligned with the existing ext-e2e harness (Chrome-for-Testing, port 29989,
extension backend; `tests/e2e/run.sh`).
- **Extension unit**: header parsing, match decision, register/unregister, popup
  this-site filter + Switch toggle.
- **E2E**: install a script → open matching site → assert sentinel/DOM took
  effect → toggle off → assert no longer injected → remove.
- **AI-E2E**: give a sub-agent a real intent ("change X to Y on this site"),
  check it completes the write→push→verify→green loop. North-star case; when it
  goes red, fix the skill/doc — do **not** overfit (no hardcoded wording, use
  variants).

## Out of scope (v1)

- `GM_*` APIs (storage, cross-origin `GM_xmlhttpRequest`, `GM_addStyle`,
  `GM_registerMenuCommand`) — future level b.
- `@require` / `@resource` / `@connect` remote-code support — future level c.
- Auto-sync on file save (watch mode) — v1 uses explicit `push`.
- Per-site permission prompts / activation confirmation gate — v1 is full-auto.
- Popup features beyond this-site list + Switch + master switch.
