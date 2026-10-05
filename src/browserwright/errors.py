"""Skill exception hierarchy (spec §A.4)."""

import re

# Playwright's public ``TimeoutError`` (``playwright.sync_api.TimeoutError``
# is this same class). Imported from its defining module because that module
# is dependency-free: ``errors`` is loaded by every CLI invocation, and the
# public ``sync_api`` package would drag the whole driver stack in with it.
from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError


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
        "retry with page.goto(url); if it persists, "
        "check the URL and network with http_get(url)"
    )

    def __init__(
        self,
        url: str = "",
        reason: str = "",
        fix: str = "",
        detail: str = "",
    ):
        # `detail` carries the ORIGINAL exception type + message. Without it a
        # caller only ever sees our own coarse `reason` bucket, which is how a
        # whole batch of CDP/relay failures once got reported as "network" with
        # no way left to tell what actually broke. Never drop it.
        self.url, self.reason, self.detail = url, reason, detail
        message = f"page load failed: {url} ({reason})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message, fix=fix)


class PageBindTimeout(BrowserwrightError):
    """Playwright did not expose the session's already-resolved target in time."""

    exit_code = 3
    retryable = True
    # BUG B: `retryable` has to be honest. A retry only helps when the daemon
    # was still going to announce the tab — a cold/reconnecting extension
    # service worker is exactly that case. If retries do NOT help, the
    # extension side is not answering at all, and recycling an executor will not
    # change that — say what to check instead of looping the user.
    # Do not advertise `$BW_PAGE_BIND_TIMEOUT` here: the bind almost always
    # runs in the resident executor, which inherits the DAEMON's environment,
    # so an agent's `export` never reaches it.
    default_fix = (
        "retry the same browserwright command (a cold extension service worker "
        "can miss the bind window). If EVERY retry fails, the extension is not "
        "answering: check `browserwright doctor` and that the browserwright "
        "extension is enabled and its Chrome window is open, then run "
        "`browserwright recover --session <id>`"
    )

    def __init__(
        self,
        target_id: str = "",
        timeout: float = 0.0,
        fix: str = "",
    ):
        self.target_id, self.timeout = target_id, timeout
        target = repr(target_id) if target_id else "<missing>"
        super().__init__(
            "timed out binding Playwright to session target "
            f"{target} after {timeout:g}s; no replacement page was created",
            fix=fix,
        )


class TabRebindFailed(BrowserwrightError):
    """The session's bound tab died AND re-binding a replacement failed.

    Issue #86: a session whose tab dies used to answer every later call in
    1-4ms with ``TargetClosedError``, forever, because the bind was performed
    once and never revisited. The bind is now re-entered when the bound page
    is dead — but that recovery has to be *bounded*: exactly one attempt, and
    a failure that is TELLABLE APART from the dead-tab condition it was trying
    to repair. Without a distinct error a genuinely unusable browser would
    look like a tab that is merely closed, and the caller would keep asking
    for a rebind that cannot ever succeed.

    So: ``TargetClosedError`` (or ``PageLoadFailed(reason="target-closed")``)
    means "the tab is gone, recovery is being attempted"; THIS error means
    "the tab is gone and recovery itself failed" — a different next action.
    """

    exit_code = 3
    default_fix = (
        "the session's tab is gone and re-opening one failed; the browser or "
        "the extension relay is likely unusable. Run `browserwright recover "
        "--session <id>` — it waits for the extension, re-attaches the tab "
        "and keeps or restarts the executor, then names the layer that is "
        "still broken. Retrying the same call will NOT help, and a new "
        "session would fail the same way"
    )

    def __init__(self, reason: str = "", fix: str = ""):
        self.reason = reason
        message = "the session's tab is gone and re-binding a new one failed"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message, fix=fix)


#: The call deadline when the caller sets none: ``browserwright -e`` without
#: ``--timeout``, and every other exec surface without a ``timeout`` field
#: (ADR-0014). It is a ceiling for the whole call, not a per-operation default.
DEFAULT_CALL_TIMEOUT_S = 90


