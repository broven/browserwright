"""Lookup a Wikipedia article and return its first-paragraph summary + section TOC.

Phase C surface: drives the injected Playwright ``page`` (bound to the session's
current tab — reused, navigated in place). No ``new_tab`` / ``js`` primitives.
"""

ARGS = {
    "title": {"type": "str", "required": True, "desc": "article title (free text)"},
    "lang": {"type": "str", "required": False, "default": "en",
             "desc": "wikipedia language subdomain (en/zh/ja/...)"},
}

OUTPUT = "{title: str, url: str, summary: str, sections: list[str]}"
TAGS = ["wikipedia", "lookup", "reference"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 6
LAST_VERIFIED = "2026-05-25"


def selftest():
    page.goto("https://en.wikipedia.org/wiki/Wikipedia")
    return "Wikipedia" in page.title()


def run(args, ctx=None):
    from urllib.parse import quote

    # `page` / `context` / `snapshot` are injected by the task runner (Phase C),
    # mirroring the heredoc namespace. Prefer ctx if a caller passed one.
    pg = ctx.page if ctx is not None and getattr(ctx, "page", None) else page

    title = args["title"]
    lang = args.get("lang", "en")
    underscore_title = title.strip().replace(" ", "_")
    url = f"https://{lang}.wikipedia.org/wiki/{quote(underscore_title)}"
    pg.goto(url)
    info = pg.evaluate(
        """
        () => {
          const summary_p = document.querySelector(
            'div.mw-parser-output > p:not(.mw-empty-elt)'
          );
          const sections = Array.from(
            document.querySelectorAll('h2 > span.mw-headline, h2 .mw-headline')
          ).map(el => el.innerText.trim());
          return {
            title: document.title.replace(' - Wikipedia', '').trim(),
            url: location.href,
            summary: summary_p ? summary_p.innerText.trim() : '',
            sections,
          };
        }
        """
    )
    return info or {"title": title, "url": url, "summary": "", "sections": []}
