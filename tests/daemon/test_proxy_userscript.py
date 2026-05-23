import json

import pytest

from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import DaemonState, UpstreamPhase


@pytest.mark.asyncio
async def test_browserdaemon_userscript_dispatch_invokes_callback():
    state = DaemonState(backend_name="extension")
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)
    client = state.allocate_client("skill-repl")
    out = []

    async def send(text: str) -> None:
        out.append(json.loads(text))

    seen = []

    async def userscript_request(verb: str, payload: dict) -> dict:
        seen.append((verb, payload))
        return {"ok": True, "id": payload["script"]["id"]}

    router.register_client(client.client_id, send)
    router._userscript_request = userscript_request
    await router.route_from_client(client, json.dumps({
        "id": 7,
        "method": "BrowserwrightDaemon.userscript.install",
        "params": {"script": {"id": "abc"}},
    }))

    assert seen == [("install", {"script": {"id": "abc"}})]
    assert out[-1] == {"id": 7, "result": {"ok": True, "id": "abc"}}
