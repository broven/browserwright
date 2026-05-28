"""Search Google for `query` and return the top N organic results.

Phase C surface: drives the injected Playwright ``page`` (reused, navigated in
place) and reads results via ``page.evaluate``.
"""

ARGS = {
    "query": {"type": "str", "required": True, "desc": "search query"},
    "limit": {"type": "int", "required": False, "default": 10},
    "hl": {"type": "str", "required": False, "default": "en", "desc": "interface language"},
}

OUTPUT = "list[{title: str, url: str, snippet: str}]"
TAGS = ["search", "general"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 8
LAST_VERIFIED = "2026-05-25"


def selftest():
    page.goto("https://www.google.com/?hl=en")
    return "google.com" in page.url


def run(args, ctx=None):
    from urllib.parse import quote_plus

    pg = ctx.page if ctx is not None and getattr(ctx, "page", None) else page

    q = args["query"]
    limit = int(args.get("limit", 10))
    hl = args.get("hl", "en")
    pg.goto(f"https://www.google.com/search?q={quote_plus(q)}&hl={hl}")
    results = pg.evaluate(
        """
        (limit) => Array.from(document.querySelectorAll('div.g, div[data-hveid]'))
          .map(div => {
            const a = div.querySelector('a[href^="http"]');
            const h = div.querySelector('h3');
            const sn = div.querySelector('div[data-content-feature="1"], div[role="link"] + div');
            if (!a || !h) return null;
            return {
              title: h.innerText.trim(),
              url: a.href,
              snippet: (sn && sn.innerText.trim()) || '',
            };
          })
          .filter(Boolean)
          .slice(0, limit)
        """,
        limit,
    )
    return results or []
