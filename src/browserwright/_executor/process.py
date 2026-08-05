"""The resident per-session executor process.

Launched as ``python -m browserwright._executor --session <id>``. On cold start
it binds the session env, connects the facade once (``connect_over_cdp``), binds
the session's current tab, and then serves a per-session unix socket: each
inbound :class:`~browserwright._executor.protocol.ExecuteRequest` is enqueued
and a single dedicated worker thread runs the code FIFO in a namespace where
``page`` / ``context`` are the LIVE held objects and ``state`` is a persistent
dict injected by reference.

Thread model (Fork 3): sync Playwright is thread-affine — the thread that did
``connect_over_cdp`` MUST be the thread that touches the browser objects. So the
WORKER thread owns connect+bind+exec; the MAIN thread runs the accept loop and
hands work over a queue.

Startup ordering (control-plane decoupling): the socket is bound + the discovery
file is written + the accept loop starts IMMEDIATELY — BEFORE any
``connect_over_cdp``. The slow facade cold-start (which can take 10–35s on a
daemon-restart race) happens LAZILY on the worker thread, triggered by the FIRST
execute request, NOT during process startup. This keeps ``ensureExecutor``'s
control-plane RPC fast (it only waits for the socket to be listening, ~sub-second)
so it never trips the daemon websockets keepalive/timeout. The cold-start latency
is absorbed by the client's own blocking data-plane call, which has no keepalive
and a generous client-owned wait.
"""
from __future__ import annotations

import ast
import io
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from typing import Any

from ..errors import BrowserwrightError, serialize
from .protocol import (
    MAX_TEXT_CHARS,
    TERMINAL_DEADLINE_EXCEEDED,
    TERMINAL_RESET_REQUESTED,
    ExecuteRequest,
    ExecuteResponse,
    recv_message,
    send_message,
)

# Cold-start facade-connect retry (Failure #4): a daemon-restart cold-start can
# race the lazily-launched rdp Chrome. ~13 attempts × 0.75s ≈ 10s of backoff
# absorbs the startup window without wedging cold-start forever (the spawn-ready
# timeout in the registry is 35s, comfortably above this).
_COLD_START_CONNECT_ATTEMPTS = 13
_COLD_START_CONNECT_BACKOFF_S = 0.75


@contextmanager
def _request_environment(env: dict[str, str]) -> Iterator[None]:
    """Temporarily overlay request-selected values on ``os.environ``.

    The resident executor must not retain request credentials. Snapshot only
    the selected keys, then restore each key exactly even if user code mutates
    or deletes it, raises, or exits via ``SystemExit``.
    """
    original: dict[str, tuple[bool, str | None]] = {
        name: (name in os.environ, os.environ.get(name)) for name in env
    }
    try:
        os.environ.update(env)
        yield
    finally:
        for name, (was_present, value) in original.items():
            if not was_present:
                os.environ.pop(name, None)
            else:
                # Present environment entries are always strings.
                os.environ[name] = value or ""


class _LivePageHolder:
    """Adapts a live Playwright ``page`` to the ``.page`` attribute
    ``snapshot.make_snapshot`` expects (it was written against the lazy
    ``PlaywrightHandle``). We rebind ``.page`` whenever the worker re-binds the
    session tab (cold-start / future ``reset()``), so ``snapshot()`` always
    observes the executor's current live page."""

    def __init__(self) -> None:
        self.page: Any = None


class _ResetRequested(BaseException):
    """Private control-flow signal for terminal ``reset()``.

    It inherits directly from ``BaseException`` so user code's common
    ``except Exception`` cannot accidentally swallow the reset request and
    continue issuing browser actions on a transport scheduled for recycle.
    """