class DeadlineExceeded(BrowserwrightError):
    """The caller's call deadline ran out before the code finished (ADR-0014).

    Fail-stop, deliberately: the code is cut off wherever it was, the
    executor that ran it is terminated, and the next call cold-starts a fresh
    one on the same tabs. Its ``state`` is gone and ``finally`` blocks were not
    guaranteed to run. That is why this is not catchable from inside the code
    and why it has its own exit code (7): the agent's next move is to give the
    call more time or less work, never to retry the same call unchanged.

    ``scope`` names which timeout ran out. It is always ``"call"`` here; an
    operation timeout (one Playwright call) is a different error.
    """

    exit_code = 7
    default_fix = (
        "raise the call deadline with `browserwright -s <id> --timeout "
        "<seconds> -e ...` (default 90), or split the work into smaller calls. "
        "Browser tabs survive, but the executor was recycled: `state` is empty "
        "on the next call"
    )

    def __init__(self, timeout: float = 0.0, scope: str = "call",
                 fix: str = ""):
        self.timeout, self.scope = timeout, scope
        super().__init__(
            f"call deadline exceeded: the code did not finish within "
            f"{timeout:g}s (--timeout) and was stopped (fail-stop)",
            fix=fix,
        )


#: Playwright's own default for an action (``click``, ``fill``, ``wait_for``…)
#: when the agent sets no ``timeout=``. Restated here only for messages; the
#: value Playwright uses is its own.
PLAYWRIGHT_ACTION_TIMEOUT_S = 30
#: browserwright's smart ``page.goto`` default (``repl/_smart_goto.py``).
SMART_GOTO_TIMEOUT_S = 60

_TIMEOUT_MS_RE = re.compile(r"Timeout (\d+(?:\.\d+)?)ms exceeded")


class OperationTimeout(BrowserwrightError, PlaywrightTimeoutError):
    """One Playwright call ran out of its own timeout (ADR-0014).

    The operation timeout is the agent's ``timeout=`` /
    ``set_default_timeout()``, or Playwright's default (30s for an action, 60s
    for browserwright's smart ``goto``), capped by what is left of the call
    deadline. When the call deadline is the smaller of the two, running out is
    ``DeadlineExceeded`` (exit 7), never this.

    It subclasses Playwright's ``TimeoutError``, so code that already does
    ``except TimeoutError`` (imported from ``playwright.sync_api``) keeps
    catching it. It is an ordinary exception: the code can catch it and carry
    on, and the executor survives with its ``state``. Only when it escapes the
    code does the call exit with its own code (8).

    The executor produces it at the one funnel every sync Playwright call
    returns through (``repl/_operation_timeout.py``), so ``page.click``,
    ``locator.wait_for``, ``expect_*`` and the smart ``goto`` all raise it.

    Constructible from a single message, because Playwright's
    ``rewrite_error`` rebuilds an error as ``type(error)(message)``.
    """

    exit_code = 8
    default_fix = (
        "a single Playwright call ran out of its own timeout; the executor and "
        "`state` survived. Call snapshot() to check the page and that the "
        "target exists (a wrong selector waits the full timeout), then retry "
        "with the current ref; if the page is just slow, raise that call's "
        "timeout= (default 30s for actions, 60s for page.goto; it can never "
        "outlast the call deadline `--timeout`)"
    )

    def __init__(self, message: str = "", *, scope: str = "operation",
                 url: str = "", detail: str = "",
                 timeout: float | None = None, fix: str = ""):
        self.scope, self.detail = scope, detail
        self.url = url or None
        if timeout is None:
            m = _TIMEOUT_MS_RE.search(message)
            timeout = float(m.group(1)) / 1000.0 if m else None
        self.timeout = timeout
        super().__init__(message or "operation timed out", fix=fix)


class ElementNotFound(BrowserwrightError):
    exit_code = 3
    default_fix = (
        "use snapshot() to list interactive elements and their [ref=eN] "
        "handles, then act via page.locator(\"aria-ref=eN\")"
    )

    def __init__(self, selector: str = "", timeout: float = 0.0, fix: str = ""):
        self.selector, self.timeout = selector, timeout
        super().__init__(f"element not found: {selector!r} after {timeout}s", fix=fix)


