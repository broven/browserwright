import pytest

from .test_relay import _MockExtension, _relay_running


@pytest.mark.asyncio
async def test_userscript_request_forwards_and_returns_result():
    async with _relay_running() as relay:
        ext = _MockExtension()
        await ext.connect(relay.port)
        await relay.wait_ready(timeout=2.0)

        async def responder():
            sent = await ext.next_command(timeout=2.0)
            assert sent["type"] == "userscript.install"
            assert sent["script"] == {"id": "abc"}
            await ext.respond(sent["id"], result={"ok": True, "id": "abc"})

        import asyncio
        task = asyncio.create_task(responder())
        res = await relay.userscript_request(
            "install", {"script": {"id": "abc"}}, timeout=1.0)
        await task
        assert res == {"ok": True, "id": "abc"}
        await ext.close()
