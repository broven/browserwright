"""Unit coverage for Phase B PR1 (no real browser, no daemon):

  - the inline static pre-check routing decision (memory-only → in-process;
    page/context/snapshot/state/reset → ship to executor).
  - the executor execute request/response wire framing (length-framed JSON).
  - executor namespace injection: `page`/`context` are the LIVE held objects,
    `state` is injected by reference (persists across calls), `snapshot` is
    rebound to the live page.

The connect+bind cold-start + cross-heredoc liveness (which need the facade + a
browser) are covered by `tests/daemon/e2e/test_l2_phase_b_executor.py`.
"""
from __future__ import annotations

import socket

import pytest

from browserwright._executor import client as executor_client
from browserwright._executor.client import ExecutorUnavailable
from browserwright._executor import protocol
from browserwright._executor.process import _LivePageHolder, _Worker
from browserwright.repl import inline


# ---- static pre-check routing ---------------------------------------------


def _touches(code: str) -> bool:
    return inline._touches_executor_surface(compile(code, "<t>", "exec"))


def test_precheck_memory_only_is_in_process():
    # Pure memory()/http_get/site-skill heredocs touch none of the executor
    # names → run in-process (lightweight, never spawn an executor).
    assert _touches("print(6 * 7)") is False
    assert _touches("x = remember('foo'); print(x)") is False
    assert _touches("print(http_get('https://example.com'))") is False
    assert _touches("import json; print(json.dumps({'a': 1}))") is False


@pytest.mark.parametrize("code", [
    "page.goto('https://example.com')",
    "print(snapshot())",
    "n = len(context.pages)",
    "state.x = 1",
    "reset()",
])
def test_precheck_browser_surface_ships_to_executor(code):
    assert _touches(code) is True


def test_precheck_scans_nested_code_objects():
    # A name used only inside a def / comprehension still routes to the executor
    # (co_names of nested code objects are scanned).
    assert _touches("def f():\n    return state.get('x')") is True
    assert _touches("vals = [p.url for p in context.pages]") is True
    assert _touches("def g():\n    page.goto('x')\n\ng()") is True


def test_precheck_does_not_false_positive_on_attribute_names():
    # `co_names` includes attribute names too; `something.page` (NOT the global
    # `page`) is acceptable to over-route — the fallback is always correct.
    # But a plain memory call referencing none of the names must stay in-process.
    assert _touches("d = {'k': 1}\nprint(d['k'])") is False


def test_executor_unavailable_fix_mentions_reset():
    err = ExecutorUnavailable("down")

    assert "reset()" in err.fix
    assert "session reset" in err.fix


def test_run_on_executor_touches_session_before_ensure(monkeypatch):
    order: list[str] = []

    class _Sess:
        session_record = {"id": "sess-touch"}

    monkeypatch.setattr(
        executor_client.reg,
        "touch",
        lambda sid: order.append(f"touch:{sid}") or {"id": sid},
    )
    monkeypatch.setattr(
        executor_client,
        "ensure_executor",
        lambda sess: order.append("ensure") or (_ for _ in ()).throw(
            ExecutorUnavailable("stop after ensure")
        ),
    )

    with pytest.raises(ExecutorUnavailable):
        executor_client.run_on_executor(_Sess(), "page.url")

    assert order == ["touch:sess-touch", "ensure"]


# ---- wire framing ----------------------------------------------------------


def test_request_roundtrip():
    a, b = socket.socketpair()
    try:
        protocol.send_message(
            a, protocol.ExecuteRequest("print('hi')", 12345).to_dict())
        got = protocol.ExecuteRequest.from_dict(protocol.recv_message(b))
        assert got.code == "print('hi')"
        assert got.timeout_ms == 12345
    finally:
        a.close()
        b.close()


def test_request_defaults_and_validation():
    # Missing/invalid timeout falls back to the default.
    r = protocol.ExecuteRequest.from_dict({"code": "x"})
    assert r.timeout_ms == protocol.DEFAULT_TIMEOUT_MS
    r2 = protocol.ExecuteRequest.from_dict({"code": "x", "timeout_ms": -5})
    assert r2.timeout_ms == protocol.DEFAULT_TIMEOUT_MS
    # Non-string code is rejected.
    with pytest.raises(ValueError):
        protocol.ExecuteRequest.from_dict({"code": 123})


