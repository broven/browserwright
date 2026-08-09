"""`browserwright markdown <url>` end to end, on the extension backend.

Every piece of this command is unit-tested elsewhere; what is only testable here
is the **composition**: mint a throwaway session → cold-start its executor →
navigate the user's real Chrome → render → tear the session back down. In
particular the teardown, because a leak here leaves a tab group behind in a real
browser (issue #53).

Uses the isolated harness — test daemon on its own socket + Chrome for Testing
with the patched unpacked extension — so it never touches the daily Chrome.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from .conftest import TEST_EXT_PORT, scrubbed_env

_PAGE = b"""<!doctype html><html><head><title>E2E Doc</title></head><body>
<nav><a href="/nav">NAVLINK</a></nav>
<article><h1>Heading One</h1>
<p>Body paragraph with <a href="rel/deep?q=1">RELLINK</a> inside it.</p>
<pre><code class="language-python">print("hi")</code></pre>
<table><thead><tr><th>k</th><th>v</th></tr></thead>
<tbody><tr><td>a</td><td>b</td></tr></tbody></table>
</article></body></html>"""

_BS_HOME = Path(__file__).resolve().parent / "_bs_home" / "extension"
_LEDGER = _BS_HOME / "sessions" / "ledger.json"


_JSON = b'{"not": "html"}'


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # A real served response, not a `data:` URL — the extension backend
        # cannot navigate to `data:` at all, so that would fail as a page load
        # long before the content-type check under test.
        json_route = self.path.startswith("/json")
        body, ctype = ((_JSON, "application/json") if json_route
                       else (_PAGE, "text/html"))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def page_url():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()


def _run_markdown(args: list[str], runtime_dir: str, timeout: float = 180.0):
    """Invoke the CLI the way a caller would.

    Deliberately does NOT seed `BD_SESSION` the way `helpers.run_skill` does:
    minting and disposing of its own session is the behaviour under test.
    """
    env = scrubbed_env()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BS_HOME"] = str(_BS_HOME)
    env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
    binary = Path(sys.executable).with_name("browserwright")
    return subprocess.run(
        [str(binary), "markdown", *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _sessions() -> dict:
    try:
        return json.loads(_LEDGER.read_text(encoding="utf-8")).get("sessions", {})
    except (OSError, ValueError):
        return {}


def test_markdown_command_renders_and_cleans_up(ext_ready, e2e_daemon, page_url):
    before = set(_sessions())

    proc = _run_markdown(
        [page_url, "--mode=full", "--name=e2e-markdown"],
        e2e_daemon.runtime_dir,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    md = proc.stdout
    # Content, on stdout, verbatim (mode=full keeps the nav the converter's own
    # default would have deleted).
    assert "# Heading One" in md
    assert "NAVLINK" in md
    # Absolute links — the thing no Python-side converter can produce.
    assert f"{page_url}rel/deep?q=1" in md
    # Fidelity that motivated this converter choice.
    assert "```python" in md
    assert "| --- | --- |" in md

    # Metadata out-of-band, never in the body (ADR-0007).
    assert "rendered as full" in proc.stderr
    assert "rendered as" not in md

    # And the throwaway session is gone: no ledger row left behind, which on
    # this backend means no orphan tab group in the browser either.
    assert set(_sessions()) == before, (
        f"throwaway session leaked into the ledger: "
        f"{set(_sessions()) - before}\nstderr:\n{proc.stderr}"
    )


def test_markdown_command_auto_mode_extracts_and_reports_which_tier(
    ext_ready, e2e_daemon, page_url,
):
    """`auto` must say which tier it landed on — the agent cannot tell from the
    markdown alone whether it got the article or the whole page."""
    proc = _run_markdown([page_url], e2e_daemon.runtime_dir)
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    assert "Heading One" in proc.stdout
    assert any(tier in proc.stderr
               for tier in ("rendered as article", "rendered as stripped")), \
        f"auto mode did not report its tier:\n{proc.stderr}"


def test_markdown_command_refuses_non_html_and_still_cleans_up(
    ext_ready, e2e_daemon, page_url,
):
    """A non-HTML URL must fail loudly AND still tear its session down — the
    `finally` matters most on the error path."""
    before = set(_sessions())
    proc = _run_markdown([page_url + "json"], e2e_daemon.runtime_dir)

    assert proc.returncode != 0
    assert "not HTML" in proc.stderr, f"stderr:\n{proc.stderr}"
    assert "application/json" in proc.stderr
    assert not proc.stdout.strip(), "refusal must not print a partial body"
    assert set(_sessions()) == before, (
        f"session leaked on the refusal path: {set(_sessions()) - before}"
    )
