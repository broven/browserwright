# Agent input defaults

The resident executor installs `repl/human_input.py` in
`_Worker._bind_live_surface`, before handing its real Playwright `page` and
`context` to agent scripts or helper modules. Chained locators, `Page`/`Frame`
selector methods (`page.click(sel)`, `frame.fill(sel, v)`), child frames, and
pages created by `context.new_page()` share the defaults. Playwright objects
stay real Playwright objects and return values are unchanged.

The defaults apply on both backends. The facade-side pieces below exist only
on the extension backend; the cdp backend keeps Chrome's native input path.

## No focus stealing

Nothing in this module activates a tab, raises a window or calls
`page.bring_to_front()`. The extension backend drives the user's own Chrome,
and browser UI activation steals the desktop's active window from them.
Through `chrome.debugger`, `Page.bringToFront` really does activate the tab
and focus its window, so the rule is enforced by not issuing it.

Every step works on a tab that is not the active tab, in a window that is not
focused. The extension already keeps attached background tabs rendering
(`keepTabRendered()`: focus emulation plus an active web lifecycle; see
`test_l2_background_render.py`). Pointer moves, coalesced samples, wheel
scrolling, keyboard input and the post-wheel geometry re-reads therefore run
the same in the background as in the foreground. Nothing is degraded.
`test_human_input_no_focus.py` enforces this. It moves the user to another
active tab and a newly focused window, then runs click, fill and
scroll-into-view on the background session tab. It asserts that the input was
trusted, that every window's active tab and the focused window are unchanged,
and that `tabs.onActivated` / `windows.onFocusChanged` never fired for the
session tab or its window.

An agent that explicitly calls `page.bring_to_front()` still gets native
activation. That is the agent's choice, never a default.

## click()

`click()` approaches a random interior point of the target, away from dead
center, through densely sampled mouse moves. It starts from the last tracked
pointer position, or from a viewport edge on first use. It then calls the
native Playwright click with a short randomized press duration (35–95 ms).
Native click still owns actionability, navigation waiting, modifiers,
button, click count, `force`, `trial` and explicit `position`/`delay`.
Preparation and the final action share the caller's timeout.

Samples are sent in small groups without waiting for each ACK, so Chrome can
coalesce them like a real device (`PointerEvent.getCoalescedEvents()`). The
extension facade forwards a run of consecutive `mouseMoved` commands
concurrently. It finishes the run before it processes a press or any other
command, and browser-session setup stays sequential. Moves stay sequential
while a mouse button is held, which preserves Playwright's drag handling.

Generated coordinates follow the device-pixel grid. For generated click
positions, the extension facade leases dispatch-time rounding
(`Browserwright.setPointerGrid`, a facade-only CDP method). This covers a
target that moves between the approach and Playwright's final actionability
pass. The lease belongs to the CDP session and tab. It is released after the
action, including on failure, and dropped when that session detaches or the
tab goes away. Explicit caller positions keep native coordinates. On the cdp
backend the method does not exist and the click keeps native dispatch.

## Scrolling into view

Before an ordinary click or text fill, a target outside its clipping area is
revealed with native wheel input instead of programmatic `scrollIntoView`.
Geometry comes from a per-context Playwright selector engine
(`_browserwright_scroll`) that runs in Playwright's utility world. It answers
through a detached transport node that never enters the document. Document
scrolling, nested overflow containers and enclosing frames are handled, and
geometry is re-read after each wheel burst. A page that cancels wheel input
uses up the caller's timeout, as it would for a real user.

## fill()

For a visible editable text control (text-like `<input>`, `<textarea>`,
contenteditable), `fill()` does the following:

1. It clicks the control (as above) so focus comes from a trusted pointer
   gesture.
2. If the control has existing content, it selects it with a physical
   Control/Command+A chord and deletes it with Backspace. An empty control
   gets no selection or delete key.
3. It types each character with a randomized hold (35–105 ms). ASCII capitals
   and shifted punctuation use physical Shift chords with their own pauses.
   Characters that are not on the keyboard go through Playwright's normal
   text insertion.

The select-all modifier is Playwright's `ControlOrMeta`, which Playwright
resolves from the browser platform. The extension backend never reported one,
so a Mac looked like Linux and got Control+A. On macOS that moves the caret to
the line start instead of selecting. The extension's service worker now sends
its own `navigator.userAgent` in the relay hello (never read from a page).
`Browser.getVersion` returns it, so Playwright uses Meta+A, plus its native
macOS `selectAll` editing command, on a Mac. An older extension that does not
send the field keeps the legacy identity.

For contenteditable, isolated inspection first checks that focus and the caret
belong to the editor. If a click on rich media left the caret elsewhere, a
second click in exposed editor space restores it. If no such space exists,
Tab/Shift+Tab is used, which fires ordinary blur/focus events. If neither
works, the call falls back to native fill. A forced click that lands on an
overlay also falls back to native fill, so the requested control is still the
one replaced.

Native fill is kept for specialized input types (number, date, time, …), for
read-only, disabled and non-editable targets (with their native errors and
timeouts), and for `<select>` (use `select_option()`).

## Cost

Human input is slower than native input. These timings are from the headful
extension E2E on a background tab, on one machine; the artifact records the
numbers for each run:

| Call | Approx. wall time |
| --- | --- |
| `click()` on a target in view (first use, pointer enters from an edge) | ~4 s |
| `fill()` of 9 characters (includes its click and select-all/delete) | ~3 s |
| `click()` on a target ~2000 px below the fold (wheel scroll + click) | ~5 s |

Most of the time goes to the pointer approach. It takes at least 32 steps of
2–4 samples, with an 8–15 ms pause between steps, and each step waits for the
facade to forward its samples to the tab. Typing costs about 40–110 ms per
character.

## Opt-outs

- Per call: `locator.click(..., human=False)`, `locator.fill(..., human=False)`,
  and the same keyword on `page.click`/`page.fill`/`frame.click`/`frame.fill`.
  These run exactly the native Playwright behavior.
- Globally: `BW_HUMAN_INPUT=0` (or `false`/`off`) in the executor's
  environment, set on the daemon before it spawns executors.

Screenshot caret defaults are separate. They live in `repl/screenshot_defaults.py`
and are not affected by these switches.

## Verification

```bash
bash tests/daemon/e2e/run.sh \
  tests/daemon/e2e/test_human_input.py \
  tests/daemon/e2e/test_human_scroll.py \
  tests/daemon/e2e/test_human_pointer_layout.py \
  tests/daemon/e2e/test_human_input_no_focus.py -v
```

Artifacts in `tests/daemon/e2e/_artifacts/`:

- `human-input.json`: trusted key/pointer events, coalesced samples, opt-outs
  and input-type compatibility.
- `human-scroll.json` / `human-scroll-bottom.json`: wheel→scroll ordering for
  the document, nested scrollers and frames.
- `human-pointer-layout.json`: pixel-grid snapping when the target moves
  during the approach.
- `human-input-no-focus.json`: Chrome tab/window state before and after,
  activation/focus events, and per-call timings on a background tab.

The first three use headless Chrome with only the extension channel, which
keeps desktop cursor movement out of the event stream. The no-focus test is
headful by default.