def test_response_roundtrip():
    a, b = socket.socketpair()
    try:
        resp = protocol.ExecuteResponse(
            console="out\n", return_value="42",
            error={"type": "X", "msg": "boom"}, exit_code=3,
            warnings=["w1"])
        protocol.send_message(a, resp.to_dict())
        got = protocol.ExecuteResponse.from_dict(protocol.recv_message(b))
        assert got.console == "out\n"
        assert got.return_value == "42"
        assert got.error == {"type": "X", "msg": "boom"}
        assert got.exit_code == 3
        assert got.warnings == ["w1"]
    finally:
        a.close()
        b.close()


def test_recv_message_closed_socket_raises():
    a, b = socket.socketpair()
    a.close()
    try:
        with pytest.raises(ConnectionError):
            protocol.recv_message(b)
    finally:
        b.close()


# ---- executor namespace injection -----------------------------------------


def _fake_worker_with_objects():
    """A `_Worker` with its live objects faked in (no real Playwright)."""
    w = _Worker("sess-test")

    class _FakePage:
        def __init__(self):
            self.url = "about:blank"

        def aria_snapshot(self, *, mode):
            return "- root [ref=e1]"

    class _FakeContext:
        def __init__(self):
            self.pages = [_FakePage()]

    page = _FakePage()
    w._page = page
    w._context = _FakeContext()
    w._snapshot_holder.page = page
    from browserwright.repl.snapshot import make_snapshot
    w._snapshot = make_snapshot(w._snapshot_holder)
    # Model a worker whose lazy cold-start ALREADY completed (these tests assert
    # post-connect execute behavior). Without this, `_execute`'s lazy
    # `_ensure_cold_started` would try a real facade connect and short-circuit
    # the code with a cold-start error.
    w._connected = True
    return w, page


def test_namespace_injects_live_objects_and_state():
    w, page = _fake_worker_with_objects()
    g = w._build_globals()
    # page/context are the LIVE held objects (NOT lazy proxies).
    assert g["page"] is page
    assert g["context"] is w._context
    # state is the worker's persistent dict, injected by reference.
    assert g["state"] is w._state
    # snapshot is the rebound first-party snapshot (the PR module, not legacy).
    assert callable(g["snapshot"])
    assert g["snapshot"].__module__ == "browserwright.repl.snapshot"
    # The lazy handle the base namespace injects is dropped (executor owns
    # teardown, not the per-call namespace).
    assert "__bw_playwright_handle__" not in g
    # Core EXPORTS / stdlib still present (superset of the heredoc surface).
    assert "remember" in g and "http_get" in g and "json" in g


def test_state_persists_across_executes():
    w, _page = _fake_worker_with_objects()
    # Two _execute calls share the same `state` dict by reference (Fork 5).
    r1 = w._execute(protocol.ExecuteRequest("state['x'] = 1", 1000))
    assert r1.exit_code == 0 and r1.error is None
    r2 = w._execute(protocol.ExecuteRequest("print('X=' + str(state['x']))", 1000))
    assert r2.exit_code == 0
    assert "X=1" in r2.console


def test_execute_captures_console_and_errors():
    w, _page = _fake_worker_with_objects()
    ok = w._execute(protocol.ExecuteRequest("print('hello')", 1000))
    assert ok.console == "hello\n"
    assert ok.error is None and ok.exit_code == 0

    boom = w._execute(protocol.ExecuteRequest("raise ValueError('nope')", 1000))
    assert boom.error is not None
    assert boom.error["type"] == "ValueError"
    assert "nope" in boom.error["msg"]
    assert boom.exit_code == 3


