"""List public GitHub issues for owner/repo without login."""
from browser_skill import *  # noqa: F401, F403

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
LAST_VERIFIED = "2026-05-18"


def selftest():
    new_tab("https://github.com/")
    if not wait_for_load(timeout=10):
        return False
    info = page_info()
    assert info["url"].startswith("https://github.com"), f"unexpected url: {info['url']}"
    return True


def run(args, ctx=None):
    owner, repo = args["owner"], args["repo"]
    state = args.get("state", "open")
    limit = int(args.get("limit", 20))
    url = f"https://github.com/{owner}/{repo}/issues?q=is%3Aissue+state%3A{state}"
    new_tab(url)
    wait_for_load(timeout=15)
    results = js(
        """
        const cards = Array.from(document.querySelectorAll(
          '[data-testid="issue-pr-title-link"], a.js-navigation-open'
        )).filter(a => /\/issues\\/\\d+$/.test(a.getAttribute('href') || ''));
        return cards.slice(0, %d).map(a => {
          const m = a.getAttribute('href').match(/\\/issues\\/(\\d+)$/);
          const stateBadge = a.closest('li, div')?.querySelector('[data-testid="issue-state"]');
          return {
            number: m ? parseInt(m[1], 10) : null,
            title: a.innerText.trim(),
            url: new URL(a.getAttribute('href'), location.origin).toString(),
            state: stateBadge ? stateBadge.innerText.trim().toLowerCase() : '',
          };
        });
        """
        % limit
    )
    return results or []
