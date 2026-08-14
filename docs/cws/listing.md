# Chrome Web Store listing materials

Everything needed to list the extension on the Chrome Web Store. The upload
artifact is the **release zip repackaged with `manifest.json` at the zip root**
(CWS rejects the GitHub Release zip, which nests everything under
`chrome-extension/`):

```bash
gh release download vX.Y.Z --repo broven/browserwright \
  --pattern "browserwright-extension-X.Y.Z.zip" --dir /tmp/bwcws
cd /tmp/bwcws && rm -rf unpacked && mkdir unpacked && cd unpacked \
  && unzip -qo ../browserwright-extension-X.Y.Z.zip \
  && mv chrome-extension/* . && rmdir chrome-extension \
  && cd .. && zip -qr browserwright-cws-X.Y.Z.zip unpacked -x "*.DS_Store"
```

Upload at <https://chrome.google.com/webstore/devconsole> (requires a Chrome Web
Store developer account — one-time $5 registration, Google account with 2FA).

## Store listing

| Field | Value |
|---|---|
| **Name** | browserwright |
| **Summary** (≤132 chars) | Local relay that lets AI coding agents drive your Chrome with real Playwright, via the browserwright daemon. (108 chars) |
| **Category** | Developer Tools |
| **Language** | English (United States) |
| **Single purpose** | Local bridge between AI coding agents and the user's browser (CDP relay) |
| **Privacy policy URL** | hosted copy of `privacy-policy.md` (see below) |
| **Icons** | `chrome-extension/icons/icon-{16,32,48,128}.png` — real artwork, all sizes present |

### Detailed description

> **browserwright** is the browser backend for AI coding agents (Claude Code,
> Codex, pi, and other agent harnesses). It is a **local-only bridge**: the
> extension connects to the browserwright daemon running on your own machine
> (`ws://127.0.0.1:19989`) and relays Chrome DevTools Protocol commands to the
> tab you attached.
>
> **What it does**
>
> - Lets an AI agent drive your real Chrome: open pages, click, type, fill
>   forms, submit workflows, take accessibility snapshots — through real
>   Playwright running on your machine.
> - Sessions appear as **Chrome tab groups**, so you always see exactly what
>   the agent is doing and can intervene at any time.
> - Optional "resident userscripts" keep small automation scripts running on
>   matching pages (off by default).
>
> **Privacy**
>
> - Everything stays on your machine: the agent, the daemon, and the extension
>   all run locally and talk over loopback only. No data is sent to any server.
> - The extension only relays commands for tabs you attach to a session.
> - Open source (AGPL-3.0): https://github.com/broven/browserwright
>
> **Permissions explained**
>
> - `debugger` — required to drive tabs over the Chrome DevTools Protocol; this
>   is the extension's entire function.
> - `<all_urls>` — the agent operates on whatever page you ask it to work on;
>   it never acts without an explicit session.
> - `userScripts` — powers the optional resident-userscripts feature.
> - `tabs` / `tabGroups` — sessions are shown as tab groups you control.
> - `storage` / `alarms` — session bookkeeping and the MV3 service-worker
>   keepalive.

### Permission justification (dashboard field)

> `chrome.debugger` is the only way to drive a tab with the Chrome DevTools
> Protocol, and driving tabs is the single purpose of this extension: it is a
> local relay between an AI coding agent and the user's browser. Every action
> is user-initiated — the user creates a session, attaches a tab, and directs
> the agent — and every byte stays on loopback. The extension is open source
> (AGPL-3.0, github.com/broven/browserwright), published by the browserwright
> project, and shows all work in user-visible tab groups.
>
> `<all_urls>` is required so the agent can interact with whichever page the
> user asks it to work on; the extension never navigates, reads, or modifies
> anything without an explicit session on that tab.
>
> `userScripts` backs the optional, off-by-default "resident userscripts"
> feature that keeps a user-authored script running on matching pages; it is
> never enabled unless the user pushes a script through the CLI.

## Privacy policy

CWS requires a hosted privacy policy URL. The text lives in
`privacy-policy.md`; host it somewhere stable (GitHub Pages / raw file /
slugjar). It must state: no data collection, local-only processing, no third
parties, no cookies.

## Screenshots

CWS requires at least one screenshot, 1280x800 or 640x400 (one of each is
recommended). The extension's only UI is the popup; a good screenshot is the
popup rendered on a neutral desktop background at 1280x800 (see the manual
recipe below).

## Automated publishing

Since vX.Y.Z releases, the `publish-cws` job in `.github/workflows/release.yml`
performs the repackaging + upload + publish steps below automatically on every
pure `vX.Y.Z` tag (pre-release tags skip the store — see RELEASING.md "Store &
pre-releases"). The manual recipe is kept as the fallback for store-only hot
fixes or a broken CI.

The store item id is `okgnalaalckoaeledbjhpjiccmcdceeb`; the live listing is
https://chromewebstore.google.com/detail/browserwright-daemon-rela/okgnalaalckoaeledbjhpjiccmcdceeb

### Manual upload steps (dashboard)

1. <https://chrome.google.com/webstore/devconsole> → **Add new item** → upload
   `browserwright-cws-<version>.zip`.
2. Fill the listing (fields above) + upload screenshots and the small promo
   tile (440x280, optional).
3. Set the **privacy policy** URL and tick the data-safety answers ("does not
   collect any data" / "no personal data transmitted").
4. **Submit for review.** Expect a review round for the `debugger` +
   `<all_urls>` permission set; the justification text above is written for
   that round. Distribution: publish on submit (public listing), not staged.
