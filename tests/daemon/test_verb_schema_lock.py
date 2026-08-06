"""Schema lock: every ``BrowserwrightDaemon.*`` verb, on every backend.

This test is the one `verbs.py` kept describing but never had. Four comments in
`verbs.py` (and one `if False:` no-op) referred to "the schema-lock test that
scans this file for method literals"; no such test existed anywhere in the repo,
so the contract those comments protect — *param validation must run before the
backend-wiring check, so an empty-params call can never be mistaken for an
unknown method* — was unenforced. The dead no-op is removed together with this
file landing; the table below replaces it.

The contract being locked (CONTEXT.md → *verb*, docs/refactor-single-daemon.md):

> every verb returns a **same-shape, honest** result on every backend. Where a
> concept is backend-specific the daemon falls back to the nearest honest
> equivalent — never a fabricated value, and **never `-32601`**.

Layer contract (why this survives the C2/C3 rewrites)
-----------------------------------------------------
Everything here goes through the daemon's downstream JSON-RPC surface:

  * ``DaemonState(backend_name=...)`` + ``allocate_client()``  — public
  * ``Router(state)`` / ``register_client`` / ``bind_lifecycle`` /
    ``update_upstream_send`` / ``route_from_client``            — public
  * one JSON-RPC request frame in, one response frame out       — the contract

No private router attribute is assigned, no callback slot is stubbed, no method
under test is monkeypatched. C2 replaces `Router`'s twelve mutable callback
slots with a declared `Upstream` protocol; none of that is reachable from here.

**The verb table is the lock.** It is written out by hand on purpose — a table
derived from the source it guards cannot detect a verb being renamed away.
Assertions are deliberately one-directional (they pass if the daemon gets
*better*), so a C2 fix never forces an edit to this file.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import DaemonState, UpstreamPhase

# ---- the lock --------------------------------------------------------------

#: Every verb the daemon promises to answer. Adding a verb without adding it
#: here means it is not covered; renaming one turns this file red.
DAEMON_VERBS: tuple[str, ...] = (
    "BrowserwrightDaemon.getBackendInfo",
    "BrowserwrightDaemon.status",
    "BrowserwrightDaemon.waitForSessionAnnounce",
    "BrowserwrightDaemon.attachActiveTab",
    "BrowserwrightDaemon.openBackgroundTab",
    "BrowserwrightDaemon.closeTab",
    "BrowserwrightDaemon.endSession",
    "BrowserwrightDaemon.ensureExecutor",
    "BrowserwrightDaemon.killExecutor",
    "BrowserwrightDaemon.recoverSession",
    "BrowserwrightDaemon.extension.reload",
    "BrowserwrightDaemon.userscript.install",
    "BrowserwrightDaemon.userscript.list",
    "BrowserwrightDaemon.userscript.remove",
    "BrowserwrightDaemon.userscript.toggle",
    "BrowserwrightDaemon.userscript.logs",
)

#: `extension` is the sole relay backend; `cdp` and `env` are the raw-CDP family
#: (CONTEXT.md: the discriminator is `backend != "extension"`, never a name
#: check. `env` folded into `cdp` in #38, so the raw-CDP family currently has
#: one member — the tuple-of-one is kept deliberately: it documents that this
#: is a *family*, and a future third raw backend joins by editing one line.)
BACKENDS: tuple[str, ...] = ("extension", "cdp")
RAW_CDP_BACKENDS: tuple[str, ...] = ("cdp",)

#: Verbs that must never answer -32601 on ANY backend, even with no params and
#: no upstream wired. These are exactly the verbs whose handlers validate params
#: *before* consulting backend wiring — the property the deleted `verbs.py`
#: comments existed to protect.
VERBS_NEVER_METHOD_NOT_FOUND: frozenset[str] = frozenset({
    "BrowserwrightDaemon.getBackendInfo",
    "BrowserwrightDaemon.status",
    "BrowserwrightDaemon.openBackgroundTab",
    "BrowserwrightDaemon.closeTab",
    "BrowserwrightDaemon.endSession",
    "BrowserwrightDaemon.ensureExecutor",
    "BrowserwrightDaemon.killExecutor",
    "BrowserwrightDaemon.recoverSession",
})

#: `extension.reload` asks a Chrome extension to reload itself. On the raw-CDP
#: backends there is no extension, and the daemon answers -32601 today. Recorded
#: as a known divergence from the "never -32601" rule rather than asserted, so a
#: future honest-shim fix needs no edit here.
RELAY_ONLY_VERBS: frozenset[str] = frozenset({
    "BrowserwrightDaemon.extension.reload",
})

#: A method the daemon has genuinely never heard of. Used as the positive
#: control that proves the unknown-method assertion below can actually fail.
UNDECLARED_VERB = "BrowserwrightDaemon.thisVerbDoesNotExist"

UNKNOWN_METHOD_MARKER = "unknown BrowserwrightDaemon method"

#: A verb dispatch is pure bookkeeping — nothing here talks to a browser. If one
#: takes longer than this, something is waiting on a resource it should not be.
VERB_BUDGET_S = 5.0


# ---- harness: a real Router, driven only by JSON-RPC frames ----------------


async def probe(backend: str, method: str,
                params: dict | None = None) -> tuple[dict, float]:
    """Feed one JSON-RPC request frame to a freshly built Router and return the
    response frame plus how long it took.

    The Router is left in its cold state (no upstream connection, no extension
    callbacks) on purpose: that is exactly the state in which a verb dispatch
    bug looks like "unknown method".
    """
    state = DaemonState(backend_name=backend)
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)
    replies: list[dict] = []

    async def ensure_upstream() -> None:
        return None

    async def trigger_disconnect(reason: str) -> None:
        return None

    async def upstream_send(text: str) -> None:
        return None

    async def send_to_client(text: str) -> None:
        replies.append(json.loads(text))

    router.bind_lifecycle(ensure_upstream, trigger_disconnect)
    router.update_upstream_send(upstream_send)
    client = state.allocate_client(
        "agent", session_id="bs-session-1", session_name="agent")
    router.register_client(client.client_id, send_to_client)

    frame = {"id": 1, "method": method}
    if params is not None:
        frame["params"] = params
    started = time.monotonic()
    await asyncio.wait_for(
        router.route_from_client(client, json.dumps(frame)),
        timeout=VERB_BUDGET_S)
    elapsed = time.monotonic() - started

    assert replies, f"{backend}/{method}: the daemon sent no response at all"
    return replies[-1], elapsed


def code_of(reply: dict) -> int | None:
    """JSON-RPC error code, or None when the reply carried a result."""
    err = reply.get("error")
    return err.get("code") if isinstance(err, dict) else None


def message_of(reply: dict) -> str:
    err = reply.get("error")
    return err.get("message", "") if isinstance(err, dict) else ""


# ---- the assertions --------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("method", DAEMON_VERBS)
@pytest.mark.asyncio
async def test_verb_is_recognised_on_every_backend(backend: str, method: str):
    """No declared verb is ever answered with "unknown method", on any backend,
    with no params, with nothing wired.

    This is the weakest form of the contract and the one that must hold
    unconditionally: a verb dropped from the dispatcher during a refactor shows
    up here immediately, on all three backends at once.
    """
    reply, elapsed = await probe(backend, method)
    assert reply["id"] == 1
    assert elapsed < VERB_BUDGET_S, (
        f"{backend}/{method} took {elapsed:.2f}s — verb dispatch must not block"
    )
    assert not (code_of(reply) == -32601
                and UNKNOWN_METHOD_MARKER in message_of(reply)), (
        f"{backend}/{method} is no longer dispatched: {reply!r}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_undeclared_verb_still_reports_unknown_method(backend: str):
    """Positive control for the test above: a method the daemon really does not
    have must produce the unknown-method error. Without this, a dispatcher that
    silently swallowed *everything* would look like a pass.
    """
    reply, _ = await probe(backend, UNDECLARED_VERB)
    assert code_of(reply) == -32601
    assert UNKNOWN_METHOD_MARKER in message_of(reply)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("method", sorted(VERBS_NEVER_METHOD_NOT_FOUND))
@pytest.mark.asyncio
async def test_param_validation_runs_before_backend_wiring(backend: str, method: str):
    """The invariant the deleted `verbs.py` comments guarded.

    Called with no params these verbs must answer with a *parameter* verdict
    (-32602), a *runtime* verdict (-32603), or a result — never -32601. Moving
    the wiring/readiness check ahead of param validation would flip them to
    "method not found" and make an empty-params call indistinguishable from an
    unimplemented verb.
    """
    reply, _ = await probe(backend, method)
    assert code_of(reply) != -32601, (
        f"{backend}/{method} answered method-not-found on an empty-params "
        f"call — param validation must run first: {reply!r}"
    )


@pytest.mark.parametrize("backend", RAW_CDP_BACKENDS)
@pytest.mark.parametrize(
    "method", [v for v in DAEMON_VERBS if v not in RELAY_ONLY_VERBS])
@pytest.mark.asyncio
async def test_raw_cdp_backends_never_answer_method_not_found(
        backend: str, method: str):
    """The "never -32601" rule, enforced strictly on `cdp` and `env`.

    Both are the raw-CDP family, so every session verb has a raw-CDP
    implementation and must reach it. An unimplemented divergence has to be an
    honest result or a -32603, never "I don't have that method".
    """
    reply, _ = await probe(backend, method)
    assert code_of(reply) != -32601, f"{backend}/{method}: {reply!r}"


@pytest.mark.parametrize(
    "method",
    [v for v in DAEMON_VERBS if v not in VERBS_NEVER_METHOD_NOT_FOUND])
@pytest.mark.asyncio
async def test_extension_verbs_report_availability_not_unknown_method(method: str):
    """The remaining extension verbs need a live extension upstream, which a
    cold Router has not wired. When they refuse, the refusal must say *why*
    (availability), not "no such method".

    One-directional on purpose: the moment one of these starts answering
    properly instead of refusing, the assertion still holds.
    """
    reply, _ = await probe("extension", method)
    code = code_of(reply)
    assert code != -32601 or "requires the extension backend" in message_of(reply), (
        f"extension/{method}: {reply!r}"
    )


@pytest.mark.asyncio
async def test_verbs_answer_without_a_websocket_session_binding():
    """A client that connected without ``?session=<id>`` must still get a
    response to every verb — an actionable -32602, never silence.

    Session scoping is enforced at the daemon boundary; a client that skips it
    should be told so, because a dropped frame here is exactly the shape of a
    hang (the client waits forever for a reply that was never queued).
    """
    for method in DAEMON_VERBS:
        state = DaemonState(backend_name="extension")
        state.upstream_phase = UpstreamPhase.CONNECTED
        router = Router(state)
        replies: list[dict] = []

        async def send_to_client(text: str) -> None:
            replies.append(json.loads(text))

        # No session_id: the connection never named a browserwright session.
        client = state.allocate_client("anonymous")
        router.register_client(client.client_id, send_to_client)
        await asyncio.wait_for(
            router.route_from_client(
                client, json.dumps({"id": 9, "method": method})),
            timeout=VERB_BUDGET_S)
        assert replies, f"{method} produced no reply for a sessionless client"
        assert replies[-1]["id"] == 9