def test_execute_browserwright_error_carries_exit_code():
    w, _page = _fake_worker_with_objects()
    code = ("from browserwright.errors import NoSession\n"
            "raise NoSession('x')")
    r = w._execute(protocol.ExecuteRequest(code, 1000))
    assert r.error is not None
    assert r.error["type"] == "NoSession"
    # NoSession.exit_code == 2 (errors.py table).
    assert r.exit_code == 2


def test_snapshot_observes_live_page():
    w, _page = _fake_worker_with_objects()
    r = w._execute(protocol.ExecuteRequest("print(snapshot())", 1000))
    assert r.exit_code == 0
    assert "[ref=e1]" in r.console


def test_live_page_holder_rebinds():
    h = _LivePageHolder()
    assert h.page is None
    sentinel = object()
    h.page = sentinel
    assert h.page is sentinel


# ---- PR3: output protocol -------------------------------------------------


def test_return_value_of_trailing_expression():
    w, _page = _fake_worker_with_objects()
    r = w._execute(protocol.ExecuteRequest("1 + 2", 1000))
    assert r.exit_code == 0 and r.error is None
    assert r.return_value == "3"


def test_no_return_value_for_statement_ending_code():
    w, _page = _fake_worker_with_objects()
    r = w._execute(protocol.ExecuteRequest("x = 5\nstate['x'] = x", 1000))
    assert r.return_value is None


def test_trailing_expression_after_statements_runs_side_effects():
    # The body executes (side effects) AND the trailing expr value is captured.
    w, _page = _fake_worker_with_objects()
    r = w._execute(protocol.ExecuteRequest(
        "print('hi')\nstate['k'] = 9\nstate['k'] * 2", 1000))
    assert "hi\n" in r.console
    assert r.return_value == "18"


def test_trailing_none_expression_yields_no_return_value():
    w, _page = _fake_worker_with_objects()
    r = w._execute(protocol.ExecuteRequest("print('x')", 1000))
    # print() returns None → no [return value] noise.
    assert r.return_value is None


def test_generic_error_carries_traceback():
    w, _page = _fake_worker_with_objects()
    r = w._execute(protocol.ExecuteRequest("raise RuntimeError('boom')", 1000))
    assert r.error is not None
    assert r.error["type"] == "RuntimeError"
    assert "boom" in r.error["msg"]
    # PR3: the traceback the in-process path writes is restored for shipped
    # heredocs.
    assert "traceback" in r.error
    assert "RuntimeError" in r.error["traceback"]
    assert "Traceback (most recent call last)" in r.error["traceback"]


def test_playwright_like_timeout_error_carries_fix():
    w, _page = _fake_worker_with_objects()
    code = "raise TimeoutError('Locator.click: Timeout 30000ms exceeded')"

    r = w._execute(protocol.ExecuteRequest(code, 1000))

    assert r.error is not None
    assert r.error["type"] == "TimeoutError"
    assert "snapshot()" in r.error["fix"]


def test_console_truncation():
    w, _page = _fake_worker_with_objects()
    # Print well over MAX_TEXT_CHARS.
    code = f"print('x' * {protocol.MAX_TEXT_CHARS + 5000})"
    r = w._execute(protocol.ExecuteRequest(code, 2000))
    assert r.truncated is True
    assert len(r.console) <= protocol.MAX_TEXT_CHARS + 64
    assert r.console.endswith("[truncated]")


def test_warnings_and_screenshots_surface():
    w, _page = _fake_worker_with_objects()
    code = ("_bw_warn('a popup became a tab')\n"
            "_bw_record_screenshot('/tmp/shot.png', label='login')\n")
    r = w._execute(protocol.ExecuteRequest(code, 1000))
    assert r.warnings == ["a popup became a tab"]
    assert r.screenshots == [{"path": "/tmp/shot.png", "label": "login"}]


def test_call_buffers_reset_between_calls():
    w, _page = _fake_worker_with_objects()
    w._execute(protocol.ExecuteRequest("_bw_warn('first')", 1000))
    r2 = w._execute(protocol.ExecuteRequest("print('plain')", 1000))
    # The second call's warnings/screenshots are not carried over.
    assert r2.warnings == []
    assert r2.screenshots == []


