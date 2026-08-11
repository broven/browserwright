# Privacy Policy — browserwright (Chrome extension)

_Last updated: 2026-08-11_

The **browserwright** Chrome extension is a local relay between the
browserwright daemon running on your own computer and your Chrome browser.

## What we collect

**Nothing.**

- The extension does not collect, store, transmit, or sell any personal data,
  browsing history, or page content.
- The extension does not use cookies, analytics, tracking pixels, or third-
  party SDKs.
- No account is required and no data is ever sent to a server operated by us
  or anyone else.

## How it works

The extension connects to a local daemon process on your own machine at
`ws://127.0.0.1:19989` (loopback only) and relays Chrome DevTools Protocol
commands for tabs you explicitly attach to a session. All communication stays
on your machine; nothing leaves it.

## What you should know

- Page content you ask an AI coding agent to work with is processed locally by
  that agent and its tools on your machine. The extension itself does not read
  or retain page content beyond relaying the commands you initiate.
- You are in control at all times: sessions appear as Chrome tab groups you
  can see and close, and removing the extension stops all activity.

## Contact

This extension is open source (AGPL-3.0). For questions, open an issue at
https://github.com/broven/browserwright.
