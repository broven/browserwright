"""Skill exception hierarchy (spec §A.4)."""


class BrowserSkillError(Exception):
    """Root of every exception Skill itself raises."""

    exit_code = 3  # default: script raised


class PageLoadFailed(BrowserSkillError):
    exit_code = 3

    def __init__(self, url: str = "", reason: str = ""):
        self.url, self.reason = url, reason
        super().__init__(f"page load failed: {url} ({reason})")


class ElementNotFound(BrowserSkillError):
    exit_code = 3

    def __init__(self, selector: str = "", timeout: float = 0.0):
        self.selector, self.timeout = selector, timeout
        super().__init__(f"element not found: {selector!r} after {timeout}s")


class AuthWall(BrowserSkillError):
    exit_code = 4

    def __init__(self, url: str = "", signals=None):
        self.url, self.signals = url, list(signals or [])
        super().__init__(f"auth wall at {url}: {self.signals}")


class Captcha(BrowserSkillError):
    exit_code = 5

    def __init__(self, kind: str = "unknown", url: str = ""):
        self.kind, self.url = kind, url
        super().__init__(f"captcha ({kind}) at {url}")


class NetworkError(BrowserSkillError):
    exit_code = 3

    def __init__(self, url: str = "", status=None):
        self.url, self.status = url, status
        super().__init__(f"network error: {url} (status={status})")


class DaemonUnavailable(BrowserSkillError):
    exit_code = 2

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(f"daemon unavailable: {detail}")


class NoSession(BrowserSkillError):
    """No BD_SESSION provided. Refuse rather than silently sharing a browser (P1)."""

    exit_code = 2

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(
            "no session: run `browser-skill session new --backend <extension|rdp> ...` "
            "first, then pass --session <id> (or BD_SESSION=<id>) on every call. " + detail
        )


class DaemonBackendMismatch(BrowserSkillError):
    """Mode-B daemon is alive but serving a different backend than the
    one the caller asked for / configured (REVIEW.md F-5d).

    Surface case: a daemon was last started against ``extension`` under
    ``BD_NAME=foo``; the operator now wants ``rdp`` for that same name.
    Without an identity check, Skill would silently reuse the stale
    daemon and target the wrong Chrome. This error makes the mismatch
    loud + actionable.
    """

    exit_code = 2

    def __init__(self, requested: str = "", actual: str = "",
                 name: str = "default"):
        self.requested, self.actual, self.name = requested, actual, name
        super().__init__(
            f"daemon backend mismatch: BD_NAME={name!r} is serving "
            f"backend={actual!r} but you requested {requested!r}. "
            f"Either restart the daemon (`browser-daemon stop --name {name}` "
            f"then `browser-daemon serve --backend {requested}`) or pick "
            f"a different BD_NAME."
        )


class SiteDrift(BrowserSkillError):
    exit_code = 3

    def __init__(self, site: str = "", task: str = "", failed_check: str = ""):
        self.site, self.task, self.failed_check = site, task, failed_check
        super().__init__(f"site drift in {site}/{task}: {failed_check}")


class CDPError(BrowserSkillError):
    exit_code = 3

    def __init__(self, method: str = "", params=None, cdp_message: str = ""):
        self.method, self.params, self.cdp_message = method, dict(params or {}), cdp_message
        super().__init__(f"CDP {method} failed: {cdp_message}")


class NeedsUserConfirm(BrowserSkillError):
    """Raised by remember_preference / solidify when the agent must surface a
    confirm prompt to the user before re-calling with confirm=False."""

    exit_code = 1

    def __init__(self, what: str = "", proposal=None):
        self.what, self.proposal = what, proposal
        super().__init__(f"needs user confirm: {what}")


def serialize(exc: BaseException) -> dict:
    """Compact JSON-friendly representation for stderr / repl socket."""
    out = {"type": type(exc).__name__, "msg": str(exc)}
    for k in ("url", "selector", "timeout", "reason", "signals", "kind",
              "status", "detail", "site", "task", "failed_check",
              "method", "cdp_message", "what", "proposal"):
        v = getattr(exc, k, None)
        if v is not None and not isinstance(v, (type(None),)):
            try:
                import json as _json
                _json.dumps(v)  # ensure serializable
                out[k] = v
            except (TypeError, ValueError):
                out[k] = repr(v)
    return out