# ---- PR3: reset() ----------------------------------------------------------


class _FakeBrowser:
    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.contexts: list = []

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def off(self, event, fn):
        self.handlers.get(event, []).remove(fn)


def _arm_fake_browser(w):
    """Give the worker a fake browser + an armed facade-death handler so we can
    assert reset() disarms/re-arms WITHOUT touching the process."""
    b = _FakeBrowser()
    w._browser = b
    w._arm_facade_death()
    assert w._facade_death_handler is not None
    assert b.handlers["disconnected"] == [w._facade_death_handler]
    return b


def test_reset_disarms_old_rearms_new_without_exiting(monkeypatch):
    import browserwright._executor.process as proc

    w, page = _fake_worker_with_objects()
    w._state["keep"] = "no"
    # reset() reuses the live driver; fake it so reset() does not enter the real
    # sync_playwright() manager (the driver was entered at cold-start).
    w._pw_cm = object()
    w._pw = object()
    old_browser = _arm_fake_browser(w)
    old_handler = w._facade_death_handler

    rebuilt = {"called": False, "new_browser": None}

    def fake_connect_and_bind(self):
        # Simulate a successful rebuild: new browser + re-arm.
        rebuilt["called"] = True
        nb = _FakeBrowser()
        self._browser = nb
        self._arm_facade_death()
        rebuilt["new_browser"] = nb

    monkeypatch.setattr(proc._Worker, "_connect_and_bind", fake_connect_and_bind)

    # If reset wrongly tripped the self-exit, this would call os._exit; guard it.
    exited = {"code": None}
    monkeypatch.setattr(proc.os, "_exit", lambda c: exited.__setitem__("code", c))

    w._reset()

    # Old handler detached from the old browser (intentional disconnect must NOT
    # self-exit).
    assert old_handler not in old_browser.handlers.get("disconnected", [])
    # New browser is armed with a FRESH handler.
    assert rebuilt["called"] is True
    nb = rebuilt["new_browser"]
    assert w._browser is nb
    assert w._facade_death_handler is not None
    assert nb.handlers["disconnected"] == [w._facade_death_handler]
    # state cleared (Fork 6).
    assert w._state == {}
    # The process was NOT exited.
    assert exited["code"] is None


def test_reset_reuses_driver_does_not_exit_sync_playwright(monkeypatch):
    """Failure 2 (CODE bug): reset() must REUSE the live `sync_playwright()`
    driver — Playwright's sync event loop is thread-bound and cannot be
    restarted once `__exit__`-ed (re-entering yields "Event loop is closed").

    Assert: reset() does NOT `__exit__` the manager, but DOES re-run
    `connect_over_cdp` on the SAME driver instance (rebuild on the live `_pw`)."""
    import browserwright._executor.process as proc

    w, _page = _fake_worker_with_objects()

    class _FakeCM:
        def __init__(self):
            self.entered = 0
            self.exited = 0
            self.driver = object()

        def __enter__(self):
            self.entered += 1
            return self.driver

        def __exit__(self, *a):
            self.exited += 1
            return False

    cm = _FakeCM()
    # Simulate a completed cold-start: driver already entered (we set _pw to the
    # live driver directly, as _start_driver would have left it), browser armed.
    w._pw_cm = cm
    w._pw = cm.driver
    _arm_fake_browser(w)

    connect_calls = {"pw": [], "n": 0}

    def fake_connect_over_cdp(pw, **_kw):
        # The executor cold-start now passes attempts=/backoff_s= for the
        # Failure #4 retry; accept + ignore them in the stub.
        connect_calls["n"] += 1
        connect_calls["pw"].append(pw)
        return _FakeBrowser()

    def fake_context_for_browser(_b):
        class _Ctx:
            pages = []
        return _Ctx()

    def fake_bind_current_page(_ctx, _sess):
        class _P:
            url = "about:blank"
        return _P()

    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.connect_over_cdp",
        fake_connect_over_cdp)
    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.context_for_browser",
        fake_context_for_browser)
    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.bind_current_page",
        fake_bind_current_page)
    monkeypatch.setattr("browserwright.session.current_session", lambda: object())
    monkeypatch.setattr(proc.os, "_exit", lambda c: None)

    w._reset()

    # The driver manager was NEVER exited (would kill the thread-bound loop)
    # and NEVER re-entered (the event loop can't be restarted in-thread).
    assert cm.exited == 0, "reset() must not __exit__ the sync_playwright manager"
    assert cm.entered == 0, "reset() must not re-enter sync_playwright"
    # connect_over_cdp ran again, on the SAME live driver instance.
    assert connect_calls["n"] == 1
    assert connect_calls["pw"] == [cm.driver]
    # The driver references are intact (reused), not nulled.
    assert w._pw is cm.driver and w._pw_cm is cm


