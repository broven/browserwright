"""Search Google for `query` and return the top N organic results."""
from browserwright import *  # noqa: F401, F403

ARGS = {
    "query": {"type": "str", "required": True, "desc": "search query"},
    "limit": {"type": "int", "required": False, "default": 10},
    "hl": {"type": "str", "required": False, "default": "en", "desc": "interface language"},
}

OUTPUT = "list[{title: str, url: str, snippet: str}]"
TAGS = ["search", "general"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 8
LAST_VERIFIED = "2026-05-18"


def selftest():
    new_tab("https://www.google.com/?hl=en")
    if not wait_for_load(timeout=10):
        return False
    info = page_info()
    assert "google.com" in info["url"], f"unexpected url after navigate: {info['url']}"
    return True


def run(args, ctx=None):
    from urllib.parse import quote_plus

    q = args["query"]
    limit = int(args.get("limit", 10))
    hl = args.get("hl", "en")
    new_tab(f"https://www.google.com/search?q={quote_plus(q)}&hl={hl}")
    wait_for_load(timeout=15)
    results = js(
        """
        return Array.from(document.querySelectorAll('div.g, div[data-hveid]'))
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
          .slice(0, %d);
        """
        % limit
    )
    return results or []
