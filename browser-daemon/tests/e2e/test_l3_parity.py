"""L3 -- same observable behaviour across backends."""
from __future__ import annotations

import json

import pytest

from .helpers import run_skill


PAGE = "data:text/html,<title>parity</title><h1 id=h>P</h1>"


def _extract_payload(stdout: str) -> dict:
    line = next(ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{"))
    return json.loads(line)


@pytest.mark.parametrize("backend,fixture_name,nav_fn", [
    ("extension", "ext_ready", "open_background"),
    ("rdp", "e2e_chrome_rdp", "goto_url"),
])
def test_dom_query_parity(backend, fixture_name, nav_fn, request):
    fixture_val = request.getfixturevalue(fixture_name)
    extra_env = {}
    if backend == "rdp":
        extra_env["BS_CDP_WS"] = fixture_val.ws_url
    script = (
        "import json\n"
        f"{nav_fn}({PAGE!r})\n"
        "wait_for_load()\n"
        "txt = js(\"document.getElementById('h').textContent\")\n"
        "title = js(\"document.title\")\n"
        "print(json.dumps({'txt': txt, 'title': title}))\n"
    )
    result = run_skill(script=script, backend=backend, extra_env=extra_env or None)
    assert result.returncode == 0, result.stderr
    payload = _extract_payload(result.stdout)
    assert payload["txt"] == "P"
    # Extension backend's open_background may prefix the title with an emoji
    assert "parity" in payload["title"]