def test_reset_is_injected_and_clears_state(monkeypatch):
    import browserwright._executor.process as proc

    w, _page = _fake_worker_with_objects()
    w._state["x"] = 1
    # reset() reuses the live driver; fake it so reset() does not enter the real
    # sync_playwright() manager.
    w._pw_cm = object()
    w._pw = object()
    _arm_fake_browser(w)

    def fake_connect_and_bind(self):
        self._browser = _FakeBrowser()
        self._arm_facade_death()

    monkeypatch.setattr(proc._Worker, "_connect_and_bind", fake_connect_and_bind)
    monkeypatch.setattr(proc.os, "_exit", lambda c: None)

    # reset() is reachable from the exec namespace and clears state.
    r = w._execute(protocol.ExecuteRequest("reset()", 1000))
    assert r.exit_code == 0 and r.error is None
    assert w._state == {}


# ---- SIGTERM self-cleanup (Failure 3 defense-in-depth) ---------------------


def test_sigterm_handler_unlinks_discovery_and_exits(monkeypatch):
    """The executor installs a SIGTERM handler that unlinks its own discovery
    file/socket then hard-exits — so a kill from ANY path (manual SIGTERM,
    orphan-sweep) never leaves a stale file the next daemon could latch onto."""
    import signal

    import browserwright._executor.process as proc

    installed = {}

    def fake_signal(signum, handler):
        installed[signum] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    cleaned: list[str] = []
    exited: dict = {}
    monkeypatch.setattr(
        "browserwright.daemon._ipc.cleanup_executor",
        lambda sid: cleaned.append(sid))
    monkeypatch.setattr(proc.os, "_exit", lambda c: exited.__setitem__("code", c))

    proc._install_sigterm_cleanup("sess-term")
    assert signal.SIGTERM in installed

    # Fire the installed handler — it must clean up + hard-exit.
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert cleaned == ["sess-term"]
    assert exited["code"] == 0


# ---- PR3: timeout enforcement ---------------------------------------------


def test_submit_timeout_does_not_wedge_next_call():
    import threading
    import time as _time

    w = _Worker("sess-timeout")
    # Already cold-started: the per-call timeout (no cold-start slack) is what
    # this test exercises. A non-connected worker would add the cold-start wait
    # slack to `submit`, which is a different (first-execute) path.
    w._connected = True

    # Drive the worker's queue manually with a slow first call + a fast second.
    release = threading.Event()

    def worker_loop():
        # First item: slow.
        req, box = w._q.get()
        release.wait(2.0)
        box.put(protocol.ExecuteResponse(console="slow-done\n", exit_code=0))
        # Second item: fast.
        req2, box2 = w._q.get()
        box2.put(protocol.ExecuteResponse(console="fast-done\n", exit_code=0))

    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

    # First submit with a tiny timeout → returns a TimeoutError without wedging.
    t0 = _time.monotonic()
    r1 = w.submit(protocol.ExecuteRequest("slow()", timeout_ms=100))
    assert _time.monotonic() - t0 < 1.0
    assert r1.error is not None and r1.error["type"] == "TimeoutError"
    assert r1.exit_code == 3

    # The worker is still finishing the slow call; let it complete, then the
    # NEXT submit must succeed (the queue did not wedge).
    release.set()
    r2 = w.submit(protocol.ExecuteRequest("fast()", timeout_ms=5000))
    assert r2.exit_code == 0
    assert "fast-done" in r2.console
    t.join(timeout=3.0)