class UnsupportedContentType(BrowserwrightError):
    """The page is not HTML, so there is nothing to convert to Markdown.

    Raised loudly on purpose (ADR-0007). A PDF is the motivating case: Chrome's
    built-in viewer renders a REAL DOM (an ``<embed>`` shell), so a best-effort
    conversion would succeed and return a short, plausible-looking result that
    contains none of the document — a silent empty answer, which is the one
    failure mode the markdown path is built to make impossible.

    browserwright deliberately does not grow a document pipeline. Route the URL
    to whatever handles that type and keep browserwright for HTML.
    """

    exit_code = 3
    default_fix = (
        "this endpoint only converts HTML; route non-HTML types "
        "(PDF, images, binaries) to a handler for that type"
    )

    def __init__(self, url: str = "", content_type: str = "", fix: str = ""):
        self.url, self.content_type = url, content_type
        super().__init__(
            f"not HTML, refusing to convert: {url} (Content-Type: "
            f"{content_type or 'unknown'})",
            fix=fix,
        )


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
        "run `browserwright recover --session <id>`: it probes the daemon "
        "twice, starts the installed one if it is gone or stale, and reports "
        "whether something else is answering on its port"
    )

    def __init__(self, detail: str = "", fix: str = ""):
        self.detail = detail
        super().__init__(f"daemon unavailable: {detail}", fix=fix)


class NoSession(BrowserwrightError):
    """No explicit session provided. Refuse rather than silently sharing a browser."""

    exit_code = 2
    default_fix = (
        "run `browserwright session new --reuse --backend=<extension|cdp> --name=SESSION_LABEL` "
        "(--reuse returns the existing session of that name instead of a new one) "
        "then pass `-s <id>` to browserwright commands, for example "
        "`browserwright -s <id> -e 'print(snapshot())'` or "
        "`browserwright -s <id> task <site>/<name>`"
    )

    def __init__(self, detail: str = "", fix: str = ""):
        self.detail = detail
        super().__init__(
            "no session: run `browserwright session new --reuse "
            "--backend=<extension|cdp> --name=SESSION_LABEL` first (use the `=` "
            "form; --name is a short session label; --reuse hands back an "
            "existing session of that name), then pass -s <id> on every "
            "execute call. "
            + detail,
            fix=fix,
        )


class CDPError(BrowserwrightError):
    exit_code = 3
    default_fix = (
        "if the message mentions an unknown method (-32601) the daemon is "
        "likely stale — `browserwright recover --session <id>` replaces it; "
        "otherwise check the method name and params"
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
            fix=fix or "surface the proposal to the user, then re-call with confirm=False",
        )


def serialize(exc: BaseException) -> dict:
    """Compact JSON-friendly representation for stderr / repl socket."""
    out = {"type": type(exc).__name__, "msg": str(exc)}
    for k in ("url", "selector", "target_id", "timeout", "scope", "reason", "signals", "kind",
              "status", "detail", "site", "task", "failed_check",
              "method", "cdp_message", "what", "proposal", "fix", "retryable"):
        v = getattr(exc, k, None)
        if v is not None and not isinstance(v, (type(None),)):
            try:
                import json as _json
                _json.dumps(v)  # ensure serializable
                out[k] = v
            except (TypeError, ValueError):
                out[k] = repr(v)
    return out


def playwright_error_fix(exc: BaseException) -> str:
    """Best-effort recovery hint for raw Playwright exceptions.

    This intentionally does not wrap or re-raise the original exception. It is
    used at serialization boundaries so agent-authored ``try/except`` behavior
    inside the executor stays native Playwright, while the surfaced error gains
    a concrete next step.
    """
    msg = str(exc)
    lower = msg.lower()
    exc_type = type(exc).__name__
    if "frame detached" in lower or "target closed" in lower or "page closed" in lower:
        return (
            "the session's tab binding is gone (extension reloaded/updated, "
            "daemon was replaced, or the tab was closed). Run "
            "`browserwright recover --session <id>`; it re-attaches or opens "
            "the session tab and confirms the executor before you retry"
        )
    if "timeout" not in lower and exc_type != "TimeoutError":
        return ""
    if "locator" in lower or "click" in lower or "fill" in lower:
        return "call snapshot() to confirm the target still exists, then re-snapshot and retry with the current ref"
    if "goto" in lower or "navigation" in lower:
        return "retry page.goto(url); Browserwright will use smart waiting, or verify the site with http_get(url)"
    return "call snapshot() to inspect the current page state, then retry the action with the current ref"
