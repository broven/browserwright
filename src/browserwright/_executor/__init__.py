"""Phase B: the persistent per-session executor.

A resident, per-session **sync** subprocess (``python -m browserwright._executor
--session <id>``) that holds live Playwright ``page`` / ``context`` / ``browser``
+ a persistent ``state`` dict + one long-lived facade ``connect_over_cdp``
connection for its whole lifetime. The ``browserwright <<'PY' … PY`` heredoc CLI
degrades to a **thin client**: it ships the code body to the session's executor,
which runs it in a namespace where ``page`` / ``context`` / ``state`` are the
LIVE persistent objects, and returns the result.

Why a separate subprocess (not a thread in the asyncio daemon):
  - sync Playwright is thread-affine and can't run on the daemon's event loop;
  - agent code (infinite loop / segfault) crashing the privileged daemon — which
    manages the user's real browser — is an unacceptable blast radius.
A per-session subprocess crashes only itself (D1 of the task).

Transport (Fork 2): the daemon owns the LIFECYCLE (spawn/discover via the
``ensureExecutor`` verb + an ``_ipc`` discovery file); the executor owns the
DATA PLANE — its own per-session unix socket speaking a simple length-framed
request/response of our design (``protocol.py``). The thin client connects
directly to that socket, keeping arbitrary code + large output OFF the daemon's
critical path.

Concurrency (Fork 3): a single dedicated worker thread owns the sync-Playwright
objects (thread-affine); the accept loop enqueues ``{code, timeout}`` requests
and the worker drains them FIFO (serial queue).

Status: PR1 (process skeleton + data plane), PR2 (daemon-side supervision —
idle reap / endSession kill / crash reap / orphan sweep), and PR3 (``reset()`` +
full output protocol: warnings / screenshots / truncation / traceback-bearing
errors + per-call timeout enforcement) are all in place.
"""
from __future__ import annotations

from .protocol import (
    ExecuteRequest,
    ExecuteResponse,
    recv_message,
    send_message,
)

__all__ = [
    "ExecuteRequest",
    "ExecuteResponse",
    "recv_message",
    "send_message",
]