def test_submit_grants_cold_start_slack_on_first_call(monkeypatch):
    """The FIRST execute (worker not yet `_connected`) gets the cold-start slack
    added to the `submit` wait, so a connect+code run that exceeds the bare
    `timeout_ms` is NOT reported as a per-call timeout. Once connected, the slack
    is gone (the bare `timeout_ms` applies)."""
    import browserwright._executor.process as proc

    captured: list[float] = []

    class _FakeBox:
        def __init__(self, *a, **k):
            pass

        def put(self, *a, **k):
            pass

        def get(self, timeout=None):
            captured.append(timeout)
            return protocol.ExecuteResponse(exit_code=0)

    monkeypatch.setattr(proc.queue, "Queue", _FakeBox)

    w = _Worker("sess-slack")
    # Not connected → first-call slack is added.
    assert w._connected is False
    w.submit(protocol.ExecuteRequest("page.goto('x')", timeout_ms=1000))
    first_wait = captured[-1]
    assert first_wait == pytest.approx(1.0 + proc._COLD_START_WAIT_SLACK_S)

    # Connected → no slack, bare per-call timeout.
    w._connected = True
    w.submit(protocol.ExecuteRequest("page.title()", timeout_ms=1000))
    assert captured[-1] == pytest.approx(1.0)


# ---- lazy cold-start decoupling (control-plane / data-plane split) ---------


def test_execute_triggers_lazy_cold_start_on_first_call(monkeypatch):
    """The first execute performs the cold-start (connect+bind) on the worker;
    subsequent executes reuse the live objects WITHOUT reconnecting. Cold-start
    is NOT done at process start — it's deferred to the data-plane first call so
    the control-plane `ensureExecutor` RPC stays fast."""
    w = _Worker("sess-lazy")
    calls = {"n": 0}

    def fake_ensure(self):
        if self._connected:
            return
        calls["n"] += 1

        class _P:
            url = "about:blank"

            def aria_snapshot(self, *, mode):
                return "- root [ref=e1]"

        class _Ctx:
            pages = []

        self._page = _P()
        self._context = _Ctx()
        self._snapshot_holder.page = self._page
        from browserwright.repl.snapshot import make_snapshot
        self._snapshot = make_snapshot(self._snapshot_holder)
        self._connected = True

    monkeypatch.setattr(_Worker, "_ensure_cold_started", fake_ensure)

    assert w._connected is False
    r1 = w._execute(protocol.ExecuteRequest("print('one')", 1000))
    assert r1.exit_code == 0 and "one" in r1.console
    assert calls["n"] == 1
    assert w._connected is True

    # Second execute reuses the live objects — no re-connect.
    r2 = w._execute(protocol.ExecuteRequest("print('two')", 1000))
    assert r2.exit_code == 0 and "two" in r2.console
    assert calls["n"] == 1  # cold-start ran exactly once


def test_cold_start_failure_on_first_execute_is_actionable_error(monkeypatch):
    """A cold-start failure on the first execute is surfaced as an actionable
    error RESPONSE (the agent sees it), not a crash; `_connected` stays False so
    the next execute retries the connect (facade-death cold-restart discipline)."""
    from browserwright.repl.playwright_handle import FacadeUnavailable

    w = _Worker("sess-coldfail")
    attempts = {"n": 0}

    def fake_ensure(self):
        attempts["n"] += 1
        raise FacadeUnavailable("facade discovery file absent")

    monkeypatch.setattr(_Worker, "_ensure_cold_started", fake_ensure)

    r = w._execute(protocol.ExecuteRequest("page.goto('x')", 1000))
    assert r.error is not None
    assert r.error["type"] == "FacadeUnavailable"
    assert r.exit_code == FacadeUnavailable.exit_code
    assert w._connected is False  # not marked connected on failure
    assert attempts["n"] == 1

    # A subsequent execute RETRIES the cold-start (no wedge / sticky failure).
    w._execute(protocol.ExecuteRequest("page.goto('x')", 1000))
    assert attempts["n"] == 2


