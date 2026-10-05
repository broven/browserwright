"""`browserwright search <query>` end to end, attached to a CDP browser.

`--attach` is the shape production uses (a remote CloakBrowser behind a CDP
forwarder), so that is what is driven here: a real Chrome on the test CDP port,
a fixture HTTP server standing in for Google with Google's markup — including
the opaque `/goto?url=<token>` link a signed-out browser gets, which only a 302
resolves — and the composition under test: throwaway session → executor →
navigate → extract → resolve goto → render → session torn down.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from .conftest import TEST_CDP_PORT, TEST_EXT_PORT, published_endpoint, scrubbed_env

_BS_HOME = Path(__file__).resolve().parent / "_bs_home" / "cdp"
_LEDGER = _BS_HOME / "sessions" / "ledger.json"


def _serp(port: int) -> bytes:
    return f"""<!doctype html><html><head><title>q - Google Search</title></head><body>
<div id="search">
  <div data-hveid="1"><a href="/goto?url=CAESzephyr"><h3>The Zephyr-9 valve: a history</h3></a>
    <div data-sncf="1">Mar 5, 2025 — How a small pressure valve ended up in every espresso machine.</div></div>
  <div data-hveid="2"><a href="http://127.0.0.1:{port}/direct"><h3>Valves for beginners</h3></a>
    <div data-sncf="1">A general introduction to pressure valves and how they work.</div></div>
</div>
<div data-attrid="title">Zephyr-9</div><div data-attrid="subtitle">Pressure valve</div>
<div data-q="who invented the zephyr-9 valve"></div>
<div id="botstuff"><a href="/search?q=zephyr+patent">zephyr valve patent</a><a href="/search?q=x&start=10">2</a></div>
</body></html>""".encode()


_EMPTY = b"""<!doctype html><title>q - Google Search</title><body><div id="search">
<p>Your search - <b>zzqx</b> - did not match any documents.</p></div></body>"""
_CAPTCHA = b"""<!doctype html><title>Sorry</title><body>Our systems have detected unusual
traffic from your computer network. <div class="g-recaptcha"></div></body>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query).get("q", [""])[0]
        port = self.server.server_address[1]
        if u.path == "/goto":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/articles/zephyr-9")
            self.end_headers()
            return
        if u.path == "/search":
            body = _EMPTY if q == "zzqx" else _CAPTCHA if q == "blocked" else _serp(port)
        else:
            body = b"<!doctype html><title>page</title><p>page</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def engine():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _run_search(args: list[str], runtime_dir: str, timeout: float = 120.0):
    """Invoke the CLI the way a caller would; it mints its own session, so no
    ledger record is seeded (unlike `helpers.run_skill`)."""
    env = scrubbed_env()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BW_DAEMON_URL"] = published_endpoint(runtime_dir) or ""
    env["BS_HOME"] = str(_BS_HOME)
    env["BD_CDP_PORT"] = str(TEST_CDP_PORT)
    env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)  # never the production relay
    env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
    assert env["BW_DAEMON_URL"], "test cdp daemon did not publish its endpoint"
    binary = Path(sys.executable).with_name("browserwright")
    return subprocess.run([str(binary), "search", *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def _sessions() -> dict:
    try:
        return json.loads(_LEDGER.read_text(encoding="utf-8")).get("sessions", {})
    except (OSError, ValueError):
        return {}


def _args(engine: str, query: str, *extra: str) -> list[str]:
    return [query, f"--attach={TEST_CDP_PORT}",
            f"--search-url={engine}/search?q={{query}}&num={{limit}}", *extra]


def test_search_command_returns_ranked_links_and_cleans_up(e2e_cdp_daemon, engine):
    before = set(_sessions())
    proc = _run_search(_args(engine, "zephyr-9 valve inventor", "--json"), e2e_cdp_daemon)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    payload = json.loads(proc.stdout)
    rows = payload["results"]
    assert [r["position"] for r in rows] == [1, 2]
    # The goto link was resolved to where its 302 points, in-browser.
    assert rows[0]["url"] == f"{engine}/articles/zephyr-9"
    assert rows[0]["date"] == "Mar 5, 2025"
    assert rows[0]["snippet"].startswith("How a small pressure valve")
    assert rows[1]["url"] == f"{engine}/direct"
    assert payload["knowledgeGraph"]["title"] == "Zephyr-9"
    assert payload["peopleAlsoAsk"] == ["who invented the zephyr-9 valve"]
    # Pagination ("2") is not a related search.
    assert payload["relatedSearches"] == ["zephyr valve patent"]
    assert payload["noMatch"] is False
    assert "search: 2 results" in proc.stderr

    assert set(_sessions()) == before, f"throwaway session leaked: {set(_sessions()) - before}"


def test_search_command_text_output_is_agent_readable(e2e_cdp_daemon, engine):
    proc = _run_search(_args(engine, "zephyr-9 valve", "--limit=1"), e2e_cdp_daemon)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith("# 1 results for 'zephyr-9 valve'")
    assert f"1. The Zephyr-9 valve: a history\n   {engine}/articles/zephyr-9" in out
    assert "Valves for beginners" not in out  # --limit honoured
    assert "## Knowledge panel\ntitle: Zephyr-9\ntype: Pressure valve" in out
    assert "/goto?" not in out


def test_search_command_reports_an_honest_empty_result(e2e_cdp_daemon, engine):
    proc = _run_search(_args(engine, "zzqx", "--json"), e2e_cdp_daemon)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["results"] == [] and payload["noMatch"] is True


def test_search_command_fails_loudly_on_a_captcha_and_cleans_up(e2e_cdp_daemon, engine):
    before = set(_sessions())
    proc = _run_search(_args(engine, "blocked"), e2e_cdp_daemon)
    assert proc.returncode == 5, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "captcha" in proc.stderr.lower()
    assert proc.stdout.strip() == ""
    assert set(_sessions()) == before
