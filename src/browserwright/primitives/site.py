"""Site + memory primitives (spec §A.2 second half).

These are the calls agents reach for to record per-site knowledge. The heavy
lifting lives in ``memory/`` — this module is mostly a thin shim that picks the
right host.
"""
from __future__ import annotations

import sys
from typing import Any, Optional
from urllib.parse import urlparse

from ..errors import NeedsUserConfirm
from ..memory import global_memory, site_memory
from ..memory.site_mem import RedactionRejected, bootstrap_site as _bootstrap, host_stem
from ..session import current_session


def _resolve_host(host_or_url: Optional[str]) -> str:
    """Pick the host stem to use.

    Priority:
      1. Explicit ``host_or_url`` argument.
      2. ``current_session().current_target_id`` → URL → host.
      3. Raise — we never silently write to "default" memory.
    """
    if host_or_url:
        return host_stem(host_or_url)
    sess = current_session()
    if sess.current_target_id:
        # Look it up via the tab list (cheap, single CDP call).
        from .page import list_tabs
        for t in list_tabs():
            if t["targetId"] == sess.current_target_id and t.get("url"):
                return host_stem(t["url"])
    raise ValueError("remember(): no host given and no current tab to infer from")


def bootstrap_site(host: str, aliases: Optional[list[str]] = None) -> str:
    """Lazy-create ``site-skills/<stem>/`` with the canonical layout. US2."""
    d = _bootstrap(host, aliases=aliases)
    return str(d)


def remember(host: Optional[str], text: str, *, section: str = "Notes") -> str:
    """Append a line to site memory (auto-lazy-creates the site dir).

    ``host`` accepts a URL, a hostname, or ``None`` to mean "the current tab".

    Refuses to write if redaction tripwires fire (high entropy, Bearer tokens,
    absolute user paths, etc.) — surfaces the reasons on stderr.
    """
    stem = _resolve_host(host)
    mem = site_memory(stem)
    try:
        path = mem.append(text, section=section)
    except RedactionRejected as e:
        print(
            f"[browserwright] remember() refused — redaction tripwires: {e.reasons}",
            file=sys.stderr,
        )
        return ""
    return str(path)


def remember_global(text: str, *, section: str = "Notes") -> str:
    """Append a free-form line to ``~/.browserwright/global.md``."""
    global_memory().append(text, section=section)
    return str(global_memory().path)


def remember_preference(key: str, value: Any, *, confirm: bool = True) -> dict:
    """Structured global preference write (spec §C.3 type D, US4).

    First call (``confirm=True``) raises ``NeedsUserConfirm``: the agent must
    surface a dialog to the user. After assent the agent re-calls with
    ``confirm=False`` and the new value lands in ``global.md`` frontmatter.

    **Dotted-key semantics** (v0.3.1 — Bug 4 from the AI E2E run):
    ``key`` is interpreted as a YAML frontmatter *path*, not a literal flat
    key. ``"daemon.preferred_backend"`` writes to ``frontmatter.daemon
    .preferred_backend`` — i.e. a nested mapping under ``daemon:``. This
    matches the install-wizard layout (``global.md`` keeps a ``daemon:``
    block with ``preferred_backend`` / ``notes`` siblings) and lets the
    agent group related preferences together.

    To write a flat top-level key, simply omit the dots
    (``remember_preference("dark_mode", True)`` → ``frontmatter.dark_mode``).

    **Caveat — silent overwrite on type mismatch** (REVIEW.md F-9):
    writing a dotted key whose root segment already holds a scalar
    silently replaces the scalar with a dict. e.g. if
    ``frontmatter.daemon`` was previously a string and the agent calls
    ``remember_preference("daemon.preferred_backend", "rdp")``, the
    string is destroyed and replaced with ``{preferred_backend: "rdp"}``.
    No diagnostic is emitted. Avoid this by never mixing scalar and
    dotted writes under the same root key; v0.6 will surface a
    ``NeedsUserConfirm`` warning when the type would change.

    The companion reader ``memory_read()`` / ``browserwright memory show
    --global`` returns the full frontmatter tree, so you can verify the
    write took the expected shape.

    Example::

        # First call asks the user.
        remember_preference("daemon.preferred_backend", "extension")
        # → NeedsUserConfirm raised; agent dialogs the user

        # After the user agrees:
        remember_preference("daemon.preferred_backend", "extension",
                            confirm=False)
        # → global.md frontmatter gains:
        #     daemon:
        #       preferred_backend: extension
        #       set_by_user_at: <ts>
    """
    if confirm:
        raise NeedsUserConfirm(
            what=f"set {key} = {value!r}",
            proposal={"key": key, "value": value},
        )
    return global_memory().set_preference(key, value, confirm=False)


def memory_read(site: Optional[str] = None) -> dict:
    """Bundle of all memory the agent might want to read.

    ``site=None`` → returns ``{"global": ..., "current_site": ...}`` with
    the current tab's site memory if attached.
    """
    out: dict[str, Any] = {"global": global_memory().read()}
    if site is None:
        sess = current_session()
        if sess.current_target_id:
            try:
                stem = _resolve_host(None)
                site = stem
            except ValueError:
                site = None
    if site:
        out["current_site"] = {"site": host_stem(site), "data": site_memory(site).read()}
    return out
