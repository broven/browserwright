# Bug A — Service worker idle-termination drops inbound daemon requests

**Status:** Open. Root cause confirmed; no reliable fix on the WebSocket transport.
Recommended fix is **native messaging** (see below). This document is a handoff
so the next implementer does not re-walk the dead ends.

**Related fix already landed:** Bug B (`Fix tab grouping failure with multiple
browser windows`, commit on branch `fix/create-tab-target-normal-window`) is a
SEPARATE, fully-fixed issue. Do not conflate them.

---

## Symptom

After the extension's MV3 service worker (SW) has been idle for roughly its
~30 s reaper window, the first inbound request from the daemon
(`openBackgroundTab`, `Target.getTargets` scoping, facade visibility check)
fails. Observed daemon-side errors, all the same underlying cause:

- `BrowserwrightDaemon.openBackgroundTab failed: ws closed: no close frame received or sent`
- `Target.getTargets failed: getTargets scoping failed: TimeoutError()`
- daemon log repeatedly: `facade(ext) scoped visibility check failed: TimeoutError()`

It works reliably **only while a DevTools console is held open on the SW** —
because an open inspector keeps the SW alive. That "fix" is not shippable.

## Root cause (confirmed empirically on the user's real Chrome)

The MV3 service worker's **JavaScript execution context is suspended** when the
SW goes idle. The architecture has the daemon *push* requests into the
extension over a WebSocket that the SW owns. When the SW is suspended:

1. The TCP/WebSocket connection can stay ESTABLISHED at the OS level (verified
   with `lsof` — Chrome↔daemon socket still open after 90 s idle), but
2. the SW's `ws.onmessage` callback is **not scheduled to run**, so inbound
   frames are never handled, and the daemon times out.

Critically: **an inbound WebSocket frame is NOT a registered MV3 wake event.**
Only events like `chrome.runtime.onMessage`, `chrome.alarms.onAlarm`,
`chrome.runtime.onConnect`, and **native-messaging port messages** can revive a
dormant SW. Bytes arriving on a socket the suspended SW used to service do not.

This is an inbound-request model fighting the MV3 lifecycle. The platform does
not support "external process reaches a dormant SW over a raw socket."

## What was tried and why each failed

1. **WebSocket in the SW + self-driven `pingLoop` (every 20 s) + `chrome.alarms`
   backstop** (the original design). The SW's own `while(true)` ping loop dies
   *with* the SW when it is reaped, so nothing re-arms the keepalive. The
   `chrome.alarms` (30 s min) wakes the SW but the next request still races the
   re-suspension. Net: still dies in practice.

2. **Move the WebSocket into an offscreen document.** Offscreen docs have no
   idle-termination timer, so the socket survives. BUT:
   - Offscreen documents expose a **heavily restricted** chrome surface:
     `chrome.runtime.getManifest` is `undefined`, `chrome.storage` is
     `undefined`. (This caused several false leads — hello aborted on a
     TypeError before sending.)
   - **`chrome.runtime.sendMessage` from the offscreen doc to the SW silently
     fails** in this Chrome: the promise resolves, no error, but the SW's
     `onMessage` listener never fires — even with the SW kept awake and the
     listener registered synchronously at top level.
   - `chrome.runtime.connect` (Port) offscreen→SW **also** never fires the SW's
     `onConnect`.
   - The reverse direction (SW→offscreen) works fine.
   - **Structural dead end regardless of the dead channel:** offscreen docs
     cannot call `chrome.debugger` / `chrome.tabs` / `chrome.tabGroups`. Every
     real request must execute in the SW. So even a *working* offscreen→SW
     channel would still have to wake the SW — which is the unsolved part.
     Offscreen relocates the socket but cannot do the work. Abandon this.

3. **Daemon-driven keepalive: have the daemon push a `server-ping` frame every
   15 s** (mirroring how the playwriter relay keeps its SW alive). Implemented
   in `relay.py` (`_server_ping_loop`, started per-connection in `_handler`) and
   a matching `case "server-ping"` no-op in `background.js`. Verified the daemon
   *does* send the frame on a sub-30 s interval (a raw test ws received
   `{"type":"server-ping"}` at 15 s). **Still failed**: after 90 s idle the
   request timed out, and `lsof` showed the Chrome↔daemon socket *still
   established*. Conclusion: in this Chrome, application-level inbound ws
   activity does **not** reliably reset the SW idle reaper / un-suspend the SW's
   JS. The Chrome-116 "WebSocket activity extends SW lifetime" behavior is not
   dependable here. This is the most important negative result: **no ws-based
   keepalive — self-driven or daemon-driven — fixes it.**

The `server-ping` experiment was reverted from the tree (both `relay.py` and the
`background.js` `case`). The reference implementation is preserved in the
appendix below so it does not have to be rewritten if revisited.

## Recommended fix: native messaging

`chrome.runtime.connectNative()` is the **only** MV3 mechanism that is officially
specified to (a) keep the SW alive while the port is open — exempt even from the
5-minute hard cap — and (b) deliver inbound messages from an external process
that **wake a dormant SW** (the native port is a registered wake source). This
is exactly what password managers (KeePassXC, 1Password desktop integration) use
for reliable desktop→extension inbound.

Shape:

