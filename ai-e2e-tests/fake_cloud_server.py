"""Fake cloud-browser service for US5.

Mimics what Browser Use / Browserless / Hyperbrowser expose:

  - HTTP GET `/json/version` with Authorization: Bearer <token> →
    returns Chrome's normal discovery payload, but rewrites
    `webSocketDebuggerUrl` to point at us (so the daemon's discovery
    finds the right ws). 401 on missing/wrong token.

  - WS upgrade at `/devtools/browser/<id>` (or any /devtools/*) with the
    same Bearer header → proxies CDP frames bidirectionally to the
    isolated Chrome on 127.0.0.1:9444. 401 on missing/wrong token.

This lets the harness exercise the daemon's cloud backend end-to-end
without paying for a real cloud-browser service: the auth path is the
real cloud path, the wire format is the real CDP, but the Chrome
behind it is our existing isolated browser.

Run standalone:

    US5_FAKE_TOKEN=secret python fake_cloud_server.py --port 9555 \
        --upstream-port 9444

The harness launches it as a subprocess and reads its lifecycle off
stderr ("ready" marker line).
"""
from __future__ import annotations

import argparse
import asyncio
import http
import json
import os
import sys
from typing import Any

import httpx
import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response


def _h(*pairs: tuple[str, str]) -> Headers:
    h = Headers()
    for k, v in pairs:
        h[k] = v
    return h


# Module-level state populated from CLI args / env. Kept simple — this is a
# single-purpose test helper, not a library.
TOKEN: str = ""
OUR_PORT: int = 0
UPSTREAM_PORT: int = 0


def _check_auth(headers) -> bool:
    """Bearer token from `Authorization` header. Headers is a Mapping-like
    in websockets v16."""
    auth = headers.get("Authorization", "")
    return auth == f"Bearer {TOKEN}"


def _process_request(connection, request) -> Response | None:
    """Pre-handshake hook. Return a Response to short-circuit (HTTP), or
    None to let the ws upgrade proceed.

    Auth applies to BOTH the HTTP discovery GET and the ws handshake —
    same as real cloud-browser services do.
    """
    headers = request.headers
    if not _check_auth(headers):
        body = b"missing or invalid Bearer token\n"
        return Response(
            status_code=http.HTTPStatus.UNAUTHORIZED.value,
            reason_phrase="Unauthorized",
            headers=_h(("Content-Length", str(len(body)))),
            body=body,
        )

    path = request.path
    if path == "/json/version":
        # Fetch real Chrome's discovery, then rewrite the ws URL to point
        # at us so the daemon's downstream connect lands here too.
        try:
            with httpx.Client(trust_env=False, timeout=2.0) as c:
                r = c.get(f"http://127.0.0.1:{UPSTREAM_PORT}/json/version")
            body = r.json()
        except Exception as e:
            err_body = f"upstream unreachable: {e}\n".encode()
            return Response(
                status_code=http.HTTPStatus.BAD_GATEWAY.value,
                reason_phrase="Bad Gateway",
                headers=_h(("Content-Length", str(len(err_body)))),
                body=err_body,
            )
        upstream_ws_url = body.get("webSocketDebuggerUrl", "")
        # Rewrite host:port → ours
        rewritten = upstream_ws_url.replace(
            f"127.0.0.1:{UPSTREAM_PORT}", f"127.0.0.1:{OUR_PORT}"
        )
        body["webSocketDebuggerUrl"] = rewritten
        payload = json.dumps(body).encode()
        return Response(
            status_code=http.HTTPStatus.OK.value,
            reason_phrase="OK",
            headers=_h(
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
            ),
            body=payload,
        )

    if path.startswith("/devtools/"):
        # Let the ws upgrade handler take over.
        return None

    nf_body = b"unknown path\n"
    return Response(
        status_code=http.HTTPStatus.NOT_FOUND.value,
        reason_phrase="Not Found",
        headers=_h(("Content-Length", str(len(nf_body)))),
        body=nf_body,
    )


async def _proxy_handler(client_ws) -> None:
    """Bidirectionally forward CDP frames between the client (skill via
    daemon) and the upstream Chrome's ws endpoint."""
    # Path on our server is the same path Chrome expects (/devtools/browser/<id>).
    upstream_url = f"ws://127.0.0.1:{UPSTREAM_PORT}{client_ws.request.path}"
    try:
        async with websockets.connect(upstream_url) as upstream_ws:
            async def c_to_u():
                try:
                    async for msg in client_ws:
                        await upstream_ws.send(msg)
                except websockets.ConnectionClosed:
                    pass

            async def u_to_c():
                try:
                    async for msg in upstream_ws:
                        await client_ws.send(msg)
                except websockets.ConnectionClosed:
                    pass

            await asyncio.gather(c_to_u(), u_to_c())
    except Exception as e:
        # Don't let a single proxy failure kill the server.
        print(f"[fake-cloud] proxy error: {e}", file=sys.stderr)


async def _main(token: str, our_port: int, upstream_port: int) -> None:
    global TOKEN, OUR_PORT, UPSTREAM_PORT
    TOKEN = token
    OUR_PORT = our_port
    UPSTREAM_PORT = upstream_port

    async with serve(
        _proxy_handler,
        "127.0.0.1",
        our_port,
        process_request=_process_request,
    ):
        # Marker the harness watches for on stderr.
        print(
            f"[fake-cloud] ready: listening on 127.0.0.1:{our_port}, "
            f"upstream :{upstream_port}",
            file=sys.stderr,
            flush=True,
        )
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9555,
                    help="Port we listen on (default 9555).")
    ap.add_argument("--upstream-port", type=int, default=9444,
                    help="Real Chrome's port to proxy to (default 9444).")
    args = ap.parse_args()
    token = os.environ.get("US5_FAKE_TOKEN")
    if not token:
        print("FATAL: set US5_FAKE_TOKEN env var with the bearer token", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_main(token, args.port, args.upstream_port))
