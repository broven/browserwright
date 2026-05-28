"""Pull top stories from Hacker News (top/new/best).

Phase C surface: drives the injected Playwright ``page`` (reused, navigated in
place); reads the story list with ``page.evaluate``.
"""

ARGS = {
    "kind": {"type": "str", "required": False, "default": "top",
             "desc": "top / new / best"},
    "limit": {"type": "int", "required": False, "default": 30},
}

OUTPUT = "list[{rank: int, id: int, title: str, url: str, score: int, comments: int}]"
TAGS = ["hn", "news", "feed"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 5
LAST_VERIFIED = "2026-05-25"

_KINDS = {
    "top": "https://news.ycombinator.com/",
    "new": "https://news.ycombinator.com/newest",
    "best": "https://news.ycombinator.com/best",
}


def selftest():
    page.goto("https://news.ycombinator.com/")
    return "ycombinator.com" in page.url


def run(args, ctx=None):
    pg = ctx.page if ctx is not None and getattr(ctx, "page", None) else page

    kind = args.get("kind", "top")
    limit = int(args.get("limit", 30))
    url = _KINDS.get(kind, _KINDS["top"])
    pg.goto(url)
    rows = pg.evaluate(
        """
        () => {
          const out = [];
          const stories = document.querySelectorAll('tr.athing');
          stories.forEach((row, i) => {
            const a = row.querySelector('span.titleline > a');
            if (!a) return;
            const sub = row.nextElementSibling;
            const score = sub ? sub.querySelector('span.score') : null;
            const links = sub ? sub.querySelectorAll('a') : [];
            const commentLink = links.length ? links[links.length - 1] : null;
            out.push({
              rank: i + 1,
              id: parseInt(row.id, 10),
              title: a.innerText.trim(),
              url: a.href,
              score: score ? parseInt(score.innerText, 10) : 0,
              comments: commentLink ? parseInt(commentLink.innerText, 10) || 0 : 0,
            });
          });
          return out;
        }
        """
    )
    return (rows or [])[:limit]