def test_generic_cold_start_failure_carries_traceback(monkeypatch):
    """A non-Browserwright cold-start exception still surfaces as a structured
    error with a `fix` hint + traceback (so the agent gets a recovery step)."""
    w = _Worker("sess-coldboom")

    def fake_ensure(self):
        raise RuntimeError("driver blew up")

    monkeypatch.setattr(_Worker, "_ensure_cold_started", fake_ensure)

    r = w._execute(protocol.ExecuteRequest("page.goto('x')", 1000))
    assert r.error is not None
    assert r.error["type"] == "RuntimeError"
    assert "cold-start failed" in r.error["msg"]
    assert "fix" in r.error and r.error["fix"]
    assert "traceback" in r.error
    assert r.exit_code == 3


def test_ensure_cold_started_enters_driver_once_and_reuses_on_retry(monkeypatch):
    """Cold-start enters `sync_playwright()` exactly ONCE; a connect failure
    leaves the live driver in place (NOT torn down) so a retry REUSES it. The
    driver's event loop is thread-bound and cannot be restarted once exited, so
    re-entering after a failed connect would yield 'Event loop is closed' — this
    guards that the failure path does not tear the driver down."""
    import browserwright._executor.process as proc

    w = _Worker("sess-driveronce")
    entered = {"n": 0}

    def fake_start_driver(self):
        entered["n"] += 1
        self._pw_cm = object()
        self._pw = object()

    monkeypatch.setattr(_Worker, "_start_driver", fake_start_driver)
    monkeypatch.setattr("browserwright.session_ctx.resolve_session",
                        lambda explicit=None: {"id": "sess-driveronce"})
    monkeypatch.setattr("browserwright.session.set_session", lambda s: None)

    connect = {"n": 0}

    def fake_connect_and_bind(self):
        connect["n"] += 1
        if connect["n"] == 1:
            raise RuntimeError("facade not up yet")
        # 2nd attempt succeeds.

    monkeypatch.setattr(_Worker, "_connect_and_bind", fake_connect_and_bind)

    # First cold-start: enters the driver, connect fails.
    with pytest.raises(RuntimeError):
        w._ensure_cold_started()
    assert entered["n"] == 1
    assert w._connected is False
    # The driver was NOT torn down on the failure (reused on retry).
    assert w._pw_cm is not None and w._pw is not None

    # Retry: REUSES the same driver (no re-enter), connect succeeds.
    w._ensure_cold_started()
    assert entered["n"] == 1  # driver entered exactly once across both calls
    assert connect["n"] == 2
    assert w._connected is True

    # Idempotent once connected.
    w._ensure_cold_started()
    assert connect["n"] == 2


def test_run_does_not_cold_start_before_serving(monkeypatch):
    """The worker loop (`_run`) must NOT cold-start before draining the queue —
    so the process can bind its socket + publish discovery immediately. Cold
    start only happens lazily inside `_execute`. We assert `_run` drains an item
    without calling `_ensure_cold_started` (the item's `_execute` would, but we
    stub `_execute` to observe the loop in isolation)."""
    import queue as _queue

    w = _Worker("sess-runorder")
    cold_called = {"n": 0}
    monkeypatch.setattr(
        _Worker, "_ensure_cold_started",
        lambda self: cold_called.__setitem__("n", cold_called["n"] + 1))

    executed: list[str] = []
    monkeypatch.setattr(
        _Worker, "_execute",
        lambda self, req: (executed.append(req.code)
                           or protocol.ExecuteResponse(exit_code=0)))
    monkeypatch.setattr(_Worker, "_teardown", lambda self: None)

    box: _queue.Queue = _queue.Queue(maxsize=1)
    w._q.put((protocol.ExecuteRequest("print(1)", 1000), box))
    w._q.put(None)  # stop sentinel
    w._run()

    # The loop drained the item via _execute and never cold-started on its own
    # (the stubbed _execute didn't call the real cold-start).
    assert executed == ["print(1)"]
    assert cold_called["n"] == 0


