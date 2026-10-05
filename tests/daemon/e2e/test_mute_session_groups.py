"""Extension backend (real Chrome): session-group tabs are muted by default.

Agent tabs must not make noise at the user. A tab is muted while it sits in a
session group (title `<name>-BW<sid>`, ADR-0009), unmuted again when it leaves
— but only if the extension muted it — and the popup setting applies to every
live session tab the moment it flips.

The session's tab comes from the real daemon path (`page.goto` in a seeded
session → `createTab` → group + title); everything after that is driven and
read back through `chrome.*` in the extension's own service worker.
"""
from __future__ import annotations

import json

import pytest

from .test_issue86_dead_tab_rebind import local_site  # noqa: F401 - fixture
from .test_l2_heredoc_playwright_page import (
    ext_autofacade_ready as _ext_autofacade_ready,  # noqa: F401 - fixture
)


def _sw_eval(chrome, extension_id: str, body: str):
    """Run `body` (an async function body) in the extension service worker."""
    from browserwright.cdp import CDPSession

    from .test_l2_multisession import _extension_worker_target_id

    cdp = CDPSession(chrome.ws_url)
    try:
        session = cdp.attach(_extension_worker_target_id(cdp, extension_id))
        result = cdp.send(
            "Runtime.evaluate", session=session, returnByValue=True,
            awaitPromise=True, expression=f"(async () => {{{body}}})()")
    finally:
        cdp.close()
    if "exceptionDetails" in result:
        raise AssertionError(f"service worker eval failed: {result!r}")
    return result.get("result", {}).get("value")


def _muted(chrome, extension_id: str, tab_id: int) -> dict:
    # Mute updates land asynchronously off tab/group events; give them a beat.
    return _sw_eval(chrome, extension_id, (
        "await new Promise(r => setTimeout(r, 300));"
        f"const t = await chrome.tabs.get({int(tab_id)});"
        "return {muted: t.mutedInfo.muted, reason: t.mutedInfo.reason || null,"
        " ours: t.mutedInfo.extensionId === chrome.runtime.id};"))


def test_session_group_tabs_are_muted(
    _ext_autofacade_ready,  # noqa: F811 - imported pytest fixture
    e2e_chrome,
    patched_ext_dir,
    local_site,  # noqa: F811 - imported pytest fixture
):
    pytest.importorskip("playwright.sync_api")
    from .test_l2_heredoc_playwright_page import (
        _chrome_group_tab_ids,
        _cleanup_session,
        _run_execute,
        _seed_session,
        _session_group_id,
    )
    from .test_l2_multisession import (
        _chrome_close_tabs,
        _extension_id_from_path,
    )

    runtime_dir, _facade_ws = _ext_autofacade_ready
    ext = _extension_id_from_path(patched_ext_dir)
    sid = _seed_session(runtime_dir, "extension")
    stray: list[int] = []
    try:
        script = f"page.goto({local_site + '/mute'!r})\nprint('OK')\n"
        r = _run_execute(script, sid=sid, runtime_dir=runtime_dir, timeout=90)
        if r.returncode != 0:
            # Known cold-start announce race the other e2e tests retry through.
            _run_execute("reset()\n", sid=sid, runtime_dir=runtime_dir,
                         timeout=60)
            r = _run_execute(script, sid=sid, runtime_dir=runtime_dir,
                             timeout=90)
        assert r.returncode == 0, f"goto failed: {r.stdout!r} {r.stderr!r}"

        gid = _session_group_id(e2e_chrome, ext, sid)
        assert gid is not None, "session has no tab group"
        tab_ids = _chrome_group_tab_ids(e2e_chrome, ext, gid)
        assert tab_ids, "session group has no tabs"
        tab = tab_ids[0]

        # 1. The agent's tab is muted by the extension.
        state = _muted(e2e_chrome, ext, tab)
        assert state == {"muted": True, "reason": "extension", "ours": True}, state

        # 2. Any tab that joins the group is muted too — not only agent ones.
        joined = _sw_eval(e2e_chrome, ext, (
            f"const t = await chrome.tabs.create({{url: {json.dumps(local_site + '/joined')}, active: false}});"
            f"await chrome.tabs.group({{groupId: {gid}, tabIds: [t.id]}});"
            "return t.id;"))
        stray.append(joined)
        assert _muted(e2e_chrome, ext, joined)["muted"] is True

        # 3. Leaving the group restores sound.
        _sw_eval(e2e_chrome, ext, f"await chrome.tabs.ungroup([{joined}]);")
        assert _muted(e2e_chrome, ext, joined)["muted"] is False

        # 4. A tab outside any session group is never touched.
        other = _sw_eval(e2e_chrome, ext, (
            f"const t = await chrome.tabs.create({{url: {json.dumps(local_site + '/other')}, active: false}});"
            "const g = await chrome.tabs.group({tabIds: [t.id]});"
            "await chrome.tabGroups.update(g, {title: 'user group'});"
            "return t.id;"))
        stray.append(other)
        assert _muted(e2e_chrome, ext, other)["muted"] is False

        # 5. Turning the setting off unmutes live session tabs at once…
        _sw_eval(e2e_chrome, ext, "await setMuteEnabled(false);")
        assert _muted(e2e_chrome, ext, tab)["muted"] is False

        # 6. …and turning it back on re-mutes them.
        _sw_eval(e2e_chrome, ext, "await setMuteEnabled(true);")
        assert _muted(e2e_chrome, ext, tab)["muted"] is True
    finally:
        _sw_eval(e2e_chrome, ext, "await setMuteEnabled(true);")
        _chrome_close_tabs(e2e_chrome, ext, stray)
        gid = _session_group_id(e2e_chrome, ext, sid)
        if gid is not None:
            _chrome_close_tabs(e2e_chrome, ext,
                               _chrome_group_tab_ids(e2e_chrome, ext, gid))
        _cleanup_session("extension", sid)
