"""L2 -- standard user flows through skill, extension backend (primary path)."""
from __future__ import annotations

import json

from .helpers import run_skill


def test_open_and_query_dom(ext_ready, e2e_daemon):
    """Drive the injected Playwright `page` over the extension backend: set
    inline content (the extension aborts `data:` navigations over
    chrome.debugger — facade spec — so use set_content), then read the DOM."""
    script = (
        "import json\n"
        "page.set_content('<h1>e2e</h1>', wait_until='load')\n"
        "txt = page.locator('h1').text_content()\n"
        "info = {'title': page.title(), 'url': page.url}\n"
        "print(json.dumps({'text': txt, 'title': info['title'], 'url': info['url']}))\n"
    )
    result = run_skill(script=script, backend="extension",
                       runtime_dir=e2e_daemon.runtime_dir)
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    payload = json.loads(line)
    assert payload["text"] == "e2e"


def test_screenshot_is_non_trivial(ext_ready, e2e_daemon, tmp_path):
    """Screenshots are now taken via the Playwright `page` (the agent
    capture_screenshot primitive was removed in Phase C PR3)."""
    out_png = tmp_path / "shot.png"
    script = (
        "page.set_content('<h1 style=\"font-size:120px\">SHOT</h1>', "
        "wait_until='load')\n"
        f"page.screenshot(path={str(out_png)!r})\n"
        f"print({str(out_png)!r})\n"
    )
    result = run_skill(script=script, backend="extension", timeout=60,
                       runtime_dir=e2e_daemon.runtime_dir)
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert out_png.exists(), f"screenshot not written; stdout={result.stdout!r}"
    size = out_png.stat().st_size
    # A black/blank PNG is typically <2KB. Real screenshot is usually >>5KB.
    assert size > 5_000, f"screenshot suspiciously small: {size}B"