# ---- Failure #4: cold-start connect_over_cdp retry/backoff -----------------


class _FakeChromium:
    def __init__(self, fail_times: int):
        self._fail_times = fail_times
        self.calls = 0

    def connect_over_cdp(self, ws_url, timeout=None):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"403 Forbidden (attempt {self.calls})")
        return f"browser@{ws_url}"


class _FakePw:
    def __init__(self, fail_times: int):
        self.chromium = _FakeChromium(fail_times)


def test_connect_over_cdp_single_attempt_raises_facade_unavailable(monkeypatch):
    """The per-heredoc Phase C consumer keeps `attempts=1`: a single failure
    surfaces immediately as the actionable FacadeUnavailable (no retry)."""
    from browserwright.repl import playwright_handle as ph

    monkeypatch.setattr(ph, "_facade_ws_url", lambda: "ws://x/cdp")
    pw = _FakePw(fail_times=1)
    with pytest.raises(ph.FacadeUnavailable):
        ph.connect_over_cdp(pw)  # attempts defaults to 1
    assert pw.chromium.calls == 1  # no retry on the default path


def test_connect_over_cdp_retries_then_succeeds(monkeypatch):
    """Failure #4 defense-in-depth: the executor cold-start passes attempts>1;
    a Chrome-still-starting race (a few early failures) is absorbed and the
    connect eventually succeeds. Backoff sleep is stubbed so the test is fast."""
    from browserwright.repl import playwright_handle as ph

    monkeypatch.setattr(ph, "_facade_ws_url", lambda: "ws://x/cdp")
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    pw = _FakePw(fail_times=2)  # fails twice, succeeds on the 3rd
    browser = ph.connect_over_cdp(pw, attempts=5, backoff_s=0.1)
    assert browser == "browser@ws://x/cdp"
    assert pw.chromium.calls == 3
    assert slept == [0.1, 0.1]  # backed off between the 3 attempts


def test_connect_over_cdp_adds_session_query(monkeypatch):
    """Session-scoped facade connections let extension context.new_page() tabs
    join the browserwright session group and close on session end."""
    from browserwright.repl import playwright_handle as ph

    monkeypatch.setattr(ph, "_facade_ws_url", lambda: "ws://x/cdp?existing=1")
    pw = _FakePw(fail_times=0)
    browser = ph.connect_over_cdp(pw, session_id="bw-s")
    assert browser == "browser@ws://x/cdp?existing=1&session=bw-s"


def test_connect_over_cdp_exhausts_attempts_raises(monkeypatch):
    """All attempts fail → FacadeUnavailable with the last failure context."""
    from browserwright.repl import playwright_handle as ph

    monkeypatch.setattr(ph, "_facade_ws_url", lambda: "ws://x/cdp")
    monkeypatch.setattr("time.sleep", lambda _s: None)
    pw = _FakePw(fail_times=99)
    with pytest.raises(ph.FacadeUnavailable):
        ph.connect_over_cdp(pw, attempts=3, backoff_s=0.0)
    assert pw.chromium.calls == 3


def test_connect_over_cdp_retries_through_missing_facade_file(monkeypatch):
    """Discovery is re-read each attempt: a not-yet-(re)written facade file on
    early attempts (FacadeUnavailable from `_facade_ws_url`) doesn't abort the
    retry loop — once the file appears the connect succeeds."""
    from browserwright.repl import playwright_handle as ph

    seq = {"n": 0}

    def _ws():
        seq["n"] += 1
        if seq["n"] < 3:
            raise ph.FacadeUnavailable("facade discovery file absent")
        return "ws://late/cdp"

    monkeypatch.setattr(ph, "_facade_ws_url", _ws)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    pw = _FakePw(fail_times=0)
    browser = ph.connect_over_cdp(pw, attempts=5, backoff_s=0.0)
    assert browser == "browser@ws://late/cdp"
    assert pw.chromium.calls == 1  # only connected once the ws resolved
