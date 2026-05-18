"""Today's top products on Product Hunt."""
from browser_skill import *  # noqa: F401, F403

ARGS = {
    "limit": {"type": "int", "required": False, "default": 20},
}

OUTPUT = "list[{name: str, url: str, tagline: str, votes: int}]"
TAGS = ["producthunt", "feed", "launches"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 10
LAST_VERIFIED = "2026-05-18"


def selftest():
    new_tab("https://www.producthunt.com/")
    if not wait_for_load(timeout=15):
        return False
    info = page_info()
    assert "producthunt" in info["url"], f"unexpected url: {info['url']}"
    return True


def run(args, ctx=None):
    limit = int(args.get("limit", 20))
    new_tab("https://www.producthunt.com/")
    wait_for_load(timeout=20)
    products = js(
        """
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
          // Look for a numeric span near the card — usually the vote count.
          let votes = 0;
          const numberEl = card.querySelector('[data-test="vote-button"], button');
          if (numberEl) {
            const m = (numberEl.innerText || '').replace(/,/g, '').match(/\\d+/);
            if (m) votes = parseInt(m[0], 10);
          }
          // Tagline is typically the second text-bearing element in the card.
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
        """
    )
    return (products or [])[:limit]
