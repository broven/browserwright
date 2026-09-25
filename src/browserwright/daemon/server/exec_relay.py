"""The endpoint's **exec-relay surface** (`/exec`) — ADR-0011.

Before ADR-0011 the client asked the daemon to ensure an executor, got a unix
socket path back, and dialed that socket itself ("Fork 2"). A socket path is
meaningless from another machine, so remote use was structurally impossible.
Now the daemon relays: the client opens `ws://<endpoint>/exec?session=<id>` and
the daemon bridges that websocket to the session's executor socket, which
becomes a daemon-internal detail nothing outside this process ever names.

Framing, and why the two legs differ:

  - **executor leg** — the executor's own length-prefixed protocol
    (`_executor/protocol.py`): 4-byte big-endian length, then that many bytes of
    UTF-8 JSON. Unchanged; the executor is not aware it is being relayed.
  - **ws leg** — one websocket message *is* one JSON frame. Websockets already
    carry a length, so re-prefixing would be a second framing layer that can
    disagree with the first. The daemon adds the prefix going out and strips it
    coming back.

Consequences the caller must know (ADR-0011 "What this does NOT change"):
execute payloads and large outputs now cross the daemon's event loop, and a
daemon restart severs a live data plane — previously it survived one. The client
surfaces that as `ExecutorUnavailable`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct

import websockets
from websockets.asyncio.server import ServerConnection

from ..._executor.protocol import ExecuteRequest, ExecuteResponse, _MAX_FRAME

logger = logging.getLogger(__name__)

_LEN = struct.Struct(">I")

#: How long to wait for the executor's unix socket to accept us. The registry
#: returns the path the moment it spawns, so the bind can race us by a few ms —
#: the same race `_executor/client._connect` used to absorb client-side.
_CONNECT_RETRY_S = 5.0


class ExecRelayError(Exception):
    """Nothing was relayed: no session, no registry, or no reachable executor."""


async def _dial_executor(sock_path: str) -> tuple:
    deadline = asyncio.get_running_loop().time() + _CONNECT_RETRY_S
    last: OSError | None = None
    while True:
        try:
            return await asyncio.open_unix_connection(sock_path)
        except OSError as e:
            last = e
            if asyncio.get_running_loop().time() >= deadline:
                raise ExecRelayError(
                    f"could not connect executor socket {sock_path!r}: {e}"
                ) from last
            await asyncio.sleep(0.05)


async def resolve_executor_sock(daemon, session_id: str) -> str:
    """The session's executor socket path, spawning the executor if absent.

    `/exec` can spawn an executor on its own (a direct or remote client, or an
    executor that died after `ensureExecutor`), so it goes through the same
    drivable path as the `ensureExecutor` verb — `Daemon.ensure_executor` —
    never a copy of it: "usually someone else warmed it up" is not an ordering
    guarantee, and a copy that skipped tab convergence is exactly how the two
    drifted.
    """
    ensure_executor = getattr(daemon, "ensure_executor", None)
    if not callable(ensure_executor):
        raise ExecRelayError(
            "exec relay unavailable: daemon has no executor registry")
    try:
        return await ensure_executor(session_id)
    except Exception as e:  # noqa: BLE001 - surfaced to the client as a close
        raise ExecRelayError(f"could not ensure executor: {e}") from e


async def serve_exec_relay(conn: ServerConnection, *, daemon,
                           session_id: str | None) -> None:
    """Bridge one `/exec` websocket to the session's executor socket."""
    if not session_id:
        with contextlib.suppress(Exception):
            await conn.close(code=1008, reason="/exec requires ?session=<id>")
        return
    try:
        sock_path = await resolve_executor_sock(daemon, session_id)
        reader, writer = await _dial_executor(sock_path)
    except ExecRelayError as e:
        logger.warning("exec relay(%s): %s", session_id, e)
        with contextlib.suppress(Exception):
            # 1011 + the reason: the client turns this into ExecutorUnavailable.
            await conn.close(code=1011, reason=str(e)[:120])
        return

    logger.info("exec relay: session %s bridged to %s", session_id, sock_path)
    c2e = asyncio.create_task(_ws_to_executor(conn, writer))
    e2c = asyncio.create_task(_executor_to_ws(
        reader, conn, daemon=daemon, session_id=session_id))
    try:
        await asyncio.wait({c2e, e2c}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (c2e, e2c):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await conn.close()


async def _ws_to_executor(conn: ServerConnection, writer) -> None:
    """One ws message → one length-prefixed executor frame."""
    try:
        async for raw in conn:
            payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
            if len(payload) > _MAX_FRAME:
                raise ExecRelayError(
                    f"exec frame too large: {len(payload)} > {_MAX_FRAME}")
            writer.write(_LEN.pack(len(payload)) + payload)
            await writer.drain()
    except websockets.exceptions.ConnectionClosed:
        return
    except (ExecRelayError, OSError) as e:
        logger.debug("exec relay c->e ended: %r", e)
        return


async def _executor_to_ws(reader, conn: ServerConnection, *, daemon=None,
                          session_id: str | None = None) -> None:
    """One length-prefixed executor frame → one ws message."""
    try:
        while True:
            header = await reader.readexactly(4)
            (length,) = _LEN.unpack(header)
            if length > _MAX_FRAME:
                raise ExecRelayError(
                    f"executor frame too large: {length} > {_MAX_FRAME}")
            payload = await reader.readexactly(length)
            payload = report_recovery_event(daemon, session_id, payload)
            await conn.send(payload.decode("utf-8", errors="replace"))
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return
    except websockets.exceptions.ConnectionClosed:
        return
    except (ExecRelayError, OSError) as e:
        logger.debug("exec relay e->c ended: %r", e)
        return


def report_recovery_event(daemon, session_id: str | None,
                          payload: bytes) -> bytes:
    """Hand the executor's ``recovery_event`` to the daemon's recovery state
    machine and return the agent-facing frame without it.

    The executor reports what it observed about the tab binding; the machine
    decides what that means for the session (ADR-0013 rule 1). Nothing here
    reads the agent-facing ``error`` or ``terminal_reason``: a recovery that
    had to be guessed from error text is one the machine could not see.
    """
    try:
        response = json.loads(payload)
    except ValueError:
        return payload  # not ours to judge; the client reports it malformed
    if not isinstance(response, dict) or "recovery_event" not in response:
        return payload
    event = response.pop("recovery_event")
    machine = getattr(daemon, "recovery", None)
    if machine is not None and session_id and isinstance(event, dict):
        from .session_state import EXECUTOR_RECOVERY_INPUTS

        kind = event.get("kind")
        machine_input = EXECUTOR_RECOVERY_INPUTS.get(kind)
        if machine_input is None:
            logger.debug("exec relay: ignoring recovery event %r", kind)
        else:
            try:
                machine.note(session_id, machine_input,
                             reason=str(event.get("detail") or kind)[:200],
                             executor_alive=True)
            except Exception:  # noqa: BLE001 - observation never breaks the data plane
                logger.debug("exec relay: could not record %r", kind,
                             exc_info=True)
    return json.dumps(response).encode("utf-8")


async def probe_executor_binding(daemon, session_id: str,
                                 *, timeout: float = 30.0) -> None:
    """Run a no-op on the resident executor to prove its tab binding."""
    registry = getattr(daemon, "executors", None)
    handle = registry.get(session_id) if registry is not None else None
    if handle is None or not handle.is_alive():
        raise ExecRelayError("no live executor available for binding probe")
    reader, writer = await _dial_executor(handle.sock_path)
    request = ExecuteRequest(
        code="None", timeout_ms=max(1, int(timeout * 1000)),
        executor_id=handle.executor_id)
    payload = json.dumps(request.to_dict()).encode()
    try:
        writer.write(_LEN.pack(len(payload)) + payload)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        (length,) = _LEN.unpack(header)
        if length > _MAX_FRAME:
            raise ExecRelayError(
                f"executor probe frame too large: {length} > {_MAX_FRAME}")
        raw = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        raw = report_recovery_event(daemon, session_id, raw)
        response = ExecuteResponse.from_dict(json.loads(raw))
        if response.error is not None:
            raise ExecRelayError(str(response.error.get("msg") or response.error))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
