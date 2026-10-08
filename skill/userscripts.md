# Resident Userscripts

Resident userscripts are the persistent automation leg of browserwright: an agent writes a Tampermonkey-style `.user.js` file, pushes it to the daemon, and the Chrome extension registers it with `chrome.userScripts`. This runs in parallel to CDP session control. Use CDP to open pages, inspect DOM, and verify behavior; use userscripts when code should keep running automatically on every matching page load.

## Mental model

- Source of truth is the local `.user.js` file you edit.
- `browserwright userscript push path/to/file.user.js` parses the header, sends it through `browserwright-daemon`, and the extension stores and registers it.
- Identity is `@namespace/@name`; pushing the same identity updates the existing script.
- Scripts run in Chrome's isolated `USER_SCRIPT` world on matching pages (or the page's own world with `@inject-into page`), without a CDP session attached.

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
// @inject-into  content
// @version      1.0
// @description  Adds a useful page affordance
// ==/UserScript==
```

- `@name` is required.
- `@namespace` defaults to `bd.userscripts` when absent.
- At least one `@match` or `@include` is required.
- `@run-at` accepts `document-start`, `document-end`, or `document-idle`; default is `document-idle`.
- `@inject-into page` runs the script in the page's MAIN world, so it can read and patch page-owned globals (`window.jQuery`, app state, `fetch`). `content` and `auto` keep the isolated `USER_SCRIPT` world, which is the default. Use `page` only when you must hook page code: the page can see and tamper with the script there. The isolated world also enforces its own CSP, so injecting an inline `<script>` from it to reach the page does **not** work; use `@inject-into page` instead.
- Unsupported directives such as `@grant`, `@require`, `@resource`, and `@connect` are ignored with warnings so pasted scripts degrade gracefully.

## Capability boundary

- Plain page JavaScript only.
- No `GM_*` APIs.
- No remote `@require` loading.
- No automatic watch mode; push explicitly after each edit.
- The popup shows matching scripts for the current site with per-script toggles and a master switch.

## Reacting to change: observers first

Most target sites render after load, swap content in place, or route without a full page load (SPAs), so the element a script wants often is not there at `document-idle`.

The rule for all of it:

> **Reach for an observer first. If no observer can express the signal, use the event
> that already fires on the change. Only when neither exists fall back to a timer —
> a one-shot `setTimeout`, or a bounded `requestAnimationFrame` — and never to a
> polling loop.**

An observer is scheduled by the browser, fires exactly when the change happens,
and costs nothing while the page is idle. Polling (`setInterval`, or a
`setTimeout` retry loop re-querying the DOM) burns CPU on every tick, fires
late, and keeps firing after the page has settled. A page can also change before
`document-idle`; if you need to catch that, inject with `@run-at document-start`.

### Pick the observer by the signal, not by habit

| You want to react to… | Use | Notes |
| --- | --- | --- |
| nodes added / removed / replaced | `MutationObserver({childList:true, subtree:true})` | observe the narrowest stable container, not `document` |
| an attribute, `class`, or text change | `MutationObserver({attributes:true})` / `{characterData:true}` | add `attributeFilter:[…]` to cut noise; `attributeOldValue`/`characterDataOldValue` to read the previous value |
| an element's box size changed | `ResizeObserver` | the tool for "a native control animates its own width", lazy images settling, layout reflow |
| is it on screen / near the viewport | `IntersectionObserver` | `rootMargin` to prefetch, `threshold` for partial visibility; `scrollMargin` for scroll containers |
| …and it must not be occluded | `IntersectionObserver({trackVisibility:true})` | Chrome-only; read `entry.isVisible` |
| a page-load / resource / layout-shift / long-task signal | `PerformanceObserver({type, buffered:true})` | `PerformanceObserver.supportedEntryTypes` lists what this browser supports |
| a browser deprecation, intervention, or CSP violation | `ReportingObserver({types:[…], buffered:true})` | explains otherwise-mysterious breakage |
| viewport, dark mode, reduced motion | `matchMedia(q).addEventListener("change", …)` | an event, not an `*Observer` interface, but the same job |
| SPA route change | the `navigation` `navigate` event; else `popstate`/`hashchange` + a `MutationObserver` on the app root | |
| mobile keyboard / visual viewport | `visualViewport` `resize`/`scroll` | |
| tab hidden/shown, page frozen/restored | `visibilitychange`, `pageshow`/`pagehide`, `freeze`/`resume` | |
| a CSS transition or animation finished | `transitionend` / `animationend`, or `el.getAnimations()` + `Animation.finished` | no observer needed — the event is the signal |
| fonts finished loading | `document.fonts.ready` / `"loadingdone"` | |

Browser support (MDN): `MutationObserver`, `ResizeObserver`, `IntersectionObserver`,
`PerformanceObserver`, `ReportingObserver`, and `matchMedia` change events are all
Baseline / widely available in the Chrome that the extension backend runs.
Experimental or Chrome-only — **check support before relying on it**:
`IntersectionObserver.trackVisibility` and `.delay`, `ResizeObserver`
`box:"device-pixel-content-box"`, `PressureObserver` (compute pressure, Chrome
desktop only), and `FileSystemObserver` (non-standard, origin trial). There is
**no `StyleObserver`**, and no observer at all for canvas pixels, network
response bodies, or another world's JS variables — see below.

### The fallback ladder (only when no observer fits)

1. **A discrete DOM/CSS event** — still event-driven and free while idle:
   `transitionend`, `visibilitychange`, a `matchMedia` change, SPA nav events,
   `focus`/`blur`, `input`. Prefer it over an observer when the change already
   emits an event.
2. **One-shot `setTimeout`** — to wait a known settle window (let the page paint
   after a navigation), or to *debounce* an observer burst into a single pass
   (`setTimeout(fn, 0..250)` behind a queued flag). One shot, not a loop.
3. **`requestAnimationFrame`** — only to sample per-frame geometry that no
   observer covers: an element moved by a CSS `transform`, a scroll-driven
   position, an overlay that must track a moving box. It **must be started by a
   real event and be bounded** — cap the self-scheduled frames, or stop when the
   geometry stops changing. An unbounded
   `function loop(){ …; requestAnimationFrame(loop) }` runs at ~60fps forever with
   the mouse parked; that is the classic "the userscript is eating a core" bug
   (a shipped bilibili card-blocker did exactly this).

Never `setInterval` for DOM state. Never a `setTimeout` retry loop that polls
`querySelector` every N ms.

### No observer exists for these

- **A JS variable in the page's world changed.** You cannot observe it from the
  isolated `USER_SCRIPT` world; read it on demand, or move to `@inject-into page`
  and wrap the setter / install a `Proxy`.
- **Computed style changed without a box change.** No `StyleObserver`; use the
  `transition*`/`animation*` events, or observe the element that does resize.
- **Network responses / `fetch` bodies.** Not an observer; hook `fetch`/`XHR`
  from `@inject-into page`, or watch `PerformanceObserver` `resource` entries for
  timing only (no body).
- **Canvas / WebGL pixels.** No observer; hook the draw calls in the page world.
- **User input.** Plain DOM events, not observers.

### A MutationObserver helper

Call `cb` once for every element matching `selector`, now and whenever one appears later — the "wait for the element the SPA hasn't rendered yet" case:

```javascript
function onElement(selector, cb, root = document) {
  const handle = (el) => {
    if (el.dataset.usSeen) return; // idempotent: each element handled once
    el.dataset.usSeen = "1";
    cb(el);
  };
  root.querySelectorAll(selector).forEach(handle);
  const observer = new MutationObserver(() =>
    root.querySelectorAll(selector).forEach(handle));
  observer.observe(root === document ? document.documentElement : root,
    { childList: true, subtree: true });
  return observer; // call observer.disconnect() once you are done
}

onElement("article.post", (post) => post.classList.add("us-highlight"));
```

- Observe the narrowest stable container you can (a feed list, not `document`) — every mutation under it re-runs the callback. A `subtree:true` observer on `documentElement` that re-queries the whole document on every record is a CPU sink on a busy page; debounce the burst into one pass.
- Mark handled elements (`dataset`) so repeated callbacks never double-apply a change.
- `disconnect()` when the job is one-shot (waiting for a single element); keep the observer when content keeps arriving (feeds, infinite scroll, SPA route changes).
- Keep a `ResizeObserver`/`IntersectionObserver` reference if you observe and later need to `unobserve` — and never resize an element inside its own `ResizeObserver` callback without an `expected size` guard (it logs `ResizeObserver loop completed with undelivered notifications`).

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
