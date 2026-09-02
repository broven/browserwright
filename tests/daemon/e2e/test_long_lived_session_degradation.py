"""Does ONE extension session degrade after hundreds of `page.goto()` calls?

Field report: a long-lived extension session that had run hundreds of batches
started failing 100% of `x.com` navigations while `b23.tv` in the SAME session
still worked, and a BRAND NEW session loaded the same x.com URL immediately.
Not the network, not the site, not concurrency — something accumulates per
session/tab, and it hurts some pages and not others.

The two structural properties that separate the failing sites (x.com,
v.douyin.com, zhuanlan.zhihu.com) from the working one (b23.tv, a bare redirect
page) are cross-site subframes (OOPIFs) and service workers. Both are exactly
what `armAutoAttach` in `chrome-extension/background.js` arms
`Target.setAutoAttach{waitForDebuggerOnStart:true, filter:[{}]}` for — every
such child target is paused by Chromium until the extension resumes it.

So this test drives ONE session through many navigations across four page
flavors and reports the failure rate PER FLAVOR and OVER TIME. It is served
entirely from a local HTTP server (plus, for the OOPIF flavor, real cross-site
iframes), so a failure here is by construction not the site blocking us.

Marked `slow`: it is an experiment/diagnostic, not part of the fast gate.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from .helpers import run_skill

# The session runs BATCHES, one `browserwright -s <sid>` invocation each, the
# way the field crawl did: the per-session executor is resident, so `page` and
# the tab survive across batches while a single call stays under the executor's
# 90s request deadline.
BATCHES = 20
PER_BATCH = 4  # one of each flavor => 80 navigations in one long-lived session
OOPIF_SRC = "https://example.com/"

_SW_JS = b"self.addEventListener('fetch', (e) => {});\n"

_FLAVORS = ("plain", "oopif", "sw", "mixed")


def _page_html(flavor: str, n: int) -> bytes:
    frames = ""
    if flavor in ("oopif", "mixed"):
        frames = "".join(
            f'<iframe src="{OOPIF_SRC}?i={n}-{k}" width=50 height=50></iframe>'
            for k in range(4)
        )
    sw = ""
    if flavor in ("sw", "mixed"):
        sw = (
            "<script>navigator.serviceWorker &&"
            " navigator.serviceWorker.register('/sw.js').catch(()=>{})</script>"
        )
    return (
        f"<!doctype html><title>bw {flavor} {n}</title>"
        f"<main id=v>{flavor}-{n}</main>{frames}{sw}"
    ).encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/sw.js":
            body, ctype = _SW_JS, "application/javascript"
        else:
            parts = path.strip("/").split("/")
            flavor = parts[0] if parts and parts[0] in _FLAVORS else "plain"
            n = parts[1] if len(parts) > 1 else "0"
            body, ctype = _page_html(flavor, n), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture
def local_site():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


_SCRIPT = r'''
import json, time
URLS = __URLS__
BASE = __BASE__
rows = []
for j, u in enumerate(URLS):
    i = BASE + j
    t0 = time.time()
    try:
        page.goto(u, timeout=30000)
        rows.append({"i": i, "url": u, "ok": True,
                     "ms": int((time.time() - t0) * 1000)})
    except BaseException as exc:
        rows.append({"i": i, "url": u, "ok": False,
                     "ms": int((time.time() - t0) * 1000),
                     "type": type(exc).__name__,
                     "reason": getattr(exc, "reason", ""),
                     "detail": getattr(exc, "detail", "") or str(exc)[:300]})
print("BWROWS " + json.dumps(rows))
'''


def _seed(bs_home: Path, sid: str) -> None:
    sessions_dir = bs_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ledger = sessions_dir / "ledger.json"
    data = json.loads(ledger.read_text()) if ledger.exists() else {
        "next_id": 1, "sessions": {}}
    now = time.time()
    data["sessions"][sid] = {
        "id": sid, "backend": "extension", "workspace": None,
        "owner": "attach", "name": sid, "created_at": now, "last_seen": now,
    }
    ledger.write_text(json.dumps(data), encoding="utf-8")


def _rows(result) -> list[dict]:
    for line in result.stdout.splitlines():
        if line.startswith("BWROWS "):
            return json.loads(line[len("BWROWS "):])
    raise AssertionError(
        f"no BWROWS line; rc={result.returncode}\n"
        f"stdout={result.stdout[-2000:]!r}\nstderr={result.stderr[-3000:]!r}")


def _summarize(rows: list[dict]) -> dict:
    fails = [r for r in rows if not r["ok"]]
    by_flavor: dict[str, dict] = {}
    for r in rows:
        flavor = r["url"].rsplit("/", 2)[-2]
        slot = by_flavor.setdefault(flavor, {"n": 0, "fail": 0, "ms": []})
        slot["n"] += 1
        slot["ms"].append(r["ms"])
        if not r["ok"]:
            slot["fail"] += 1
    for slot in by_flavor.values():
        ms = sorted(slot.pop("ms"))
        slot["median_ms"] = ms[len(ms) // 2] if ms else 0
        slot["max_ms"] = ms[-1] if ms else 0
    quarters = [0, 0, 0, 0]
    for r in fails:
        quarters[min(3, r["i"] * 4 // max(1, len(rows)))] += 1
    per_quarter_ms = [[], [], [], []]
    for r in rows:
        per_quarter_ms[min(3, r["i"] * 4 // max(1, len(rows)))].append(r["ms"])
    medians = [sorted(q)[len(q) // 2] if q else None for q in per_quarter_ms]
    return {
        "median_ms_per_quarter": medians,
        "total": len(rows),
        "failed": len(fails),
        "by_flavor": by_flavor,
        "failures_per_quarter": quarters,
        "first_failure_index": fails[0]["i"] if fails else None,
        "distinct_details": sorted({f["detail"] for f in fails})[:12],
        "reasons": sorted({f["reason"] for f in fails}),
    }


@pytest.mark.slow
def test_one_session_survives_many_navigation_batches(ext_ready, e2e_daemon, local_site):
    """A single session must not get worse the longer it is used."""
    bs_home = Path(__file__).resolve().parent / "_bs_home" / "extension"
    sid = f"e2e-longlived-{uuid.uuid4().hex[:8]}"
    _seed(bs_home, sid)

    rows: list[dict] = []
    for batch in range(BATCHES):
        urls = [f"{local_site}/{flavor}/{batch}" for flavor in _FLAVORS][:PER_BATCH]
        script = (_SCRIPT
                  .replace("__URLS__", json.dumps(urls))
                  .replace("__BASE__", str(batch * PER_BATCH)))
        result = run_skill(script=script, backend="extension", timeout=180,
                           runtime_dir=e2e_daemon.runtime_dir,
                           extra_env={"BD_SESSION": sid,
                                      "BW_PAGE_BIND_TIMEOUT": "30"})
        rows.extend(_rows(result))
    report = _summarize(rows)
    print("DEGRADATION REPORT " + json.dumps(report, indent=2))

    assert report["failed"] == 0, (
        f"{report['failed']}/{report['total']} navigations failed in ONE "
        f"long-lived session against a LOCAL server: {report}"
    )