- Register a **native messaging host manifest** in the macOS location
  `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/<name>.json`
  (Windows = registry key; Linux = `~/.config/google-chrome/NativeMessagingHosts/`),
  naming the allowed extension ID and the host executable path. The daemon's
  installer can drop this file.
- The host "executable" speaks Chrome's native-messaging wire protocol
  (**4-byte little-endian length prefix + JSON** on stdin/stdout) — NOT
  WebSocket. So write a small **stdio↔daemon shim** (~50 lines of Python) that
  Chrome spawns and that relays to the existing daemon (over the current
  `ws://127.0.0.1:19989`, a unix socket, or by *being* the daemon). The
  WebSocket relay can likely be retired entirely.
- In the SW: `chrome.runtime.connectNative(<name>)` on startup; handle requests
  in `port.onMessage` (executing via `chrome.debugger`/`chrome.tabs`/
  `chrome.tabGroups`, all of which remain SW-only); reply via `port.postMessage`.
- In `port.onDisconnect`: log and **immediately reconnect** (`connectNative`
  again). Add a `chrome.alarms` 30 s backstop that reconnects if the port is
  null (covers the rare "SW died for an unrelated reason and the port is closed"
  window).
- **Do NOT** add an `onConnectExternal` handler in the same SW — the documented
  #2688 "native port dies after ~5 min" bug only reproduces when
  `connectNative` is combined with `connect()`/`onConnectExternal`. Plain
  native-host-only is exempt from the cap.

Cost: a host-manifest install step + the stdio shim. That is the price of the
only design where "always-reachable inbound channel" is a platform guarantee
rather than a keepalive hack.

### Alternative (cheaper v1, accept latency): flip to extension-dials-out

If the native-messaging install cost is not yet worth it, the next-best,
ecosystem-standard pattern (used by browser-use / OpenClaw-style CDP relays) is
to **invert the connection direction**: the extension dials *out* to the daemon
and the daemon **long-holds** each request until the extension's outbound
connection arrives, instead of pushing into a possibly-dormant SW. Pair with a
`chrome.alarms` (30 s) tick that reconnects if the socket is down. Worst-case
wake latency ≈ the alarm period (≤30 s). Acceptable if the daemon tolerates
up-to-30 s pickup; migrate to native messaging when sub-second inbound is
needed. NOTE: the current daemon is push-initiated, so this also requires
daemon-side changes (queue/long-poll requests per session).

## Reference for playwriter (the upstream this project mirrors)

`git@github.com:broven/playwriter.git`, `extension/src/background.ts` +
`offscreen.ts`. Confirmed architecture:
- WebSocket lives in the **service worker**, extension **dials out** to
  `ws://127.0.0.1:19988/extension`.
- The offscreen document is used **only for MediaRecorder/screen recording** —
  it never touches the ws, CDP, tabs, or tabGroups. (Matches the conclusion
  that offscreen is the wrong home for the relay.)
- Keepalive: the **server sends periodic `ping`**, the SW replies `pong`; plus a
  3 s `maintainLoop` reconnect. (Note: this is the same daemon-driven heartbeat
  idea as experiment 3 above. If playwriter stays alive with it and browserwright
  does not, the difference is worth investigating — it may be Chrome-version- or
  profile-specific, or playwriter may stay alive via its active `chrome.debugger`
  session, see next.)

## Lead worth checking before building native messaging

Chrome **118+** keeps the SW alive while a `chrome.debugger` session is
**actively attached**. browserwright attaches `chrome.debugger` per driven tab.
So Bug A may only bite in the **idle-with-nothing-attached** window (the first
request after a long pause). If verified, a lighter mitigation may exist:
keep a debugger session attached, or accept that the first request after long
idle needs one `chrome.alarms`-driven wake. Verify empirically:
1. Attach a tab via the extension.
2. Leave it 2+ minutes with no activity.
3. Issue a request — does it succeed (debugger keepalive held) or time out?
If it succeeds, scope Bug A down to "cold idle, nothing attached" and the fix is
much smaller than full native messaging.

---

## Appendix — reverted `server-ping` reference implementation

Daemon side (`src/browserwright/daemon/server/relay.py`, inside `RelayServer`):

```python
SERVER_PING_INTERVAL_S = 15.0

async def _server_ping_loop(self, ext: "_ExtensionConn") -> None:
    try:
        while True:
            await asyncio.sleep(self.SERVER_PING_INTERVAL_S)
            try:
                await ext.conn.send(json.dumps({"type": "server-ping"}))
            except websockets.exceptions.ConnectionClosed:
                return
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("server-ping send failed: %r", e)
                return
    except asyncio.CancelledError:
        return
```

In `_handler`, after `self._extensions[temp_key] = ext`:

```python
ping_task = asyncio.ensure_future(self._server_ping_loop(ext))
```

In the `finally:` of `_handler`, before the pop():

```python
ping_task.cancel()
with contextlib.suppress(asyncio.CancelledError):
    await ping_task
```

Extension side (`chrome-extension/background.js`, in `handleDaemonMessage`'s
`switch (type)`):

```javascript
case "server-ping":
  // Daemon-driven keepalive heartbeat. Receiving it fires ws.onmessage; no
  // reply required. NOTE: empirically insufficient to keep the SW alive in the
  // tested Chrome — kept here only as reference.
  return;
```

This is verified to *send* correctly but verified NOT to keep the SW alive.
Re-enable only if combined with something that actually un-suspends the SW.
