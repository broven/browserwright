"""Discover extension execution contexts without Runtime or Debugger events.

Blink resolves a native Document node in its frame's main world. V8's remote
object handle contains that world's actual context ID, so no page scripts,
globals, console previews, or debugger machinery are needed for discovery.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

SendCommand = Callable[[str, dict], Awaitable[dict]]
_REMOTE_OBJECT_ID = re.compile(r"(-?[0-9]+)\.([0-9]+)\.([0-9]+)\Z")


class ContextDiscoveryError(ValueError):
    """Chrome could not expose a supported local document context."""


class UnsupportedContextHandleError(ContextDiscoveryError):
    """Chrome's remote object format cannot supply a validated context ID."""


def _context_id(object_id: object) -> int:
    # V8 15.4 remote-object-id.cc serializes isolate.context.boundObject.
    # CDP treats the string as opaque: reject unfamiliar formats explicitly
    # rather than guessing an ID or enabling expensive Runtime events.
    match = (_REMOTE_OBJECT_ID.fullmatch(object_id)
             if isinstance(object_id, str) and len(object_id) <= 128 else None)
    if match is None:
        raise UnsupportedContextHandleError("unsupported Chrome remote-object ID format")
    _, context_id, bound_id = map(int, match.groups())
    if context_id <= 0 or bound_id <= 0:
        raise UnsupportedContextHandleError("invalid Chrome remote-object ID")
    return context_id


async def discover_default_context(
    send: SendCommand, frame_id: str, *, main: bool,
) -> dict:
    """Return the real default realm for a native Chrome frame ID.

    Child documents must belong to this CDP target; OOPIFs require their own
    target routing. Internal discovery uses backend node IDs because requesting
    the root document resets the DOM domain's frontend node ID bindings.
    """
    if main:
        document = (await send("DOM.getDocument", {"depth": 0}))["root"]
    else:
        owner = await send("DOM.getFrameOwner", {"frameId": frame_id})
        described = await send("DOM.describeNode", {
            "backendNodeId": owner["backendNodeId"], "depth": 0,
        })
        document = described["node"].get("contentDocument")
        if not isinstance(document, dict):
            raise ContextDiscoveryError("frame has no local document in this CDP target")
    remote = (await send("DOM.resolveNode", {
        "backendNodeId": document["backendNodeId"],
    })).get("object", {})
    object_id = remote.get("objectId")
    try:
        if remote.get("subtype") != "node":
            raise ContextDiscoveryError("document did not resolve to a native node")
        context_id = _context_id(object_id)
        checked = await send("Runtime.evaluate", {
            "expression": "void 0", "contextId": context_id,
            "silent": True, "returnByValue": True, "disableBreaks": True,
            "throwOnSideEffect": True,
        })
        if checked.get("exceptionDetails"):
            raise ContextDiscoveryError("Chrome rejected the document execution context")
        return {
            "id": context_id, "origin": "", "name": "",
            # Context numbers can repeat in a replacement renderer; the native
            # isolate component distinguishes those realms during navigation.
            "_native_identity": f"{int(object_id.split('.')[0])}.{context_id}",
            "auxData": {"isDefault": True, "type": "default", "frameId": frame_id},
        }
    finally:
        if isinstance(object_id, str):
            await send("Runtime.releaseObject", {"objectId": object_id})
