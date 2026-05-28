"""Today's top products on Product Hunt.

Phase C surface: drives the injected Playwright ``page`` (reused, navigated in
place); scrapes the feed with ``page.evaluate``.
"""

ARGS = {
    "limit": {"type": "int", "required": False, "default": 20},
}

OUTPUT = "list[{name: str, url: str, tagline: str, votes: int}]"
TAGS = ["producthunt", "feed", "launches"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 10
LAST_VERIFIED = "2026-05-25"


def selftest():
    page.goto("https://www.producthunt.com/")
    return "producthunt" in page.url


def run(args, ctx=None):
    pg = ctx.page if ctx is not None and getattr(ctx, "page", None) else page

    limit = int(args.get("limit", 20))
    pg.goto("https://www.producthunt.com/")
    products = pg.evaluate(
        """
        () => {
          // Hand-rolled scrape: walk every /posts/<slug> anchor on the page
          // and grab a sibling vote count if we can find one.
          const seen = new Set();
          const out = [];
          const anchors = document.querySelectorAll('a[href^="/posts/"]');
          anchors.forEach(a => {
            const slug = a.getAttribute('href').replace(/^\\/posts\\//, '').split(/[?#]/)[0];
            if (!slug || seen.has(slug)) return;
            const card = a.closest('[data-test^="post-item"], li, article, div');
            if (!card) return;
            const name = a.innerText.trim();
            if (!name) return;
            let votes = 0;
            const numberEl = card.querySelector('[data-test="vote-button"], button');
            if (numberEl) {
              const m = (numberEl.innerText || '').replace(/,/g, '').match(/\\d+/);
              if (m) votes = parseInt(m[0], 10);
            }
            let tagline = '';
            const para = card.querySelector('p, [class*="tagline"], [class*="description"]');
            if (para && para.innerText) tagline = para.innerText.trim();
            out.push({
              name,
              url: 'https://www.producthunt.com/posts/' + slug,
              tagline,
              votes,
            });
            seen.add(slug);
          });
          return out;
        }
        """
    )
    return (products or [])[:limit]