class _Worker:
    """Owns the live Playwright objects + persistent ``state``; runs code FIFO.

    All Playwright access happens on THIS thread (thread-affinity). The main
    thread enqueues ``(request, reply_box)`` pairs; the worker connects on
    cold-start, then drains the queue."""

    def __init__(self, session_id: str, executor_id: str | None = None) -> None:
        self._session_id = session_id
        self._executor_id = executor_id
        self._q: "queue.Queue[tuple[ExecuteRequest, queue.Queue[ExecuteResponse]] | None]" = queue.Queue()
        # Whether cold-start (connect_over_cdp + bind) has completed. Cold-start
        # is LAZY: it runs on the worker thread on the FIRST execute request, not
        # at process start (so the control-plane `ensureExecutor` RPC returns the
        # moment the socket is listening, never waiting on the ~10-35s connect).
        self._connected = False

        # Live Playwright objects (held for the worker's lifetime).
        self._pw_cm: Any = None
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        # The currently-armed facade-death handler (Fork 4).
        self._facade_death_handler: Any = None
        # Persistent per-session state, injected by reference each call (Fork 5).
        self._state: dict[str, Any] = {}
        self._snapshot_holder = _LivePageHolder()
        self._snapshot: Any = None
        # Per-call warnings/screenshots the running code can append to via the
        # injected `_bw_warn` / screenshot helpers. Reset at the start of each
        # _execute; collected into the ExecuteResponse.
        self._call_warnings: list[str] = []
        self._call_screenshots: list[dict[str, Any]] = []
        # C1b: what this worker is running RIGHT NOW, and since when. `None`
        # when idle. Read in-process (a SIGUSR1 dump lands next to it) and
        # published to `_ipc.executor_inflight_path` so the daemon's `status`
        # verb can answer "worker is 47s into a task" for a worker that is too
        # busy — or too wedged — to answer an RPC. Monotonic, never wall clock.
        self._inflight: dict[str, Any] | None = None
        self._inflight_lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._run, name="bw-executor-worker", daemon=True)

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def submit(self, req: ExecuteRequest) -> ExecuteResponse:
        """Enqueue a request and block for its response, ENFORCING the per-call
        timeout (Fork 3 + PR3).

        The worker thread is thread-affine to the sync-Playwright driver, so a
        running call cannot be interrupted safely in-process.  The main thread
        instead bounds the wait across queueing, cold-start, and user code.  On
        expiry it returns a terminal response; the serve loop flushes it and
        exits the whole executor process, which lets the OS tear down the
        Playwright transport without touching browser tabs."""
        box: "queue.Queue[ExecuteResponse]" = queue.Queue(maxsize=1)
        self._q.put((req, box))
        try:
            return box.get(timeout=max(req.timeout_ms, 1) / 1000.0)
        except queue.Empty:
            return ExecuteResponse(
                error={
                    "type": "TimeoutError",
                    "msg": (
                        f"executor request deadline exceeded "
                        f"({req.timeout_ms}ms); the executor was recycled"
                    ),
                    "fix": (
                        "retry the command; browser tabs were preserved, but "
                        "executor state and Python finally blocks were not"
                    ),
                },
                exit_code=3,
                terminal_reason=TERMINAL_DEADLINE_EXCEEDED,
            )

    def shutdown(self) -> None:
        self._q.put(None)

    # ---- worker thread --------------------------------------------------

    def _run(self) -> None:
        """Worker loop. Cold-start is NOT done here — it is deferred to the
        first execute (see :meth:`_execute` → :meth:`_ensure_cold_started`), so
        the process can start serving its socket immediately while the slow
        facade connect happens off the control-plane RPC's critical path."""
        while True:
            item = self._q.get()
            if item is None:
                break
            req, box = item
            self._begin_inflight(req)
            try:
                response = self._execute(req)
            finally:
                self._end_inflight()
                # Values are restored out of os.environ by
                # `_request_environment`; also drop the request mapping before
                # this resident thread blocks on its next queue read.  Python
                # cannot promise physical erasure of immutable strings, but it
                # need not retain our explicit references.
                req.env.clear()
            box.put(response)
        self._teardown()

    # ---- in-flight introspection (C1b) ----------------------------------

    def _begin_inflight(self, req: ExecuteRequest) -> None:
        """Record + publish the call about to run.

        Deliberately records no code *body*: the sidecar lands in a shared
        runtime dir and heredocs routinely carry credentials. A sha256 prefix is
        enough to correlate a stuck worker with the call the agent sent."""
        import hashlib

        if req.task is not None:
            what = f"task:{req.task.site}/{req.task.name or '?'}"
        else:
            what = "code"
        entry = {
            "session": self._session_id,
            "executor_id": self._executor_id,
            "pid": os.getpid(),
            "what": what,
            "code_sha": hashlib.sha256(
                (req.code or "").encode("utf-8", "replace")).hexdigest()[:12],
            "code_chars": len(req.code or ""),
            "timeout_ms": req.timeout_ms,
            "started_at": time.monotonic(),
            "started_wall": time.time(),
            "connected": self._connected,
        }
        with self._inflight_lock:
            self._inflight = entry
        self._publish_inflight(entry)

    def _end_inflight(self) -> None:
        with self._inflight_lock:
            self._inflight = None
        self._publish_inflight(None)

    def _publish_inflight(self, entry: dict[str, Any] | None) -> None:
        from ..daemon import _ipc

        if entry is None:
            _ipc.write_executor_inflight(self._session_id, None)
            return
        # `started_at` is monotonic and meaningless to another process; publish
        # the wall-clock twin instead and let the reader age it.
        published = {k: v for k, v in entry.items() if k != "started_at"}
        _ipc.write_executor_inflight(self._session_id, published)

    def inflight_snapshot(self) -> dict[str, Any] | None:
        """The in-process view: what this worker is running, plus its age."""
        with self._inflight_lock:
            entry = dict(self._inflight) if self._inflight is not None else None
        if entry is None:
            return None
        entry["elapsed_s"] = round(
            max(0.0, time.monotonic() - entry["started_at"]), 3)
        return entry

    def _ensure_cold_started(self) -> None:
        """Lazily connect the facade + bind the session's current tab — ON the
        first execute (worker thread), NOT at process start.

        Idempotent: a no-op once connected (subsequent executes reuse the live
        objects). Raises on a failed connect; the caller (:meth:`_execute`)
        turns that into an actionable error response so the agent sees it. A
        failed cold-start leaves ``_connected`` False, so the NEXT execute retries the connect —
        matching the facade-death cold-restart discipline.

        Reuses the shared connect+bind free functions (single source of truth
        for the FATAL "no Playwright CDP session over the extension facade"
        constraint). Bind the session FIRST so ``current_session()`` /
        ``resolve_current_target()`` resolve the right ledger record."""
        if self._connected:
            return
        from ..session import Session, set_session
        from ..session_ctx import resolve_session

        # Bind the session from the ledger (same as inline.py's entrypoint).
        rec = resolve_session(self._session_id)
        set_session(Session(record=rec))
        # Enter the sync_playwright() driver ONCE — on the FIRST cold-start only.
        # On a retry after a failed connect we REUSE the live driver (just re-run
        # `_connect_and_bind` below), NEVER re-enter the manager: its event loop
        # is thread-bound and cannot be restarted once `__exit__`-ed
        # ("Event loop is closed").
        if self._pw_cm is None:
            self._start_driver()
        # Raises on a still-dead facade; `_connected` stays False so the next
        # execute retries the connect on the SAME live driver (or the process is
        # reaped and a fresh ensure cold-starts a new one). The driver is NOT
        # torn down here — only `_teardown` (process exit) exits the manager.
        self._connect_and_bind()
        self._connected = True

    def _start_driver(self) -> None:
        """Enter ``sync_playwright()`` ONCE (cold-start only).

        Playwright's sync driver runs a thread-bound asyncio event loop that
        CANNOT be restarted in the same thread once stopped — re-entering
        ``sync_playwright()`` on the worker after a prior ``__exit__`` yields
        "Event loop is closed." So the driver is entered exactly once here at
        The manager is ``__exit__``-ed only at :meth:`_teardown` (process exit).
        MUST run on the worker thread (Playwright sync is thread-affine)."""
        from playwright.sync_api import sync_playwright

        self._pw_cm = sync_playwright()
        self._pw = self._pw_cm.__enter__()

    def _connect_and_bind(self) -> None:
        """``connect_over_cdp`` to the facade on the live ``self._pw`` driver,
        bind the session's current tab, rebind ``snapshot``, and arm
        facade-death.

        It does not enter/exit the ``sync_playwright()`` manager — that is
        :meth:`_start_driver` / :meth:`_teardown`'s job.  MUST run on the worker
        thread (Playwright sync is thread-affine)."""
        from ..repl import playwright_handle as ph
        from ..repl.snapshot import make_snapshot
        from ..session import current_session

        # Failure #4 defense-in-depth: a daemon-restart cold-start races the rdp
        # Chrome that `ensureExecutor` is bringing up lazily — the facade can
        # 404/403 for a brief window while Chrome binds its CDP port. Retry a few
        # times over ~10s so the startup race doesn't hard-fail the executor.
        # `ensureExecutor` now also pre-launches Chrome before returning the exec
        # socket, so this is belt-and-suspenders, not the primary fix.
        sess = current_session()
        self._browser = ph.connect_over_cdp(
            self._pw, session_id=ph._session_id_from(sess),
            attempts=_COLD_START_CONNECT_ATTEMPTS,
            backoff_s=_COLD_START_CONNECT_BACKOFF_S)
        self._context = ph.context_for_browser(self._browser)
        self._page = ph.bind_current_page(self._context, sess)
        self._snapshot_holder.page = self._page
        self._snapshot = make_snapshot(self._snapshot_holder)
        self._arm_facade_death()

    def _reset(self) -> None:
        """Request terminal executor recycle without touching browser tabs.

        Reconnecting in-place used to abandon the old facade CDP transport,
        leaking one bridge per reset.  Reset is now terminal control flow:
        statements after ``reset()`` do not run, the response is flushed, and
        the thin client asks the daemon to reap this exact executor instance.
        The next command cold-starts and rebinds the session's current tab.
        """
        raise _ResetRequested()

    def _arm_facade_death(self) -> None:
        """Fork 4: when the facade transport drops (daemon restarted → facade ws
        gone), self-exit cleanly rather than live-reconnecting. The daemon's
        crash-reaper then drops us from the registry, and the NEXT heredoc
        cold-starts a fresh executor that re-binds the session's current tab via
        the ledger fast-path. `state` is LOST on this path — acceptable + by
        design (same as `reset()`); the cold restart is simpler + already
        correct vs patching a live connection.

        Playwright fires the browser `disconnected` event from its own driver
        thread; we hard-exit the process from there so the worker thread (which
        may be blocked in a queue.get) doesn't have to notice.

        The handler reference is retained for testability and lifecycle
        introspection."""
        def handler(*_):
            return self._on_facade_dead()

        try:
            self._browser.on("disconnected", handler)
            self._facade_death_handler = handler
        except Exception:  # noqa: BLE001 - never let arming break cold-start
            self._facade_death_handler = None

    def _on_facade_dead(self) -> None:
        sys.stderr.write(
            f"executor {self._session_id}: facade transport dropped "
            "(daemon restart?); self-exiting for cold restart\n")
        # os._exit so we don't run atexit/finalizers that might re-enter the
        # (now dead) Playwright driver and hang. The daemon reaps us + cleans
        # the discovery file; the next heredoc cold-starts a fresh executor.
        os._exit(0)

    def _execute(self, req: ExecuteRequest) -> ExecuteResponse:
        """Run one code blob in the persistent namespace, capturing stdout +
        the full PR3 output protocol (return value / warnings / screenshots /
        truncation / error-with-traceback).

        Runs on the worker thread (Playwright thread-affinity). The per-call
        timeout is enforced by :meth:`submit` on the executor's main thread;
        this method itself runs the code until completion or process exit.

        Cold-start (connect_over_cdp + bind) is performed HERE, lazily, on the
        first execute — so the slow connect is absorbed by this data-plane call,
        not the control-plane `ensureExecutor` RPC. A cold-start failure becomes
        an actionable error response (the agent should `reset()` / retry once
        the facade is up); `_connected` stays False so the next execute retries."""
        self._call_warnings = []
        self._call_screenshots = []
        try:
            self._ensure_cold_started()
        except BrowserwrightError as e:
            return self._finish(
                io.StringIO(), error=serialize(e), exit_code=e.exit_code)
        except BaseException as e:  # noqa: BLE001 - surface cold-start failure
            return self._finish(
                io.StringIO(),
                error={
                    "type": type(e).__name__,
                    "msg": f"executor cold-start failed: {e}",
                    "fix": ("the facade/browser was not reachable on first use; "
                            "retry the call (a fresh connect is attempted), or "
                            "end the session to recycle the executor if it "
                            "persists"),
                    "traceback": traceback.format_exc(),
                },
                exit_code=3)
        globals_ = self._build_globals()
        buf = io.StringIO()
        return_value: str | None = None
        task_result_json: str | None = None
        with _request_environment(req.env):
            try:
                with redirect_stdout(buf):
                    if req.task is None:
                        return_value = self._exec_with_return(req.code, globals_)
                    else:
                        result = self._run_task_on_live_surface(
                            req.task.site,
                            req.task.name,
                            isolated=req.task.isolated,
                            **req.task.args,
                        )
                        try:
                            return_value = repr(result)
                        except Exception:  # noqa: BLE001
                            return_value = (
                                f"<{type(result).__name__} (repr failed)>"
                            )
                        task_result_json = json.dumps(result, default=str)
            except _ResetRequested:
                return self._finish(
                    buf,
                    warnings=[
                        (
                            "executor reset requested; statements after "
                            "reset() were not run"
                        )
                    ],
                    terminal_reason=TERMINAL_RESET_REQUESTED,
                )
            except BrowserwrightError as e:
                return self._finish(buf, error=serialize(e), exit_code=e.exit_code)
            except SystemExit as e:
                code = int(e.code) if isinstance(e.code, int) else 0
                return self._finish(buf, exit_code=code)
            except BaseException as e:  # noqa: BLE001 - surface to the client
                # Restore traceback fidelity: the in-process path writes
                # `traceback.format_exc()` to stderr; a shipped heredoc must
                # show the SAME traceback. We carry it on the serialized error.
                from ..errors import playwright_error_fix

                error = {
                    "type": type(e).__name__,
                    "msg": str(e),
                    "traceback": traceback.format_exc(),
                }
                fix = playwright_error_fix(e)
                if fix:
                    error["fix"] = fix
                return self._finish(buf, error=error, exit_code=3)
        return self._finish(
            buf,
            return_value=return_value,
            task_result_json=task_result_json,
            exit_code=0,
        )

    @staticmethod
    def _exec_with_return(code: str, globals_: dict[str, Any]) -> str | None:
        """Exec ``code``; if the LAST statement is a bare expression, return its
        ``repr`` (playwriter's ``[return value]``), else None.

        We split the trailing expression off and ``eval`` it so its value is
        observable — a plain ``exec`` of an expression statement discards the
        value. Non-expression-ending code (assignments, loops, defs) returns
        None. A failed ``repr`` degrades to a type marker, never raising."""
        tree = ast.parse(code, "<executor>", "exec")
        last_value_expr: ast.Expression | None = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body.pop()
            last_value_expr = ast.Expression(last.value)  # type: ignore[attr-defined]
            ast.copy_location(last_value_expr, last)
        exec(compile(tree, "<executor>", "exec"), globals_)
        if last_value_expr is None:
            return None
        value = eval(compile(last_value_expr, "<executor>", "eval"), globals_)
        if value is None:
            return None
        try:
            return repr(value)
        except Exception:  # noqa: BLE001 - a hostile __repr__ must not crash us
            return f"<{type(value).__name__} (repr failed)>"

    def _finish(
        self,
        buf: io.StringIO,
        *,
        return_value: str | None = None,
        error: dict[str, Any] | None = None,
        exit_code: int = 0,
        warnings: list[str] | None = None,
        terminal_reason: str | None = None,
        task_result_json: str | None = None,
    ) -> ExecuteResponse:
        """Assemble the response: cap the console at ``MAX_TEXT_CHARS`` and
        attach the warnings/screenshots collected during the call."""
        console = buf.getvalue()
        truncated = False
        if len(console) > MAX_TEXT_CHARS:
            console = console[:MAX_TEXT_CHARS] + "\n… [truncated]"
            truncated = True
        return ExecuteResponse(
            console=console,
            return_value=return_value,
            error=error,
            exit_code=exit_code,
            warnings=(
                list(self._call_warnings) if warnings is None else list(warnings)
            ),
            screenshots=list(self._call_screenshots),
            truncated=truncated,
            terminal_reason=terminal_reason,
            task_result_json=task_result_json,
        )

    def _run_task_on_live_surface(
        self,
        site: str,
        name: str | None = None,
        *,
        isolated: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Run a task while borrowing this executor's sole browser surface."""
        from ..task_runner import BrowserSurface, _run_task_on_surface

        if name is None and "/" in site:
            site, name = site.split("/", 1)
        if name is None:
            raise ValueError(
                "run_task: missing task name. Pass '<site>/<name>' or two "
                "positional args."
            )
        return _run_task_on_surface(
            site,
            name,
            browser_surface=BrowserSurface(
                page=self._page,
                context=self._context,
                snapshot=self._snapshot,
            ),
            isolated=isolated,
            **kwargs,
        )

    def _build_globals(self) -> dict[str, Any]:
        """The exec namespace: the Phase C surface, but with ``page`` /
        ``context`` replaced by the LIVE held objects, ``state`` injected by
        reference (persists across calls — Fork 5), and ``snapshot`` rebound to
        the live page.

        We reuse ``_namespace.build_globals()`` so the agent helper layer +
        EXPORTS stay identical to the heredoc, then OVERWRITE the lazy proxies
        with live objects. The handle the lazy build injected
        (``__bw_playwright_handle__``) is dropped — the executor owns teardown,
        not the per-call namespace."""
        from ..repl import _namespace

        g = _namespace.build_globals()
        g.pop("__bw_playwright_handle__", None)
        g["page"] = self._page
        g["context"] = self._context
        g["state"] = self._state  # same dict object each call → persistent
        if self._snapshot is not None:
            g["snapshot"] = self._snapshot
        # Task invocations inside inline code borrow the same resident surface;
        # never let the public task runner allocate a second Playwright handle.
        g["run_task"] = self._run_task_on_live_surface
        # ``reset()`` is injected terminal control flow.  The response tells the
        # client to reap this exact process; the next command reconnects and
        # binds the session's durable current target.
        g["reset"] = self._reset
        # Output-protocol producers the agent (or helpers) can call to surface a
        # `[WARNING]` line or a screenshot path back through the response.
        g["_bw_warn"] = self._call_warnings.append
        g["_bw_record_screenshot"] = self._record_screenshot
        return g

    def _record_screenshot(self, path: str, **meta: Any) -> str:
        """Register a screenshot the heredoc captured so its path surfaces in
        the response's ``screenshots`` block (path-based — the bytes stay on
        disk, shared between executor + client). Returns the path so it can be
        used inline (e.g. ``page.screenshot(path=_bw_record_screenshot(p))``)."""
        block: dict[str, Any] = {"path": str(path)}
        block.update(meta)
        self._call_screenshots.append(block)
        return str(path)

    def _teardown(self) -> None:
        """Disconnect the facade transport WITHOUT closing the user's tabs.

        Mirrors ``PlaywrightHandle.close``: only ``__exit__`` the
        ``sync_playwright()`` manager (stops the driver = severs CDP). NEVER
        ``page/context/browser.close()`` (would kill the user's real tab / hang
        over the facade)."""
        self._browser = None
        self._context = None
        self._page = None
        if self._pw_cm is not None:
            with _suppress():
                self._pw_cm.__exit__(None, None, None)
        self._pw_cm = None
        self._pw = None


class _suppress:
    """Local ``contextlib.suppress(BaseException)`` clone for teardown paths."""

    def __enter__(self) -> "_suppress":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None


def _serve(session_id: str, sock: socket.socket, worker: _Worker) -> None:
    """Accept loop: one request → one response per connection (serial).

    Connections are handled one at a time on the main thread; the worker thread
    runs the code, so even concurrent clients serialize through the worker queue
    (Fork 3). Each client opens, sends one ExecuteRequest, gets one response.

    PR2 idle signal: after every served call we touch the discovery file's mtime
    so the daemon's idle-watchdog can tell when this executor last did work
    WITHOUT a separate heartbeat channel — the file already exists (we wrote it
    at startup) and the daemon already reads it. A spawn-time write plus a
    per-call touch gives the watchdog an accurate "last did work" clock (read
    daemon-side via `ExecutorHandle.idle_seconds`)."""
    from ..daemon import _ipc

    while True:
        try:
            conn, _addr = sock.accept()
        except OSError:
            break
        with conn:
            try:
                msg = recv_message(conn)
            except (ConnectionError, ValueError, OSError):
                continue
            try:
                req = ExecuteRequest.from_dict(msg)
            except ValueError as e:
                send_message(conn, ExecuteResponse(
                    error={"type": "ValueError", "msg": str(e)},
                    exit_code=3).to_dict())
                continue
            # `from_dict` copied the selected environment into `req.env`; clear
            # the decoded frame's duplicate mapping before submit blocks.
            raw_env = msg.get("env")
            if isinstance(raw_env, dict):
                raw_env.clear()
            msg.clear()
            if req.executor_id is not None and req.executor_id != worker._executor_id:
                send_message(
                    conn,
                    ExecuteResponse(
                        error={
                            "type": "ExecutorIdentityMismatch",
                            "msg": (
                                "executor lease no longer matches the process "
                                "serving this socket"
                            ),
                        },
                        exit_code=3,
                    ).to_dict(),
                )
                continue
            resp = worker.submit(req)
            # The worker loop clears req.env before publishing its response.
            # Drop the now-empty request reference on the serving thread too.
            del req
            _touch_discovery(session_id, _ipc)
            try:
                send_message(conn, resp.to_dict())
            except OSError:
                pass
            if resp.terminal_reason is not None:
                # The response has been flushed.  Returning lets main clean the
                # socket/discovery record and exit even if the daemon/client
                # disappears before issuing its exact-instance reap RPC.
                return


def _touch_discovery(session_id: str, _ipc: Any) -> None:
    """Bump the discovery file mtime = "this executor just did work" (PR2 idle
    signal read by the daemon idle-watchdog). Best-effort — a failed touch only
    costs us an earlier idle reap, never correctness."""
    try:
        os.utime(_ipc.executor_file_path(session_id), None)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..daemon import _ipc
    from ..daemon.observability import install_sigusr1_traceback

    parser = argparse.ArgumentParser(prog="browserwright._executor")
    parser.add_argument(
        "--session", required=True, help="the session id this executor serves"
    )
    parser.add_argument(
        "--executor-id",
        required=True,
        help="opaque daemon-issued executor instance identity",
    )
    args = parser.parse_args(argv)
    session_id = args.session
    # Keep the env marker for helper code that still reads it; the --session
    # flag is authoritative for binding.
    os.environ["BD_SESSION"] = session_id

    executor_id = args.executor_id
    worker = _Worker(session_id, executor_id=executor_id)
    worker.start()

    # Bind the socket + publish the discovery file BEFORE any cold-start, so the
    # daemon's `ensureExecutor` (which only waits for the discovery file) returns
    # the moment we are LISTENING — sub-second, off the slow connect's path. The
    # facade cold-start (connect_over_cdp + bind, ~10-35s on a daemon-restart
    # race) is deferred to the worker's first execute (the client's data-plane
    # call absorbs it). This decouples executor READINESS (socket up) from
    # CONNECTEDNESS (browser bound), keeping the keepalive-sensitive control
    # plane fast.
    sock = _ipc.make_executor_socket(session_id)
    _ipc.write_executor_file(
        session_id,
        str(_ipc.executor_sock_path(session_id)),
        os.getpid(),
        executor_id=executor_id,
    )

    # Defense-in-depth: clean our own discovery file + socket on SIGTERM (the
    # daemon's `registry.kill` also cleans them daemon-side, but a kill from any
    # path — manual SIGTERM, orphan-sweep — must not leave a stale file the next
    # daemon's `ensureExecutor` could latch onto). We can't run the normal
    # `finally` from a default-SIGTERM-killed process, so install a handler that
    # unlinks the discovery file/socket then hard-exits.
    _install_sigterm_cleanup(session_id)
    install_sigusr1_traceback("executor")

    try:
        _serve(session_id, sock, worker)
    finally:
        worker.shutdown()
        with _suppress():
            sock.close()
        _ipc.cleanup_executor(session_id)
    return 0


def _install_sigterm_cleanup(session_id: str) -> None:
    """Install a SIGTERM handler that unlinks this executor's discovery file +
    socket before exiting. POSIX-only (no SIGTERM on Windows); best-effort."""
    import signal

    from ..daemon import _ipc

    if not hasattr(signal, "SIGTERM"):
        return

    def _handler(_signum, _frame):  # noqa: ANN001
        with _suppress():
            _ipc.cleanup_executor(session_id)
        # Hard-exit: avoid re-entering the (about-to-die) Playwright driver via
        # atexit/finalizers, which can hang. The daemon reaps our pid.
        os._exit(0)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Not on the main thread / unsupported — daemon-side cleanup covers us.
        pass
