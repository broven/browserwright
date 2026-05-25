# Resident Userscripts

Resident userscripts are the persistent automation leg of browserwright: an agent writes a Tampermonkey-style `.user.js` file, pushes it to the daemon, and the Chrome extension registers it with `chrome.userScripts`. This runs in parallel to CDP session control. Use CDP to open pages, inspect DOM, and verify behavior; use userscripts when code should keep running automatically on every matching page load.

## Mental model

- Source of truth is the local `.user.js` file you edit.
- `browserwright userscript push path/to/file.user.js` parses the header, sends it through `browserwright-daemon`, and the extension stores and registers it.
- Identity is `@namespace/@name`; pushing the same identity updates the existing script.
- Scripts run in Chrome's `USER_SCRIPT` world on matching pages, without a CDP session attached.

## Header spec

Supported v1 metadata directives:

```javascript
// ==UserScript==
// @name         Example Helper
// @namespace    bd.userscripts
// @match        https://example.com/*
// @include      https://example.org/*
// @exclude      https://example.com/admin/*
// @run-at       document-idle
// @version      1.0
// @description  Adds a useful page affordance
// ==/UserScript==
```

- `@name` is required.
- `@namespace` defaults to `bd.userscripts` when absent.
- At least one `@match` or `@include` is required.
- `@run-at` accepts `document-start`, `document-end`, or `document-idle`; default is `document-idle`.
- Unsupported directives such as `@grant`, `@require`, `@resource`, and `@connect` are ignored with warnings so pasted scripts degrade gracefully.

## Capability boundary

- Plain page JavaScript only.
- No `GM_*` APIs.
- No remote `@require` loading.
- No automatic watch mode; push explicitly after each edit.
- The popup shows matching scripts for the current site with per-script toggles and a master switch.

## Golden workflow

1. Write or edit `something.user.js`.
2. Push it: `browserwright userscript push something.user.js`.
3. Open the target site with `browserwright -s <id> -e ...`.
4. Verify the intended effect.
5. If red, edit the file, push again, reload or reopen the target page, and re-verify.

### One-step verify with `--verify`

If the target tab is already open, `--verify` collapses steps 2–4 into one
command: push, reload the live tab, and capture a fresh screenshot.

```bash
browserwright userscript push something.user.js --verify
```

On a successful push it reloads the currently-active matching tab, lets it
settle, captures a screenshot, and prints the screenshot path so you see the
result without a separate reload→screenshot round-trip. If the push fails it
returns the failure and does **not** reload/screenshot a stale page — fix the
script and push again. `--verify` is a browserwright convenience and is never
forwarded to the daemon. Open the target tab first; `--verify` reloads whatever
tab is currently active, it does not navigate for you.

## Verification menu

- UI change: capture a DOM snapshot or screenshot and inspect the visible result.
- Data pull: extract the data from the page and judge it against the expected shape.
- Pure injection: set/read a shared DOM sentinel such as `document.documentElement.dataset.usExample = 'ok'`; `window.__us_*` variables set in `USER_SCRIPT` are isolated from main-world CDP evals.
- Always verify after writing; do not assume a push worked just because it returned success.

## Command cheat sheet

```bash
browserwright userscript push ./example.user.js
browserwright userscript list --site=https://example.com/page
browserwright userscript toggle bd.userscripts/Example\ Helper --enabled=false
browserwright userscript logs --limit=20
browserwright userscript remove bd.userscripts/Example\ Helper
```

`install` is an alias of `push` and accepts `-` for stdin.
