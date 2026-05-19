"""L2 -- standard user flows through skill, extension backend (primary path)."""
from __future__ import annotations

import json
from pathlib import Path

from .helpers import run_skill


def test_open_background_and_query_dom(ext_ready):
    script = (
        "import json\n"
        "h = open_background('data:text/html,<h1>e2e</h1>')\n"
        "wait_for_load()\n"
        "txt = js(\"document.querySelector('h1').textContent\")\n"
        "info = page_info()\n"
        "print(json.dumps({'text': txt, 'title': info.get('title'), 'url': info.get('url')}))\n"
    )
    result = run_skill(script=script, backend="extension")
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    payload = json.loads(line)
    assert payload["text"] == "e2e"
    assert payload["url"].startswith("data:text/html")


def test_screenshot_is_non_trivial(ext_ready, tmp_path):
    out_png = tmp_path / "shot.png"
    script = (
        f"open_background('data:text/html,<h1 style=\"font-size:120px\">SHOT</h1>')\n"
        "wait_for_load()\n"
        f"path = capture_screenshot({str(out_png)!r})\n"
        f"print(path)\n"
    )
    result = run_skill(script=script, backend="extension", timeout=60)
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert out_png.exists(), f"screenshot not written; stdout={result.stdout!r}"
    size = out_png.stat().st_size
    # A black/blank PNG is typically <2KB. Real screenshot is usually >>5KB.
    assert size > 5_000, f"screenshot suspiciously small: {size}B"
