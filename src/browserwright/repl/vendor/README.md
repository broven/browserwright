# Vendored third-party sources

## `readability.js`

- **Upstream:** [mozilla/readability](https://github.com/mozilla/readability)
- **Version:** 0.6.0
- **Commit:** `ab4027a8b37669745016869a37a504727992b2ba`
- **Vendored:** 2026-08-09
- **License:** Apache-2.0 (`Copyright (c) 2010 Arc90 Inc`) — the full notice is
  preserved verbatim at the top of the file. Apache-2.0 is one-way compatible
  with this project's AGPL-3.0-only license, so redistribution here is fine
  **as long as that header stays**. Do not strip or minify it.

**Unmodified.** The file is byte-identical to upstream. If it ever needs a
patch, record the diff here rather than editing silently — the whole point of a
recorded commit hash is that the next person can re-fetch and compare.

### Why vendored instead of a dependency

It is loaded into the page with `page.evaluate(<source text>)`, not
`page.add_script_tag`: under a strict CSP the latter raises while the former is
exempt (see `repl/_md_normalize.py` for the verification). That means we need
the source as a **string at runtime**, which rules out an npm dependency and
makes a pinned file the honest representation.

### What it is used for, and what it is not

It powers `mode="article"` on the `read_markdown()` content view. It is **not**
the default path: ADR-0007 records that every extractor measured — this one
included — dropped 100% of the links on application-shaped pages (a GitHub
issue page went 118 links → 0), so extraction is only adopted when it can be
shown not to have collapsed.

Two upstream behaviours the caller must respect:

- **It mutates the document it is given.** Always hand it a scratch document,
  never the live one.
- **It does not traverse shadow roots**
  ([#926](https://github.com/mozilla/readability/issues/926), open since
  2024-12), which is why our own normalization flattens them *before*
  Readability ever sees the document.

`isProbablyReaderable` from the same project is deliberately **not** vendored —
ADR-0007 explains why it cannot serve as the extraction gate.
