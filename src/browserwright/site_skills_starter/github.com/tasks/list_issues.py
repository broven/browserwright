"""List public GitHub issues for owner/repo without login.

Phase C surface: drives the injected Playwright ``page`` (reused, navigated in
place); reads the issue list with ``page.evaluate``.
"""

ARGS = {
    "owner": {"type": "str", "required": True, "desc": "github owner / org name"},
    "repo": {"type": "str", "required": True, "desc": "repo name"},
    "state": {"type": "str", "required": False, "default": "open",
              "desc": "open / closed / all"},
    "limit": {"type": "int", "required": False, "default": 20},
}

OUTPUT = "list[{number: int, title: str, url: str, state: str}]"
TAGS = ["github", "issues", "list"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 10
LAST_VERIFIED = "2026-05-25"


def selftest():
    page.goto("https://github.com/", wait_until="load")
    return page.url.startswith("https://github.com")


def run(args, ctx=None):
    pg = ctx.page if ctx is not None and getattr(ctx, "page", None) else page

    owner, repo = args["owner"], args["repo"]
    state = args.get("state", "open")
    limit = int(args.get("limit", 20))
    url = f"https://github.com/{owner}/{repo}/issues?q=is%3Aissue+state%3A{state}"
    pg.goto(url, wait_until="load")
    results = pg.evaluate(
        """
        (limit) => {
          const cards = Array.from(document.querySelectorAll(
            '[data-testid="issue-pr-title-link"], a.js-navigation-open'
          )).filter(a => /\\/issues\\/\\d+$/.test(a.getAttribute('href') || ''));
          return cards.slice(0, limit).map(a => {
            const m = a.getAttribute('href').match(/\\/issues\\/(\\d+)$/);
            const stateBadge = a.closest('li, div')?.querySelector('[data-testid="issue-state"]');
            return {
              number: m ? parseInt(m[1], 10) : null,
              title: a.innerText.trim(),
              url: new URL(a.getAttribute('href'), location.origin).toString(),
              state: stateBadge ? stateBadge.innerText.trim().toLowerCase() : '',
            };
          });
        }
        """,
        limit,
    )
    return results or []
