"""Non-browser-driving primitive surface.

The legacy CDP browser-driving primitives (``open`` / ``goto_url`` /
``click_at_xy`` / ``js`` / ``cdp`` / ``capture_screenshot`` / ``snapshot`` /
the whole page/tab interaction stack) are DELETED — the agent drives the
browser with real Playwright via the injected ``page`` / ``context`` and
observes with ``snapshot()`` (see ``repl/_namespace.build_globals``). The
internal tab lifecycle that binding glue still needs lives in
``browserwright.session_runtime``.

What remains here is exactly the set listed in ``browserwright.EXPORTS``:
``http_get`` (no-browser escape hatch, ``.http``), the site/memory verbs
(``.site``), and the site-skill discovery/task layer (``.discovery_api``).
Import them from the top-level ``browserwright`` package (or from the
submodule directly) — this ``__init__`` deliberately re-exports nothing, so
there is exactly one place the surface is spelled out. Keep it boring — no
decorators, no metaprogramming — so the agent gets stable, greppable names.
"""
