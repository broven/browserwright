"""L3 -- same observable behaviour across backends."""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from .helpers import run_skill


PAGE = "data:text/html,<title>parity</title><h1 id=h>P</h1><button id=b>go</button>"


def _extract_payload(stdout: str) -> dict:
    line = next(ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{"))
    return json.loads(line)


def _rdp_bs_home() -> Path:
    return Path(__file__).resolve().parent / "_bs_home" / "rdp"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_rdp_create_session(session_id: str, name: str) -> Path:
    sessions = _rdp_bs_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    ledger = sessions / "ledger.json"
    data = json.loads(ledger.read_text()) if ledger.exists() else {
        "next_id": 1,
        "sessions": {},
    }
    now = time.time()
    data["sessions"][session_id] = {
        "id": session_id,
        "backend": "rdp",
        "workspace": {"port": _free_port()},
        "owner": "create",
        "name": name,
        "created_at": now,
        "last_seen": now,
    }
    ledger.write_text(json.dumps(data), encoding="utf-8")
    return ledger


def _remove_session(ledger: Path, session_id: str) -> None:
    if not ledger.exists():
        return
    data = json.loads(ledger.read_text())
    data.get("sessions", {}).pop(session_id, None)
    ledger.write_text(json.dumps(data), encoding="utf-8")


PARITY_SCRIPT = (
    "import json\n"
    "from browserwright.session import current_session\n"
    f"tab = open({PAGE!r})\n"
    "wait_for_load()\n"
    "before = js(\"document.getElementById('h').textContent\")\n"
    "js(\"document.getElementById('h').textContent = 'Q'; document.body.dataset.parity = 'yes'\")\n"
    "after = js(\"document.getElementById('h').textContent\")\n"
    "marker = js(\"document.body.dataset.parity\")\n"
    "info = page_info()\n"
    "current = current_tab()\n"
    "tabs_before_close = list_tabs(include_chrome=False)\n"
    "close = close_tab(target_id=tab['targetId'])\n"
    "end_result = None\n"
    "if current_session().session_record.get('owner') == 'create':\n"
    "    from browserwright import session_create\n"
    "    end_result = session_create.end(current_session().session_record)\n"
    "print(json.dumps({\n"
    "    'tab': tab,\n"
    "    'before': before,\n"
    "    'after': after,\n"
    "    'marker': marker,\n"
    "    'info': info,\n"
    "    'current': current,\n"
    "    'tabsBeforeClose': tabs_before_close,\n"
    "    'close': close,\n"
    "    'end': end_result,\n"
    "}, sort_keys=True))\n"
)


@pytest.mark.parametrize("case", [
    "extension",
    "rdp_attach",
    "rdp_create",
])
def test_usage_layer_parity_after_backend_selection(case, request):
    """After session creation, the same skill primitives work on every backend."""
    backend = "extension" if case == "extension" else "rdp"
    extra_env = None
    ledger = None
    session_id = None
    if case == "extension":
        request.getfixturevalue("ext_ready")
        runtime_dir = request.getfixturevalue("e2e_daemon").runtime_dir
    else:
        runtime_dir = request.getfixturevalue("e2e_rdp_daemon")
        if case == "rdp_create":
            session_id = "e2e-rdp-create-parity"
            ledger = _seed_rdp_create_session(session_id, "rdp-create-parity")
            extra_env = {"BD_SESSION": session_id}
    try:
        result = run_skill(
            script=PARITY_SCRIPT,
            backend=backend,
            runtime_dir=runtime_dir,
            extra_env=extra_env,
            timeout=90,
        )
        assert result.returncode == 0, (
            f"skill exited {result.returncode};\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        payload = _extract_payload(result.stdout)
    finally:
        if ledger is not None and session_id is not None:
            _remove_session(ledger, session_id)

    target_id = payload["tab"]["targetId"]
    assert payload["before"] == "P"
    assert payload["after"] == "Q"
    assert payload["marker"] == "yes"
    assert "parity" in payload["info"]["title"]
    assert payload["current"]["targetId"] == target_id
    assert any(tab["targetId"] == target_id for tab in payload["tabsBeforeClose"])
    assert payload["close"]["ok"] is True
    if case == "extension":
        assert isinstance(payload["tab"]["groupId"], int)
        assert payload["tab"]["groupId"] >= 0
    else:
        assert payload["tab"]["groupId"] == -1
        assert payload["tab"]["tabId"] is None
    if case == "rdp_create":
        assert "browser it launched was closed" in payload["end"]
