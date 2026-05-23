"""Multi-task fan-out (v0.3).

Runs N tasks concurrently. Each one gets its own ``Session`` (and therefore
its own ws to the daemon, its own sessionId namespace, its own
``current_target_id``). The daemon v0.3 multi-client mux serialises traffic
into the single upstream Chrome ws; from Skill's point of view the tasks
are truly independent — `new_tab()` in task A doesn't yank the tab task B
is operating on.

This module is intentionally small. The hard work was done in #55 (the
``ContextVar``-backed ``with_session`` machinery). Here we just iterate.

Concurrency model
-----------------
Primitives are sync. The CDP transport is thread-safe (single ``send`` lock).
So we use a ``ThreadPoolExecutor`` rather than asyncio:

  - Each worker thread enters ``with_session(Session())`` and runs the task.
  - Sessions are independent ``ContextVar`` slots (#55 covers thread isolation).
  - Daemon assigns each ws its own client id, so per-thread sessionIds don't
    collide on the wire either.

Layer 3 (cron / scheduler) shells out to either ``browserwright task ...``
one-at-a-time or — for bursty work — calls this helper from Python::

    from browserwright.multitask import run_tasks_concurrent
    rows = run_tasks_concurrent([
        ("ycombinator.com", "front_page", {"limit": 10}),
        ("wikipedia.org",         "lookup",     {"title": "Python"}),
    ], max_workers=4)
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Callable, Iterable, Optional

from .errors import BrowserSkillError
from .session import Session, with_session
from .task_runner import run_task


TaskSpec = tuple[str, str, dict]   # (site, name, kwargs)


class TaskResult(dict):
    """Single fan-out result. Acts as a dict for JSON friendliness::

        {"site": "...", "name": "...", "ok": True/False,
         "value": <return>,             # only when ok
         "error_type": "ClassName",     # only when not ok
         "error_msg": "...",            # only when not ok
         "elapsed_sec": float}
    """


def _run_one(spec: TaskSpec) -> TaskResult:
    """Worker: build a fresh ``Session``, push it onto the ContextVar, run.

    Each worker owns its CDP transport. We close it on exit so the daemon's
    client slot is freed promptly. Daemon v0.3 doesn't enforce a single-client
    cap, but releasing eagerly still helps the daemon's idle policy + uiState
    accounting stay accurate.
    """
    import time
    site, name, kwargs = spec
    t0 = time.monotonic()
    sess = Session()
    try:
        with with_session(sess):
            value = run_task(site, name, **kwargs)
    except BrowserSkillError as e:
        return TaskResult(
            site=site, name=name, ok=False,
            error_type=type(e).__name__, error_msg=str(e),
            elapsed_sec=round(time.monotonic() - t0, 3),
        )
    except Exception as e:  # noqa: BLE001 — agent-facing catch-all
        return TaskResult(
            site=site, name=name, ok=False,
            error_type=type(e).__name__, error_msg=str(e),
            elapsed_sec=round(time.monotonic() - t0, 3),
        )
    finally:
        sess.close()
    return TaskResult(
        site=site, name=name, ok=True, value=value,
        elapsed_sec=round(time.monotonic() - t0, 3),
    )


def run_tasks_concurrent(specs: Iterable[TaskSpec], *,
                         max_workers: int = 4,
                         warm_upstream: bool = False) -> list[TaskResult]:
    """Run every (site, name, kwargs) tuple concurrently. Returns one
    ``TaskResult`` per spec, in input order.

    Exceptions never propagate out — each failure becomes an ``ok=False``
    result. Layer 3 examines results and decides what to retry/log.

    .. deprecated:: 0.3.0
       The ``warm_upstream`` keyword is a no-op since the daemon shipped
       the #76 pre-open buffer fix. The earlier Skill-side workaround
       (sync probe on the main session before spawning workers) is no
       longer needed: the daemon now per-client-buffers frames received
       while ``upstream phase != CONNECTED`` and replays them after the
       upstream ws opens (``PRE_OPEN_BUFFER_LIMIT=100``; overflow surfaces
       as JSON-RPC error ``-32603``). The keyword is accepted for source
       compatibility but has no effect; remove the argument from your
       call site.

       **Removal target: v0.6** (REVIEW.md F-17). After v0.6 ships the
       keyword will be removed from the signature and any caller still
       passing it will hit a ``TypeError``.

    Notes
    -----
    * ``max_workers`` defaults to 4. Daemon v0.3 multi-client supports more
      but Chrome itself gets stressed past ~8 concurrent navigations.
    * The order of results matches the order of inputs (not completion
      order) — predictable for the caller's downstream pipeline.
    """
    _ = warm_upstream  # accepted-but-ignored; see deprecation note above.
    specs = list(specs)
    if not specs:
        return []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(max_workers, len(specs)),
        thread_name_prefix="bs-task",
    ) as pool:
        futures = [pool.submit(_run_one, s) for s in specs]
        return [f.result() for f in futures]
