# @browserwright/pi

Two tools for [pi](https://github.com/badlogic/pi-mono), each one call to the
[browserwright](https://github.com/broven/browserwright) CLI:

```
bw_web_fetch(url, timeout?)    → browserwright markdown <url>    the page as Markdown
bw_web_search(query, timeout?) → browserwright search <query>    ranked links + SERP features
```

Session lifecycle, extraction, `/goto` link decoding and teardown all live in
the CLI; this package declares the tools, builds the argv and turns a CLI error
into the thrown sentence the model sees. Zero npm dependencies; `typebox` and
the pi packages come from pi's own install.

`timeout` is browserwright's **call deadline** in seconds (default 90), the same
knob as `browserwright -e --timeout`; when it runs out the call fails with
`DeadlineExceeded` (exit 7).

The names are `bw_`-prefixed rather than bare `web_fetch`/`web_search` because
providers reserve generic tool names: grok rejects a custom function named
`web_search` with a 400.

## Install

```bash
pi install npm:@browserwright/pi
```

Requires a `browserwright` CLI on `PATH` from the same release (it needs the
`search` command) and its daemon running:

```bash
uv tool install browserwright
browserwright version check      # expect drift=equal
```

## Which browser

One browser per setup, **no fallback** (ADR-0015):

- `BW_REMOTE_CDP` unset → your own Chrome (extension backend). Sees what you
  see, including pages behind your logins; opens a tab group while it works.
- `BW_REMOTE_CDP` set → only that CDP endpoint, passed as `--attach` (e.g. a
  CloakBrowser on another host). An `http(s)://` value is resolved through its
  `/json/version`; `ws(s)://` is used as-is. The browser is borrowed and left
  running. No login state.

```bash
export BW_REMOTE_CDP=https://cdp-host.example.ts.net
```

The variable is read when pi starts; restart pi after changing it. If the
chosen browser fails, the call fails and says why — it never quietly answers
from a different browser.

## The two tools

`bw_web_search` returns **links, never page bodies**. The model then calls
`bw_web_fetch` on the one or two worth reading.

### What `bw_web_search` returns

| field | contents |
|-------|----------|
| `results[]` | `position`, `title`, `url`, `snippet`, `date` |
| `answerBox` | Google's AI Overview, labelled in the output as generated and unsourced |
| `knowledgeGraph` | `title`, `subtitle`, `description`, `attributes` |
| `peopleAlsoAsk[]` | the expandable questions (capped at 10) |
| `relatedSearches[]` | the queries at the foot of the page (capped at 10) |

Everything except `results` is absent on most queries and is omitted entirely
rather than rendered empty.

### What it does not return

Measured against what Serper and SerpApi expose, so you know when to reach for
one of those instead. Nothing here is blocked by the architecture — the SERP
carries all of it — these are simply extractors nobody has needed yet.

**Not implemented (a selector away):**

| missing | why it hasn't been done |
|---------|------------------------|
| `sitelinks` | only render for brand-shaped queries; the two probes that looked for them found none, so there was nothing to write a selector against |
| `topStories` / `images` / `videos` | vertical carousels. An agent that wants news or images is better served asking for them explicitly than having them folded into every search |
| `places` / local pack | needs a location the daemon does not have; results would silently reflect the user's IP |
| `shopping` | product rows, ratings, prices — a different consumer than "find me a source" |
| ads | deliberately not extracted. They are the one part of the page that is *paid to look like* a result |
| spelling correction / "Did you mean" | cheap to add, nobody has asked |
| `searchInformation` | total-result count and timing. Google's own totals are estimates, so the field would be precise-looking and wrong |
| answer-box source URL | we take the AI Overview text but not its citation links |

**Structural, not a missing extractor:**

- **No pagination.** One page per call, `num` results. There is no offset
  parameter and adding one means another 10-20s round trip per page.
- **`position` is not the engine's rank.** It is the index after rows without a
  URL are dropped, so a dropped row shifts everything below it. Fine for finding
  sources; wrong for rank tracking — use a hosted API if you need true ranks.
- **Results are personalised.** They come through a real browser, its IP and
  (for your own Chrome) your login state, so region, language and search history
  all affect them. Results are **not reproducible** and are unsuitable as an
  objective baseline.
- **One engine.** The extractors are written against Google's DOM.
- **No usage or quota metadata**, because there is no account behind it.

`bw_web_fetch` returns the CLI's Markdown under a one-line header (the URL).
Past 50,000 characters it is cut on a line boundary; the header then names the
file holding the full text. Non-HTML (PDF, images) is refused with its
Content-Type.

## Errors are thrown, not returned

`AgentToolResult` has no `isError` field. pi's agent loop hardcodes
`isError: false` on the normal return path and only sets it in the `catch`
around `execute`, so a failure here throws — with the CLI's own `type: message`
from its JSON error envelope (e.g. `Captcha: captcha (unusual traffic) at …`).

## Tests

```bash
node --test '*.test.ts'    # the CLI contract, against a stub browserwright on PATH
```

Needs Node >= 23.6 for unflagged type stripping. The CLI side of the contract
is covered by browserwright's real-Chrome e2e tests for `search` and `markdown`.

## License

[AGPL-3.0-only](LICENSE), the same as browserwright itself — copyleft including
network service use.
