# @browserwright/pi

Two tools for [pi](https://github.com/badlogic/pi-mono), backed by **declarative
providers** that drive [browserwright](https://github.com/broven/browserwright):

```
bw_web_fetch(url, provider?, timeout?)   → the page as Markdown, or raw text for text endpoints
bw_web_search(query, provider?, timeout?) → ranked links + the SERP features Google showed
```

`timeout` is browserwright's **call deadline** in seconds (default 90), the same
knob as `browserwright -e --timeout`. It is forwarded to the browserwright rung as
`--timeout`; when it runs out that rung fails with `DeadlineExceeded` (exit 7)
instead of being retried. The rung's own process budget is widened to outlast it.

The browserwright paths run through the user's **own Chrome**, so they see what
the user sees — including pages behind a login. Fetch also has a raw-text
fallback for endpoints such as GitHub Raw; that fallback makes a direct request
and returns the text body verbatim. Zero npm dependencies; `typebox` and the pi
packages come from pi's own install.

## Install

```bash
pi install npm:@browserwright/pi
```

Requires the `browserwright` CLI (>= 0.9.0) on `PATH` and its daemon running:

```bash
uv tool install browserwright
browserwright-daemon serve
browserwright version check      # expect drift=equal
```

The npm package and the Python package are cut from the same git tag, so their
versions always match. Install them together.

The names are `bw_`-prefixed rather than bare `web_fetch`/`web_search` because
providers reserve generic tool names: grok rejects a custom function named
`web_search` with a 400, so the prefix keeps every provider safe.

## The two tools

`bw_web_search` returns **links, never page bodies**. The model then calls
`bw_web_fetch` on the one or two worth reading. That split is deliberate: fetching
all ten hits costs ~30 seconds and 50KB+ of context to answer a question that
usually needs one of them.

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
- **Results are personalised.** They come through the user's own browser, IP and
  login state, so region, language and search history all affect them. That is
  the point of this rung, but it also means results are **not reproducible** and
  are unsuitable as an objective baseline.
- **One engine at a time.** `searchUrl` in the provider declaration is a
  template, so pointing it at another engine is a config edit — but the
  extractors are written against Google's DOM and will not transfer as-is.
- **No usage or quota metadata**, because there is no account behind it.

If a missing field matters more than login state does, the answer is usually to
drop a hosted-API provider JSON into `providers/` and put it ahead of this rung,
not to extend the extractor. `normalizeSearchPayload` already maps Serper's
response field-for-field.

The response header tells the model what it got, because pi's tool `details`
field never reaches the LLM — it only feeds the TUI renderer:

```
# Example Domain
https://example.com
provider=browserwright · format=markdown · 385B
chain: browserwright✗1.1s browserwright-search✓2.8s   ← only when >1 rung ran
truncated: 382 of 480 lines (49.7KB of 71.5KB) · full: /tmp/browserwright-pi-xxx.txt
```

## What ships, and what does not

This package ships the browserwright-backed rungs plus a text fallback for fetch.
Each tool has one local browserwright rung, and one optional remote rung that
stays inert until `BW_REMOTE_CDP` is set (see [Remote browser](#remote-browser-bw_remote_cdp)):

| tool | provider | kind |
|------|----------|------|
| `bw_web_fetch` | `browserwright` → `browserwright-remote` → `raw` | `command` — browser-rendered HTML (your Chrome, then the remote browser), then `module` — text body verbatim |
| `bw_web_search` | `browserwright-search` → `browserwright-search-remote` | `module` — a session lifecycle in TS |

That is a real trade-off, and it points the wrong way for casual HTML fetches: a
browserwright `bw_web_fetch` opens a tab in the daily browser and takes ~4-7s,
where a hosted reader API answers in ~1s without touching Chrome. What you get
for the browser rung is login state and full JS rendering. Text endpoints such
as GitHub Raw skip the browser conversion failure and are returned verbatim by
the `raw` fallback.

### Remote browser (`BW_REMOTE_CDP`)

Set `BW_REMOTE_CDP` to a CDP endpoint and both tools gain a fallback rung that
runs in that browser instead of yours — for example a CloakBrowser instance on
another host:

```bash
export BW_REMOTE_CDP=https://cdp-host.example.ts.net   # or ws(s)://…/devtools/browser/…
```

An `http(s)://` value is resolved through its `/json/version`; a `ws(s)://` one
is used as-is. It is passed to `browserwright markdown --attach=…` (fetch) and
`browserwright session new --backend=cdp --attach=…` (search). The executor and
daemon stay local; only the browser is remote, and it is borrowed — left
running when the throwaway session ends. Needs a browserwright CLI with
`markdown --attach` (newer than 0.20.0).

- **Unset means absent.** The rungs report `missing env BW_REMOTE_CDP` and the
  chain moves on without spawning anything.
- **Fallback by default.** They run only after the local rung failed. To make
  the remote browser primary, put it first in `config.json`:
  `"fetch": ["browserwright-remote", "browserwright", "raw"]`,
  `"search": ["browserwright-search-remote", "browserwright-search"]`.
  Keep `browserwright-remote` ahead of `raw`: raw accepts `text/html` verbatim,
  so any browser rung after it never runs for an HTML page.
- **No login state.** It is not your browser profile, so pages behind your
  logins come back logged out.
- **Slow on purpose.** Both rungs allow 240s, because an endpoint at its
  concurrency limit may queue the connection for minutes and the browser starts
  lazily on first connect. The daemon's own upstream connect timeout is
  separate and short (default 5s): raise it via `BD_TIMEOUT` in the daemon's
  environment, or `timeout` in its config, if attaches fail fast while the
  endpoint is queueing.

**The chain engine is still here.** Drop your own JSON into `providers/` to add a
cheaper or anonymous rung ahead of the browser one — nothing needs to be
registered, and a provider missing from `config.json`'s `order` is appended
rather than ignored.

## Adding a provider

### kind: "http"

```json
{
  "name": "example",
  "role": "fetch",
  "kind": "http",
  "method": "POST",
  "url": "https://api.example.com/read",
  "headers": { "Authorization": "Bearer $EXAMPLE_TOKEN" },
  "body": { "url": "{url}" },
  "pick": "result",
  "returns": "markdown"
}
```

- `role` is `fetch` (the default) or `search`. It decides which tool can reach
  the provider, and which tokens it may use: `{url}`/`{urlEncoded}` for fetch,
  `{query}`/`{queryEncoded}` for search. `{dir}` is available to both, and so
  is `{timeout}`: the caller's call deadline in seconds (90 when unset), for a
  command that forwards it as browserwright's `--timeout`.
- `$ENV_VAR` is substituted in the declaration first, then the tokens — so a
  `$NAME` inside the requested URL or query is never read as an env reference.
- A referenced env var that is unset makes the rung **skip** with
  `missing env NAME` rather than sending the literal `$NAME`. A literal value
  passes through untouched — but prefer `$ENV` for anything secret, since these
  files are meant to be shareable.
- `pick` is a dot path into a JSON response. For a `search` provider it must
  land on an **array** of organic rows; they are coerced from whatever field
  names the API uses (`link`/`url`/`href`, `snippet`/`description`/`content`, …).
  The SERP-feature fields are read from the top level of the same body by their
  usual names (`answerBox`/`answer_box`, `knowledgeGraph`, `peopleAlsoAsk`,
  `relatedSearches`, …), so a hosted search API is a pure JSON drop-in — omit
  `pick` entirely and the whole response is mapped for you.

### kind: "command"

```json
{
  "name": "example",
  "kind": "command",
  "command": ["{dir}/providers/example.sh", "{url}"],
  "returns": "html"
}
```

`command` and `cwd` support the same tokens and `$ENV_VAR` as the http kind,
including the `missing env NAME` skip — that is how `browserwright-remote`
stays inert until `BW_REMOTE_CDP` is set.

Exit code contract — this is what lets a shell script participate without the
core knowing anything about the tool it wraps:

| exit | meaning |
|------|---------|
| `0` | success, stdout is the content |
| `2` | not applicable — drop to the next rung, not an error |
| other | hard error — also drops a rung, reported as an error |

The last line of stderr becomes the reason in the chain trace, so make it a
sentence.

### kind: "module"

The escape hatch for a provider that needs real logic — a multi-step lifecycle,
its own retries, progress reporting. It gets the event loop instead of one
process:

```json
{ "name": "example", "kind": "module", "module": "./providers/example.ts", "returns": "results" }
```

The module default-exports `(subject, ctx) => Promise<ProviderOutcome<T>>`.
`ctx` carries `dir`, `timeoutMs`, `callTimeoutS` (the caller's call deadline
in seconds, or undefined for browserwright's 90s default — forward it as
`--timeout`), `signal`, `options` (verbatim from the declaration) and
`onProgress`. Cancellation is cooperative: there is no process
to kill, so the runner must unwind its own resources when `ctx.signal` fires.

`providers/browserwright-search.ts` is the worked example. Its `options`:
`limit`, `searchUrl` (a template), and `sessionArgs` — the argv after
`browserwright session new`, default `["--backend=extension",
"--name=pi-websearch"]`. Each `sessionArgs` element supports `$ENV_VAR` with the
same `missing env NAME` skip, which is what `browserwright-search-remote.json`
uses to point the same runner at `--backend=cdp --attach=$BW_REMOTE_CDP`. Its header documents
the six measured executor behaviours it is built around, and its declaration
records why each SERP extractor anchors where it does — including the finding
that Google's AI Overview body is **not** in the server-rendered HTML at all, so
extraction has to run against the live DOM rather than the document response.

### `returns`

`markdown` | `html` | `text` | `results`. **The core never converts between
them**; it only labels the output so the model knows what it is reading. The
built-in `raw` fetch provider returns accepted text response bodies unchanged.

## failWhen: the reason the chain exists

The common real-world failure is not an error. It is **HTTP 200 with a JS shell,
a cookie wall, or a login page** — and for search, **a perfectly parsed empty
list**. Without content-level rejection the first rung "succeeds", the model gets
garbage, and later rungs never run.

Two layers: the core default in `config.json` → `defaultFailWhen`, and
`failWhen` per provider. Per-provider values **replace** the default field by
field; they do not merge. That is what makes `"matches": []` a working opt-out,
which the browser rung relies on — phrases like "enable JavaScript" appear
legitimately inside raw HTML and inside search results *about* JavaScript.

| field | applies to | note |
|-------|-----------|------|
| `minChars` | text payloads only | default 0 (off) |
| `minResults` | list payloads only | an empty list is rejected regardless, unless the engine asserted it |
| `matches` | both | searched in the text, or in joined titles + snippets |

The one exception is an **asserted** empty. When a search provider reports
`noMatch: true` — Google says "did not match any documents" in prose, on an
ordinary HTTP 200 page — the empty list is the engine's answer and is accepted,
`minResults` included. Everything else empty is still rejected, because that is
what a consent wall or a captcha looks like from here. Without the distinction a
query the engine answered correctly reaches the model as `bw_web_search failed`,
which reads as broken tooling rather than as a query worth rewriting.

`minChars` is deliberately not applied to a list, and `minResults` not to text:
the two floors measure different things, and applying both would reject a short
but complete set of hits.

`minChars` defaults to **0 (off)**. A false positive here fails the whole call,
because there is only one rung — so the bar for rejecting is deliberately high.

## retries: for what is genuinely transient

```json
{ "retries": 1, "retryWhen": ["PageBindTimeout", "retryable"] }
```

Retries apply to **transport failures only**. Content rejected by `failWhen` is
never retried: that verdict is deterministic, so a second identical call only
costs time and, for a browser rung, another tab.

`retryWhen` scopes it. Measured 2026-08-09: browserwright intermittently fails
with `PageBindTimeout` on a healthy daemon and marks it `retryable: true`
itself. With one rung per tool there is nothing to fall through to, so that one
retry is the difference between a blip and a failed call — while a 404 is not
worth repeating.

## Probe: rules from evidence, not guesses

```
/bw list             # show both chains
/bw probe            # every fetch provider
/bw probe browserwright
```

The slash command is `/bw` (not `/browserwright`) so it cannot be confused with
pi's `browserwright` skill, which pi exposes as `/skill:browserwright`.

Runs a provider against the real URLs in `probe-cases.json` — a normal article,
a client-rendered shell, a bot wall, a login wall, a 404, a page past the
truncation limit, a PDF, and localhost — then prints what came back and writes
`providers/<name>.probe.json`.

Constraints:

- **Manual only.** It hits real sites and opens tabs in the user's browser. It
  asks for confirmation first.
- **Fetch providers only** — the cases are URLs.
- **Evidence files store summaries, never whole pages.** Real pages can carry
  the user's logged-in content.
- Results drift as sites change, so every evidence file is timestamped.
- When installed from npm the package lives under `node_modules`, so evidence
  falls back to the temp dir rather than being lost.

## Tests

```bash
node --test 'core/*.test.ts'    # 105 cases, no network, no browser
node verify.ts                  # real fetch chain against a real URL
node verify.ts --search "…"     # real search chain, opens a tab
```

The unit tests need Node >= 23.6 for unflagged TypeScript type stripping. The
package itself has no such floor — pi loads it through jiti.

The executor is injected throughout `core/`, which is why nothing there touches
the network. That is also where the tests are, because that is the code which
fails **silently**: a rung never tried, a JS shell accepted as success, or an
empty result list returned as an answer produce no error — just quietly worse
answers.

## Errors are thrown, not returned

`AgentToolResult` has no `isError` field. pi's agent loop hardcodes
`isError: false` on the normal return path and only sets it in the `catch`
around `execute`. A tool that returns `{isError: true}` therefore records a
failed call as a **successful** one: the TUI does not mark it, and observers of
the `tool_result` event see `isError: false`. So a chain failure here throws.

## Deliberately not built

- **No cache.** Overflow past 50KB goes to a temp file whose path the model
  gets; it then uses `read` and `grep`, which beat any pagination parameter.
- **No site route table.** A route that sends a host straight to a heavy rung
  destroys the evidence that would later invalidate it. The waste is exposed in
  `chain:` instead — add a route when it actually annoys you.
- **No SSRF guard.** This is a local CLI, not a server: there is no external
  attacker, and blocking private addresses would remove localhost fetching,
  which is a real workflow. The requested URL is printed so internal fetches
  stay visible in the transcript.
- **No body fetching inside `bw_web_search`.** See the two-tool split above.

## License

[AGPL-3.0-only](LICENSE), the same as browserwright itself — copyleft including
network service use.
