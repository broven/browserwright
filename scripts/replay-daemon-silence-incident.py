#!/usr/bin/env python3
"""Replay the 2026-10-19 daemon incident — and show it no longer reproduces.

What happened that day: a machine-global daemon that was *alive but momentarily
slow* was SIGTERMed two seconds after three concurrent CLI calls missed two 1.5s
``/__ping__`` probes. The daemon was never wedged — it was still accepting ws
clients and it shut down gracefully on the signal — but one negative short probe
was all the evidence the port reclaim needed.

Two questions the incident turned on, asked here for four endpoint shapes:

1. what one 1.5s probe says — the only evidence the old code acted on, and
2. what ``daemon_lifecycle.ensure`` concludes — the decision that starts or
   stops a daemon.

Expected: the slow endpoint reads as ``timeout`` to the single probe and ``up``
to ``ensure`` (no spawn, no stop); a silent holder is ``unresponsive`` (no
spawn, no stop); a healthy daemon costs nothing extra; and a genuinely dead
endpoint still spawns, because recovery must keep working.

Hermetic by construction: this script sets HOME-adjacent state aside *before*
importing the package, and it replaces the three side-effecting seams
(`daemon_endpoint`, `_spawn_detached`, `run_verb`) with fakes. It therefore
cannot contact, stop, or start the machine-global daemon. That is not
decoration: an earlier version of this replay called ``ensure()`` with the
default endpoint while running a dev checkout (version ``0.0.0``), which the
lifecycle correctly read as a *stale* daemon and replaced — killing the user's
live daemon. ADR-0012 is why the test suite has a wall for exactly this.

Usage: uv run python scripts/replay-daemon-silence-incident.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time

# --- isolate before importing the package (ADR-0012) ------------------------
# The daemon log, the sessions ledger and the default config file all resolve
# from these; pointing them at a scratch dir keeps a replay from writing into
# the real browserwright home.
_SCRATCH = tempfile.mkdtemp(prefix="bw-replay-")
os.environ["BS_HOME"] = _SCRATCH
os.environ["TMPDIR"] = _SCRATCH

from browserwright import daemon_lifecycle as lifecycle  # noqa: E402
from browserwright.daemon._ipc import probe_endpoint_sync  # noqa: E402
from browserwright.daemon_url import DaemonEndpoint  # noqa: E402
from browserwright.version import package_version  # noqa: E402

SHORT_PROBE_S = 1.5
VERSION = package_version()


def fake_daemon(delay: float | None):
    """A listener answering /__ping__ after ``delay`` seconds, or never."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=answer, args=(conn,), daemon=True).start()

    def answer(conn) -> None:
        with conn:
            try:
                conn.recv(4096)
                if delay is None:
                    time.sleep(3600)  # hold the socket, never answer
                    return
                time.sleep(delay)
                body = json.dumps(
                    {"pong": True, "pid": 4242, "version": VERSION}
                ).encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
            except OSError:
                pass

    threading.Thread(target=accept_loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def probe_that_never_listens() -> int:
    """A port nothing is listening on (bound, then closed)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run_case(label, port, *, expect_state, expect_spawn, expect_stop,
             max_s=None) -> bool:
    endpoint = DaemonEndpoint(
        url=f"http://127.0.0.1:{port}", explicit=False, source="replay")

    started: list = []
    stopped: list = []
    saved = (lifecycle.daemon_endpoint, lifecycle._spawn_detached,
             lifecycle.run_verb)
    lifecycle.daemon_endpoint = lambda: endpoint
    lifecycle._spawn_detached = lambda args, **_kw: (started.append(args), None)[1]
    lifecycle.run_verb = lambda args, **_kw: (stopped.append(args), None)[1]
    try:
        began = time.monotonic()
        short = probe_endpoint_sync("127.0.0.1", port, timeout=SHORT_PROBE_S)
        short_ms = (time.monotonic() - began) * 1000
        began = time.monotonic()
        verdict = lifecycle.ensure(f"replay {label}")
        elapsed = time.monotonic() - began
    finally:
        (lifecycle.daemon_endpoint, lifecycle._spawn_detached,
         lifecycle.run_verb) = saved

    ok = (
        verdict.state == expect_state
        and bool(started) == expect_spawn
        and bool(stopped) == expect_stop
        and (max_s is None or elapsed <= max_s)
    )
    print(f"  {label}")
    print(f"    one short probe  -> {short.kind:12s} ({short_ms:6.0f}ms)"
          "   <- all the evidence the old code had")
    print(f"    ensure()         -> {verdict.state:12s} ({elapsed:5.2f}s)   "
          f"spawn={'yes' if started else 'no':3s} stop={'yes' if stopped else 'no'}")
    print(f"    {'PASS' if ok else 'FAIL'}   expected state={expect_state}, "
          f"spawn={'yes' if expect_spawn else 'no'}, "
          f"stop={'yes' if expect_stop else 'no'}")
    print()
    return ok


def main() -> int:
    print("Replaying the 2026-10-19 incident: the daemon was slow, not dead.\n")
    results = []

    # The incident: alive, answers in 3s. One short probe times out — the old
    # code called that FOREIGN, replaced the daemon, and the replacement's port
    # reclaim SIGTERMed it on one more negative ping.
    srv, port = fake_daemon(delay=3.0)
    results.append(run_case("slow (3s) but alive — the incident", port,
                            expect_state="up", expect_spawn=False,
                            expect_stop=False))
    srv.close()

    # Not slow — silent. Still must not be replaced, and must not be stopped.
    srv, port = fake_daemon(delay=None)
    results.append(run_case("accepts the connection, never answers", port,
                            expect_state="unresponsive", expect_spawn=False,
                            expect_stop=False))
    srv.close()

    # Healthy: no escalation tax.
    srv, port = fake_daemon(delay=0.0)
    results.append(run_case("healthy (no escalation cost)", port,
                            expect_state="up", expect_spawn=False,
                            expect_stop=False, max_s=0.5))
    srv.close()

    # Genuinely dead: recovery must still work.
    results.append(run_case("nothing listening (truly gone)", probe_that_never_listens(),
                            expect_state="down", expect_spawn=True,
                            expect_stop=False, max_s=0.5))

    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
