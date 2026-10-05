"""The search pipeline behind ``browserwright search <query>``.

One results page of a search engine, read off the live DOM of a real browser.
Moved here from the pi-extension (`providers/browserwright-search.ts`, which
used to ship this as an executor script) so every caller gets the same
extraction from one place, and a Google restyle is fixed once.

    page.goto(results URL)
      │  page.evaluate(_EXTRACT_JS)       organic rows + SERP features, each
      │                                   extractor guarded on its own
      ▼
    rows  ──►  /goto?url=<token> resolved by one non-following request
      ▼
    payload dict (JSON-safe)  ──►  render_results() for agents, or --json

Failures are raised, never returned as an empty list: a captcha or consent wall
parses perfectly and yields zero rows, which is indistinguishable from an honest
empty search unless the page is read for which of the two it was.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from ..errors import BrowserwrightError, Captcha, NetworkError

DEFAULT_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en&num={limit}"
DEFAULT_LIMIT = 10

# The engine's own answer can run long; the caller asked for links, not an essay.
ANSWER_BOX_CAP = 1200

# Runs in the page. Selectors live here rather than in a site skill because the
# command is the only consumer and a layout change must be one edit.
#
# Every extractor is independently guarded: Google restyles constantly, and the
# failure that matters is one changed container silently taking the organic
# rows down with it.
_EXTRACT_JS = r"""(query) => {
  const QUERY = query.toLowerCase();
  const attempt = (fn, fallback) => { try { return fn(); } catch (e) { return fallback; } };
  const clean = (s) => (s || '')
    .replace(/\s*\bRead more\s*$/i, '')
    .replace(/\s*\bShow more\s*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();

  // --- organic rows ------------------------------------------------------
  const results = attempt(() => {
    const rows = [];
    document.querySelectorAll('#search a h3').forEach((h3) => {
      const a = h3.closest('a');
      if (!a || !a.href) return;
      const block = h3.closest('div[data-hveid]');
      let snippet = '';
      if (block) {
        const sn = block.querySelector('div[data-sncf], div[style*="-webkit-line-clamp"]');
        snippet = sn ? sn.innerText : '';
        if (!snippet) {
          const long = (block.innerText || '').split('\n').filter((s) => s.length > 40);
          snippet = long[0] || '';
        }
      }
      // Google prefixes dated results with "Mar 5, 2025 — ". Promote that to a
      // field so freshness can be judged without parsing prose.
      let date = null;
      const m = snippet.match(/^([A-Z][a-z]{2} \d{1,2}, \d{4})\s*[—·\-]\s*/);
      if (m) { date = m[1]; snippet = snippet.slice(m[0].length); }
      rows.push({ title: clean(h3.innerText), url: a.href, snippet: clean(snippet).slice(0, 400), date });
    });
    return rows;
  }, []);

  // --- answer box / AI Overview -----------------------------------------
  // Class names are obfuscated and rotate, so anchor on the accessible heading
  // and walk up until the container actually holds the body.
  const answerBox = attempt(() => {
    const head = [...document.querySelectorAll('[role="heading"]')]
      .find((e) => (e.innerText || '').trim() === 'AI Overview');
    if (!head) return null;
    let node = head;
    for (let i = 0; i < 8 && node; i++) {
      const t = node.innerText || '';
      if (t.length >= 150) {
        const body = clean(t.replace(/^AI Overview\s*/, ''))
          .replace(/AI responses may include mistakes.*$/i, '')
          // innerText splices the citation chips into the prose ("Reddit +2").
          .replace(/\s\+\d+\b/g, '')
          .trim();
        return body ? { kind: 'ai-overview', text: body.slice(0, 4000) } : null;
      }
      node = node.parentElement;
    }
    return null;
  }, null);

  // --- knowledge panel ---------------------------------------------------
  // data-attrid is semantic markup rather than a styling class, which makes it
  // the one stable hook on this page.
  const knowledgeGraph = attempt(() => {
    const pick = (sel) => {
      const e = document.querySelector('[data-attrid="' + sel + '"]');
      return e ? clean(e.innerText).slice(0, 300) : undefined;
    };
    const title = pick('title');
    const subtitle = pick('subtitle');
    const description = pick('wa:/description') || pick('description');
    const attributes = {};
    document.querySelectorAll('[data-attrid]').forEach((e) => {
      const id = e.getAttribute('data-attrid') || '';
      // Only "kc:/…:label" ids carry labelled facts; the rest are layout.
      if (!/^kc:/.test(id)) return;
      const label = id.split(':').pop() || id;
      // innerText repeats the label above the value. Plain string ops, not a
      // constructed RegExp: the label is engine-supplied.
      const human = label.replace(/_/g, ' ').toLowerCase();
      let text = clean(e.innerText);
      if (text.toLowerCase().startsWith(human)) {
        text = text.slice(human.length).replace(/^[\s:\uff1a]+/, '');
      }
      if (!text || text.length > 160) return;
      if (!attributes[label]) attributes[label] = text;
    });
    if (!title && !description && Object.keys(attributes).length === 0) return null;
    const out = { title, subtitle, description };
    if (Object.keys(attributes).length) out.attributes = attributes;
    return out;
  }, null);

  // --- people also ask ---------------------------------------------------
  // data-q also carries the query itself on the search box; drop echoes.
  const peopleAlsoAsk = attempt(() => {
    const seen = new Set();
    const out = [];
    document.querySelectorAll('[data-q]').forEach((e) => {
      const q = clean(e.getAttribute('data-q'));
      if (!q || q.toLowerCase() === QUERY || seen.has(q.toLowerCase())) return;
      seen.add(q.toLowerCase());
      out.push(q);
    });
    return out.slice(0, 10);
  }, []);

  // --- explicit "nothing matched" ---------------------------------------
  // HTTP 200, no interstitial, zero rows: reading Google's own sentence is the
  // only way to tell this empty list from the one a consent wall produces.
  const noMatch = attempt(() => {
    const body = (document.body.innerText || '').toLowerCase();
    return ['did not match any documents', 'no results found for'].some((n) => body.includes(n));
  }, false);

  // --- related searches --------------------------------------------------
  // Scoped to #botstuff: the same href shape at the top is Google's tab bar.
  const relatedSearches = attempt(() => {
    const bot = document.querySelector('#botstuff');
    if (!bot) return [];
    const seen = new Set();
    const out = [];
    bot.querySelectorAll('a[href*="/search?"]').forEach((a) => {
      const t = clean(a.innerText);
      // Pagination shares this selector: bare page numbers plus nav labels.
      if (!t || /^\d+$/.test(t) || t.length < 3 || t.length > 80) return;
      if (/^(next|previous|prev|more results?)$/i.test(t)) return;
      if (t.toLowerCase() === QUERY || seen.has(t.toLowerCase())) return;
      seen.add(t.toLowerCase());
      out.push(t);
    });
    return out.slice(0, 10);
  }, []);

  return { results, answerBox, knowledgeGraph, peopleAlsoAsk, relatedSearches, noMatch };
}"""

_INTERSTITIALS = ("recaptcha", "unusual traffic", "detected unusual")


def build_search_url(template: str, query: str, limit: int) -> str:
    return template.replace("{query}", quote(query, safe="")).replace("{limit}", str(limit))


def _read(page: Any, query: str) -> tuple[str, dict]:
    """HTML + extraction, with one settle-and-retry.

    A search engine can bounce the page right after load (consent, region
    redirect), which destroys the execution context mid-evaluate.
    """
    for i in range(2):
        try:
            return page.content(), page.evaluate(_EXTRACT_JS, query)
        except Exception as e:  # noqa: BLE001 — only the one race is retried
            if "ontext was destroyed" not in str(e) or i == 1:
                raise
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(800)
    raise AssertionError("unreachable")


def _resolve_goto(page: Any, rows: list[dict]) -> None:
    """A signed-out profile (e.g. a remote CloakBrowser) gets opaque
    ``/goto?url=<token>`` hrefs with the target nowhere in the DOM. The redirect
    answers with a 302 to the real URL inside this same browser context. On any
    failure keep the goto link rather than drop the row."""
    for row in rows:
        if "/goto?" not in (row.get("url") or ""):
            continue
        try:
            r = page.request.get(row["url"], max_redirects=0, timeout=10000)
            loc = r.headers.get("location")
            if r.status in (301, 302, 303, 307, 308) and loc and loc.startswith("http"):
                row["url"] = loc
        except Exception:  # noqa: BLE001 — a goto link is still a usable answer
            pass


def search_page(page: Any, query: str, *, limit: int = DEFAULT_LIMIT,
                search_url: str = DEFAULT_SEARCH_URL) -> dict:
    """Navigate ``page`` to the results for ``query`` and read them.

    Returns a JSON-safe dict: ``query``, ``url`` (the results page),
    ``results`` (``position``/``title``/``url``/``snippet``/``date``),
    ``answerBox``, ``knowledgeGraph``, ``peopleAlsoAsk``, ``relatedSearches``,
    ``noMatch``. Zero results is only ever returned with ``noMatch`` true — the
    engine said so itself. Everything else that yields no rows raises.
    """
    url = build_search_url(search_url, query, limit)
    page.goto(url)
    html, data = _read(page, query)

    # Chrome serves its own error document through a *successful* navigation.
    if "neterror" in html and "error-code" in html:
        raise NetworkError(url, fix="the browser could not reach the search engine; check its connectivity")

    rows = (data.get("results") or [])[:limit]
    no_match = bool(data.get("noMatch"))
    if not rows and not no_match:
        low = html.lower()
        for needle in _INTERSTITIALS:
            if needle in low:
                raise Captcha(needle, page.url)
        if urlparse(page.url).hostname and urlparse(page.url).hostname.startswith("consent."):
            raise Captcha("consent wall", page.url,
                          fix="accept the search engine's consent page once in this browser profile, then retry")
        raise BrowserwrightError(
            f"no results could be read from {page.url} ({page.title()!r})",
            fix="the results page layout may have changed; update _EXTRACT_JS in browserwright/repl/search.py",
        )

    _resolve_goto(page, rows)
    for i, row in enumerate(rows, 1):
        row["position"] = i
    return {
        "query": query,
        "url": page.url,
        "results": rows,
        "answerBox": data.get("answerBox"),
        "knowledgeGraph": data.get("knowledgeGraph"),
        "peopleAlsoAsk": data.get("peopleAlsoAsk") or [],
        "relatedSearches": data.get("relatedSearches") or [],
        "noMatch": no_match and not rows,
    }


def _cap(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap].rstrip() + "… [truncated]"


def render_results(payload: dict) -> str:
    """The agent-facing text. Links plus whatever SERP features the query
    triggered — never page bodies: the caller decides which two of ten links
    are worth reading. Optional sections are omitted, not rendered empty."""
    query = payload.get("query", "")
    results = payload.get("results") or []
    out = [f"# {len(results)} results for {query!r}"]

    box = payload.get("answerBox")
    if box:
        label = "AI Overview" if box.get("kind") == "ai-overview" else "Featured snippet"
        # Attributed: it is the engine's claim, not a source the reader can open.
        out += ["", f"## {label} (generated by the search engine, unsourced)", _cap(box.get("text", ""), ANSWER_BOX_CAP)]

    graph = payload.get("knowledgeGraph")
    if graph:
        # Labelled, not run together: a reader once took "title — subtitle" plus
        # a bare description line as title and subtitle.
        lines = [f"{k}: {graph[src]}" for k, src in (("title", "title"), ("type", "subtitle"),
                                                      ("description", "description")) if graph.get(src)]
        lines += [f"{k}: {v}" for k, v in (graph.get("attributes") or {}).items()]
        if lines:
            out += ["", "## Knowledge panel", *lines]

    if not results:
        out += ["", "The search engine states that nothing matched this query. That is its answer, not a "
                    "failure — widen the query: drop a `site:` path, a quoted phrase, or the least "
                    "essential keywords."]
    else:
        out.append("")
        for row in results:
            out += [f"{row['position']}. {row['title']}", f"   {row['url']}"]
            tail = " · ".join(x for x in (row.get("date"), row.get("snippet")) if x)
            if tail:
                out.append(f"   {tail}")
            out.append("")

    if payload.get("peopleAlsoAsk"):
        out += ["## People also ask", *(f"- {q}" for q in payload["peopleAlsoAsk"]), ""]
    if payload.get("relatedSearches"):
        out += ["## Related searches", " · ".join(payload["relatedSearches"]), ""]
    return "\n".join(out).rstrip() + "\n"
