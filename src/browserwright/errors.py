"""Skill exception hierarchy (spec §A.4)."""


class BrowserwrightError(Exception):
    """Root of every exception Skill itself raises.

    Every error can carry a ``fix`` — a short, concrete next-action string
    ("call X" / "run Y") so an agent reading the error has a recovery step,
    not just a raw transport/protocol message. This generalizes the pattern
    that ``NeedsUserConfirm.proposal`` established: errors are actionable.

    Subclasses MAY set a class-level ``default_fix`` so a bare ``raise`` is
    still actionable; an explicit ``fix=`` at the raise site overrides it.
    """

    exit_code = 3  # default: script raised
    default_fix = ""

    def __init__(self, *args, fix: str = ""):
        super().__init__(*args)
        # Explicit fix wins; otherwise fall back to the class default.
        self.fix = fix or type(self).default_fix
        if self.fix and args:
            # Surface the next-action in __str__ so an agent that only logs
            # the message still sees the recovery step.
            self.args = (f"{args[0]}  [fix: {self.fix}]",) + tuple(args[1:])


class PageLoadFailed(BrowserwrightError):
    exit_code = 3
    default_fix = (
        "retry with new_tab(url) then wait_for_load(); if it persists, "
        "check the URL and network with http_get(url)"
    )

    def __init__(self, url: str = "", reason: str = "", fix: str = ""):
        self.url, self.reason = url, reason
        super().__init__(f"page load failed: {url} ({reason})", fix=fix)


class ElementNotFound(BrowserwrightError):
    exit_code = 3
    default_fix = (
        "capture_screenshot() to confirm the element is visible, then "
        "click_at_xy(x, y); use snapshot() to list interactive elements"
    )

    def __init__(self, selector: str = "", timeout: float = 0.0, fix: str = ""):
        self.selector, self.timeout = selector, timeout
        super().__init__(f"element not found: {selector!r} after {timeout}s", fix=fix)


class AuthWall(BrowserwrightError):
    exit_code = 4
    default_fix = "stop and ask the user to log in; do not type credentials from a screenshot"

    def __init__(self, url: str = "", signals=None, fix: str = ""):
        self.url, self.signals = url, list(signals or [])
        super().__init__(f"auth wall at {url}: {self.signals}", fix=fix)


class Captcha(BrowserwrightError):
    exit_code = 5
    default_fix = "stop and ask the user to solve the captcha; do not attempt to bypass it"

    def __init__(self, kind: str = "unknown", url: str = "", fix: str = ""):
        self.kind, self.url = kind, url
        super().__init__(f"captcha ({kind}) at {url}", fix=fix)


class NetworkError(BrowserwrightError):
    exit_code = 3
    default_fix = "verify the URL and connectivity, then retry; check http_get(url) for static pages"

    def __init__(self, url: str = "", status=None, fix: str = ""):
        self.url, self.status = url, status
        super().__init__(f"network error: {url} (status={status})", fix=fix)


class DaemonUnavailable(BrowserwrightError):
    exit_code = 2
    default_fix = (
        "start the single global daemon: `browserwright-daemon serve` "
        "(or run `browserwright doctor` to see what is missing)"
    )

    def __init__(self, detail: str = "", fix: str = ""):
        self.detail = detail
        super().__init__(f"daemon unavailable: {detail}", fix=fix)


class NoSession(BrowserwrightError):
    """No explicit session provided. Refuse rather than silently sharing a browser."""

    exit_code = 2
    default_fix = (
        "run `browserwright session new --backend=<extension|rdp> --name=TAB_GROUP_TITLE` "
        "then run `browserwright -s <id> -e 'print(snapshot())'`"
    )

    def __init__(self, detail: str = "", fix: str = ""):
        self.detail = detail
        super().__init__(
            "no session: run `browserwright session new --backend=<extension|rdp> "
            "--name=TAB_GROUP_TITLE` first (use the `=` form; --name is the "
            "Chrome tab group title), then pass -s <id> on every execute call. "
            + detail,
            fix=fix,
        )


class CDPError(BrowserwrightError):
    exit_code = 3
    default_fix = (
        "if the message mentions an unknown method (-32601) the daemon is "
        "likely stale — `browserwright-daemon stop` then re-run; otherwise check "
        "the method name and params"
    )

    def __init__(self, method: str = "", params=None, cdp_message: str = "", fix: str = ""):
        self.method, self.params, self.cdp_message = method, dict(params or {}), cdp_message
        super().__init__(f"CDP {method} failed: {cdp_message}", fix=fix)


class NeedsUserConfirm(BrowserwrightError):
    """Raised by remember_preference (and similar) when the agent must surface
    a confirm prompt to the user before re-calling with confirm=False."""

    exit_code = 1

    def __init__(self, what: str = "", proposal=None, fix: str = ""):
        self.what, self.proposal = what, proposal
        super().__init__(
            f"needs user confirm: {what}",
            # The proposal IS the next-action; mirror it into fix so the
            # generic envelope is uniform across every error type.
            fix=fix or "surface the proposal to the user, then re-call with confirm=True",
        )


def serialize(exc: BaseException) -> dict:
    """Compact JSON-friendly representation for stderr / repl socket."""
    out = {"type": type(exc).__name__, "msg": str(exc)}
    for k in ("url", "selector", "timeout", "reason", "signals", "kind",
              "status", "detail", "site", "task", "failed_check",
              "method", "cdp_message", "what", "proposal", "fix"):
        v = getattr(exc, k, None)
        if v is not None and not isinstance(v, (type(None),)):
            try:
                import json as _json
                _json.dumps(v)  # ensure serializable
                out[k] = v
            except (TypeError, ValueError):
                out[k] = repr(v)
    return out
