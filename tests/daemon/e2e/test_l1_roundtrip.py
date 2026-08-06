"""L1 -- single round-trip through the skill CLI."""
from __future__ import annotations

import json

from .helpers import run_skill


def test_extension_backend_page_info(ext_ready, e2e_daemon):
    """`browserwright` drives the injected Playwright `page` via the extension
    backend. The lazy `page` auto-binds the session's current tab (auto-opening
    one if needed), so we just read its url/title."""
    result = run_skill(
        script=(
            "import json\n"
            "info = {'url': page.url, 'title': page.title()}\n"
            "print(json.dumps(info))\n"
        ),
        backend="extension",
        runtime_dir=e2e_daemon.runtime_dir,
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


def test_cdp_backend_page_info(e2e_cdp_daemon):
    """CDP backend: the skill drives Chrome *through* the cdp Mode B daemon.
    ``e2e_cdp_daemon`` yields the daemon's XDG_RUNTIME_DIR (its fixed socket)."""
    result = run_skill(
        script=(
            "import json\n"
            "info = {'url': page.url, 'title': page.title()}\n"
            "print(json.dumps(info))\n"
        ),
        backend="cdp",
        runtime_dir=e2e_cdp_daemon,
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
