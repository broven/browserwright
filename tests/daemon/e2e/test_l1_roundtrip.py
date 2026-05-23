"""L1 -- single round-trip through the skill CLI."""
from __future__ import annotations

import json

from .helpers import run_skill


def test_extension_backend_page_info(ext_ready):
    """`browserwright` returns page_info() via extension backend.
    Extension backend requires open_background() first (no default tab)."""
    result = run_skill(
        script=(
            "import json\n"
            "open_background('about:blank')\n"
            "wait_for_load()\n"
            "info = page_info()\n"
            "print(json.dumps(info))\n"
        ),
        backend="extension",
    )
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The last JSON line in stdout is page_info; tolerate skill banner noise.
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    info = json.loads(line)
    assert isinstance(info, dict)
    assert "url" in info and "title" in info


def test_rdp_backend_page_info(e2e_chrome_rdp):
    """RDP backend: bypass daemon, point skill directly at Chrome's ws URL."""
    result = run_skill(
        script=(
            "import json\n"
            "info = page_info()\n"
            "print(json.dumps(info))\n"
        ),
        backend="rdp",
        extra_env={"BS_CDP_WS": e2e_chrome_rdp.ws_url},
    )
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    info = json.loads(line)
    assert isinstance(info, dict)
    assert "url" in info and "title" in info
