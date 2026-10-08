"""Internal document inspection in a CDP isolated world.

Agent-authored ``page.evaluate`` keeps Playwright's main-world semantics.
Framework views use this helper so their globals and native DOM methods are
separate from the page's JavaScript. The CDP handle belongs to the Page; worlds
are resolved for every call because navigation destroys execution contexts.
"""
from __future__ import annotations

import json
from typing import Any

_WORLD_NAME = "browserwright-internal"
_SESSION_ATTR = "_browserwright_inspection_cdp"


def page_cdp_session(page: Any) -> Any:
    session = getattr(page, _SESSION_ATTR, None)
    if session is None:
        session = page.context.new_cdp_session(page)
        setattr(page, _SESSION_ATTR, session)
        def clear_session(*_args: Any) -> None:
            if getattr(page, _SESSION_ATTR, None) is session:
                setattr(page, _SESSION_ATTR, None)
        session.on("close", clear_session)
    return session


def target_id_for_page(page: Any) -> str:
    """Exact target identity without putting a marker in the document."""
    tree = page_cdp_session(page).send("Page.getFrameTree")
    return tree["frameTree"]["frame"]["id"]


def isolated_evaluate(page: Any, expression: str, arg: Any = None) -> Any:
    """Evaluate an internal JavaScript function with a JSON argument.

    Lightweight test doubles keep their existing ``evaluate`` surface. Real
    Playwright Pages fail closed if isolated evaluation fails; there is no
    fallback into the page's main world.
    """
    if not hasattr(page, "_impl_obj"):
        return page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
    session = page_cdp_session(page)
    tree = session.send("Page.getFrameTree")
    frame_id = tree["frameTree"]["frame"]["id"]
    world = session.send("Page.createIsolatedWorld", {
        "frameId": frame_id,
        "worldName": _WORLD_NAME,
        "grantUniveralAccess": False,
    })
    result = session.send("Runtime.evaluate", {
        "contextId": world["executionContextId"],
        "expression": f"({expression})({json.dumps(arg, ensure_ascii=False)})",
        "returnByValue": True,
        "awaitPromise": True,
    })
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        raise RuntimeError((details.get("exception") or {}).get("description")
                           or details.get("text") or "isolated evaluation failed")
    remote = result.get("result") or {}
    return remote.get("value")
