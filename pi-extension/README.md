# @browserwright/pi

Two tools for [pi](https://github.com/badlogic/pi-mono), backed by **declarative
providers** that drive [browserwright](https://github.com/broven/browserwright):

```
web_fetch(url, provider?)      → the page as Markdown
web_search(query, provider?)   → ranked links + the SERP features Google showed
```

Both run through the user's **own Chrome**, so they see what the user sees —
including pages behind a login. Zero npm dependencies; `typebox` and the pi
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

## The two tools

`web_search` returns **links, never page bodies**. The model then calls
`web_fetch` on the one or two worth reading. That split is deliberate: fetching
all ten hits costs ~30 seconds and 50KB+ of context to answer a question that
usually needs one of them.

### What `web_search` returns

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

This package ships **only the browserwright rungs**. There is one per tool:

| tool | provider | kind |
|------|----------|------|
| `web_fetch` | `browserwright` | `command` — `browserwright markdown <url>` |
| `web_search` | `browserwright-search` | `module` — a session lifecycle in TS |

That is a real trade-off, and it points the wrong way for casual fetches: every
`web_fetch` opens a tab in the daily browser and takes ~4-7s, where a hosted
reader API answers in ~1s without touching Chrome. What you get for it is login
state and full JS rendering, which no anonymous rung has.

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
  `{query}`/`{queryEncoded}` for search. `{dir}` is available to both.
- Tokens are substituted first, then `$ENV_VAR`.
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
`ctx` carries `dir`, `timeoutMs`, `signal`, `options` (verbatim from the
declaration) and `onProgress`. Cancellation is cooperative: there is no process
to kill, so the runner must unwind its own resources when `ctx.signal` fires.

`providers/browserwright-search.ts` is the worked example. Its header documents
the six measured executor behaviours it is built around, and its declaration
records why each SERP extractor anchors where it does — including the finding
that Google's AI Overview body is **not** in the server-rendered HTML at all, so
extraction has to run against the live DOM rather than the document response.

### `returns`

`markdown` | `html` | `text` | `results`. **The core never converts between
them**; it only labels the output so the model knows what it is reading.

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
| `minResults` | list payloads only | an empty list is rejected regardless |
| `matches` | both | searched in the text, or in joined titles + snippets |

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
/browserwright list             # show both chains
/browserwright probe            # every fetch provider
/browserwright probe browserwright
```

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
node --test 'core/*.test.ts'    # 83 cases, no network, no browser
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
- **No body fetching inside `web_search`.** See the two-tool split above.

## License

[AGPL-3.0-only](LICENSE), the same as browserwright itself — copyleft including
network service use.
