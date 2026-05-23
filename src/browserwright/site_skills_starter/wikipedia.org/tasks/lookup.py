"""Lookup a Wikipedia article and return its first-paragraph summary + section TOC."""
from browserwright import *  # noqa: F401, F403

ARGS = {
    "title": {"type": "str", "required": True, "desc": "article title (free text)"},
    "lang": {"type": "str", "required": False, "default": "en",
             "desc": "wikipedia language subdomain (en/zh/ja/...)"},
}

OUTPUT = "{title: str, url: str, summary: str, sections: list[str]}"
TAGS = ["wikipedia", "lookup", "reference"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 6
LAST_VERIFIED = "2026-05-18"


def selftest():
    new_tab("https://en.wikipedia.org/wiki/Wikipedia")
    if not wait_for_load(timeout=10):
        return False
    return "Wikipedia" in page_info()["title"]


def run(args, ctx=None):
    from urllib.parse import quote

    title = args["title"]
    lang = args.get("lang", "en")
    underscore_title = title.strip().replace(" ", "_")
    url = f"https://{lang}.wikipedia.org/wiki/{quote(underscore_title)}"
    new_tab(url)
    wait_for_load(timeout=15)
    info = js(
        """
        const summary_p = document.querySelector(
          'div.mw-parser-output > p:not(.mw-empty-elt)'
        );
        const sections = Array.from(document.querySelectorAll('h2 > span.mw-headline'))
          .map(el => el.innerText.trim());
        return {
          title: document.title.replace(' - Wikipedia', '').trim(),
          url: location.href,
          summary: summary_p ? summary_p.innerText.trim() : '',
          sections,
        };
        """
    )
    return info or {"title": title, "url": url, "summary": "", "sections": []}
