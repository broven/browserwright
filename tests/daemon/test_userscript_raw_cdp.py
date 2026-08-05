"""Raw-CDP userscript registration — the shim must not report a success it
did not achieve, and a script installed before a tab exists must still run in
that tab once it opens.

Kept out of test_upstream_protocol.py on purpose: that file (with
test_hang_budget / test_relay_reconnect_paths / test_verb_schema_lock) has gone
untouched through every refactor on this branch, which is only a meaningful
signal while nobody edits it — including to add unrelated coverage.
"""
import pytest

from browserwright.daemon.server.upstream import CdpUpstream

# ---- raw-CDP userscript honesty (review-loop round 2) ----------------------
#
# `userscript push` used to take its target list from the *calling client's*
# bindings. A one-shot CLI websocket has none, so the list was empty: the script
# was stored, registered against nothing, and reported ok — a success the caller
# could not distinguish from a script that was actually running.


def _cdp_with_targets(*upstream_session_ids: str) -> CdpUpstream:
    async def _noop_frame(_text: str) -> None:
        return None

    async def _noop_close(_reason: str) -> None:
        return None

    up = CdpUpstream(_noop_frame, _noop_close)
    for i, sid in enumerate(upstream_session_ids):
        up._target_sessions[f"T{i}"] = sid
    return up


@pytest.mark.asyncio
async def test_userscript_install_uses_adapter_targets_when_caller_has_none():
    """A transient client supplies no bindings; the adapter knows its own."""
    up = _cdp_with_targets("sid-a", "sid-b")
    seen: list[str | None] = []

    async def _send(method, params=None, session_id=None):
        seen.append(session_id)
        return {"result": {"identifier": f"id-{session_id}"}}

    up.send_command = _send
    result = await up.userscript_request(
        "install", {"script": {"id": "s1", "source": "console.log(1)"}},
        session_ids=[])

    assert sorted(x for x in seen if x) == ["sid-a", "sid-b"]
    assert result["sync"]["registered"] == 2
    assert result["sync"]["pending"] is False


@pytest.mark.asyncio
async def test_userscript_install_with_no_live_target_reports_pending():
    """Stored is not active. Zero registrations must not read as success."""
    up = _cdp_with_targets()  # no attached page targets

    async def _send(method, params=None, session_id=None):  # pragma: no cover
        raise AssertionError("must not register against a nonexistent target")

    up.send_command = _send
    result = await up.userscript_request(
        "install", {"script": {"id": "s1", "source": "console.log(1)"}},
        session_ids=[])

    sync = result["sync"]
    assert sync["registered"] == 0
    assert sync["failed"] == []
    # Nothing failed, so ok stays True — but `pending` is what tells the caller
    # the script is stored and not yet running anywhere.
    assert sync["pending"] is True


@pytest.mark.asyncio
async def test_stored_userscript_is_applied_to_a_newly_attached_page():
    """Install-then-open-a-tab must run the script in that tab."""
    up = _cdp_with_targets()
    up._userscripts["s1"] = {
        "id": "s1", "identity": "s1", "source": "console.log(1)",
        "ids": [], "enabled": True,
    }
    registered: list[str] = []

    async def _send(method, params=None, session_id=None):
        if method == "Page.addScriptToEvaluateOnNewDocument":
            registered.append(session_id)
            return {"result": {"identifier": "id-1"}}
        raise AssertionError(f"unexpected {method}")

    up.send_command = _send
    await up._apply_userscripts_to_session("sid-new")

    assert registered == ["sid-new"]
    assert up._userscripts["s1"]["ids"] == [("sid-new", "id-1")]
