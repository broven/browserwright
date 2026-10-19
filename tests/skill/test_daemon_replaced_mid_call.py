"""A daemon that is replaced mid-call must be survived, or reported honestly.

2026-10-19, observed verbatim: three agent calls died with

    ensureExecutor failed for session '2378': CDP
    BrowserwrightDaemon.ensureExecutor failed: ensureExecutor failed:
    RuntimeError('upstream readiness: no browserwright extension is connected
    to the daemon (session 2378). Open the browser where the extension is
    installed and ensure it is enabled; ... chrome://extensions ...')

while the extension was connected and healthy throughout — the *daemon* was
being replaced under the call. H2's half of the fix is pinned here:

  - the two seams that run strictly before a request can reach the executor
    (the `ensureExecutor` verb, the `/exec` connect) retry a daemon that went
    away, and re-resolve the endpoint between attempts, so a replacement is
    invisible to the agent and its code still runs exactly once;
  - a failure once a request frame may be on the wire is NOT retried (a replay
    would run the agent's code twice) — it reaps the exact executor instead;
  - the retry decision is "the daemon went away", never "any error": the
    no-extension diagnosis is answered immediately, with its wording untouched;
  - when the retries run out, the text names the daemon and ends with the one
    instruction that applies (ADR-0013 rule 3, the banned-word list).
"""
from __future__ import annotations

import json
import time

import pytest

import browserwright.session as session_mod
from browserwright._executor import client as exec_client
from browserwright._executor import protocol
from browserwright.daemon.errors import DaemonShuttingDown
from browserwright.daemon_url import DaemonEndpoint
from browserwright.errors import CDPError, CDPTransportClosed, DaemonUnavailable

SID = "s-1"

#: The shape the daemon produced on 2026-10-19 for a *connected* extension,
#: because its own shutdown had pulled the relay away. It must never be the
#: answer to a daemon departure, and it must not be retried.
NO_EXTENSION_ENVELOPE = (
    "ensureExecutor failed: RuntimeError(\"upstream readiness: no browserwright "
    f"extension is connected to the daemon (session '{SID}'). Open the browser "
    "where the extension is installed and ensure it is enabled; if you just "
    "installed or upgraded browserwright, reload the extension at "
    "chrome://extensions so its service worker reconnects to the daemon relay. "
    "Then retry. (Use --backend=cdp --create for an isolated Chrome that needs "
    "no extension.)\")"
)


class _FakeDaemon:
    """`ModeBClient`'s two-method contract, scripted.

    ``endpoints`` is consumed one entry per resolve (the last one repeats): a
    string is returned, an exception is raised. A replacement publishes a new
    endpoint, so a retry that re-resolves can be told apart from one that dials
    the dead address again.
    """

    def __init__(self, *endpoints):
        self._endpoints = list(endpoints) or ["ws://127.0.0.1:19990/control"]
        self.resolves = 0
        self.invalidated = 0

    def resolve_ws_url(self) -> str:
        item = (self._endpoints.pop(0) if len(self._endpoints) > 1
                else self._endpoints[0])
        self.resolves += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def invalidate(self) -> None:
        self.invalidated += 1


class _ExecLink:
    """The `/exec` ws: records the frames the client shipped, answers one."""

    def __init__(self, fail_on_recv=None):
        self.frames = []
        self._reply = protocol.ExecuteResponse(console="ran once\n")
        self._fail_on_recv = fail_on_recv

    def send(self, payload):
        self.frames.append(json.loads(payload))

    def recv(self, timeout=None):
        if self._fail_on_recv is not None:
            raise self._fail_on_recv
        return json.dumps(self._reply.to_dict())

    def close(self):
        return None


def _session(monkeypatch, *endpoints):
    """A real `Session` over a scripted `ModeBClient` (its `CDPSession` is
    replaced by `_install_control`), with the ledger write neutered."""
    daemon = _FakeDaemon(*endpoints)
    monkeypatch.setattr(exec_client.reg, "touch", lambda _sid: None)
    return session_mod.Session(daemon, record={"id": SID}), daemon


def _install_control(monkeypatch, outcomes):
    """Point `Session.cdp` at a scripted control socket.

    Returns the sockets created, in order: the count is how many times the
    client re-dialed the daemon, and each ``ws_url`` shows which endpoint that
    attempt used (so "re-resolved" is observable, not assumed).
    """
    sockets = []

    class _ControlSocket:
        def __init__(self, ws_url):
            self.ws_url = ws_url
            self._closed = False
            self.methods = []
            sockets.append(self)

        def send(self, method, **params):
            self.methods.append(method)
            if method == "BrowserwrightDaemon.killExecutor":
                return {"reaped": True}
            assert method == "BrowserwrightDaemon.ensureExecutor", method
            outcome = outcomes.pop(0)
            if isinstance(outcome, CDPTransportClosed):
                # The daemon serving this socket exited mid-call.
                self._closed = True
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def close(self):
            self._closed = True

    monkeypatch.setattr(session_mod, "CDPSession", _ControlSocket)
    return sockets


def _install_exec_link(monkeypatch, link, *, fail_first=False):
    """Stand in for the data plane's ``ws_connect``, recording each attempt."""
    connects = []

    def _connect(url, **_kwargs):
        connects.append(url)
        if fail_first and len(connects) == 1:
            raise OSError(61, "Connection refused")
        return link

    monkeypatch.setattr(exec_client, "ws_connect", _connect)
    return connects


# ---- the replacement is survived -------------------------------------------


def test_a_replaced_daemon_is_retried_against_the_re_resolved_endpoint(monkeypatch):
    """Attempt 1's socket dies because the daemon was replaced; attempt 2 must
    reach the daemon that answers *now*, and the caller's code must still cross
    the wire exactly once."""
    sess, daemon = _session(
        monkeypatch,
        "ws://127.0.0.1:19990/control?session=s-1",  # the daemon that leaves
        "ws://127.0.0.1:29990/control?session=s-1",  # its replacement
    )
    sockets = _install_control(monkeypatch, [
        CDPTransportClosed(
            method="BrowserwrightDaemon.ensureExecutor",
            cdp_message="sent 1000 (OK); then received 1001 (going away)"),
        {"ready": True, "executor_id": "exec-2"},
    ])
    link = _ExecLink()
    connects = _install_exec_link(monkeypatch, link)

    response = exec_client.run_on_executor(
        sess, "print('once')", timeout_ms=5000)

    # Re-resolved, not reused: the cached url was dropped and the dead socket
    # replaced, so attempt 2 dialed the endpoint the replacement published.
    assert daemon.invalidated == 1
    assert [s.ws_url for s in sockets] == [
        "ws://127.0.0.1:19990/control?session=s-1",
        "ws://127.0.0.1:29990/control?session=s-1",
    ]
    # The caller's code ran once, carrying the identity attempt 2 confirmed.
    assert len(connects) == 1
    assert len(link.frames) == 1
    assert link.frames[0]["code"] == "print('once')"
    assert link.frames[0]["executor_id"] == "exec-2"
    assert response.console == "ran once\n"


def test_a_shutting_down_daemon_answer_is_retried(monkeypatch):
    """The daemon's own words for its departure must be read as
    "it left" — retried against the replacement, not reported as the browser's
    problem. The phrase comes from `DaemonShuttingDown` itself so the marker
    and this classifier cannot drift apart."""
    sess, daemon = _session(
        monkeypatch,
        "ws://127.0.0.1:19990/control?session=s-1",
        "ws://127.0.0.1:29990/control?session=s-1",
    )
    sockets = _install_control(monkeypatch, [
        CDPError(method="BrowserwrightDaemon.ensureExecutor",
                 cdp_message=f"ensureExecutor failed: {DaemonShuttingDown(SID)!r}"),
        {"ready": True, "executor_id": "exec-3"},
    ])

    assert exec_client.ensure_executor(sess) == "exec-3"
    # It retried (the second scripted answer is what came back) and re-armed
    # endpoint resolution for the next dial. An *open* control socket is
    # reused on purpose; only a dead one is re-dialed (`Session.cdp`).
    assert sockets[0].methods == ["BrowserwrightDaemon.ensureExecutor"] * 2
    assert daemon.invalidated == 1


def test_a_replacement_still_binding_the_endpoint_is_retried(monkeypatch):
    """After a departure, a *refused* endpoint is the same event continuing (the
    replacement has not bound yet) — not a new, hopeless cause. One call spans
    leave → refused → bound, and still ships the code once."""
    sess, daemon = _session(
        monkeypatch,
        "ws://127.0.0.1:19990/control?session=s-1",  # the daemon that leaves
        DaemonUnavailable("no browserwright daemon answered at "
                          "ws://127.0.0.1:19990: Connection refused"),
        DaemonUnavailable("no browserwright daemon answered at "
                          "ws://127.0.0.1:19990: Connection refused"),
        "ws://127.0.0.1:29990/control?session=s-1",  # the replacement, bound
    )
    sockets = _install_control(monkeypatch, [
        CDPTransportClosed(
            method="BrowserwrightDaemon.ensureExecutor",
            cdp_message="received 1001 (going away)"),
        {"ready": True, "executor_id": "exec-5"},
    ])
    link = _ExecLink()
    _install_exec_link(monkeypatch, link)

    response = exec_client.run_on_executor(
        sess, "print('once')", timeout_ms=5000)

    assert [s.ws_url for s in sockets] == [
        "ws://127.0.0.1:19990/control?session=s-1",
        "ws://127.0.0.1:29990/control?session=s-1",
    ]
    assert daemon.resolves >= 3      # the refused attempt re-asked, and retried
    assert len(link.frames) == 1
    assert link.frames[0]["executor_id"] == "exec-5"
    assert response.console == "ran once\n"


def test_the_exec_connect_retries_a_replacement_on_its_new_endpoint(monkeypatch):
    """The data plane's half: a `/exec` ws that will not open is the
    replacement still binding. Each attempt re-resolves the endpoint (the state
    file the replacement publishes), so a daemon that came back on another port
    is dialed at the new address instead of the dead one."""
    sess, daemon = _session(monkeypatch, "ws://127.0.0.1:19990/control?session=s-1")
    _install_control(monkeypatch, [{"ready": True, "executor_id": "exec-4"}])
    endpoints = ["http://127.0.0.1:19990", "http://127.0.0.1:29990"]

    def _endpoint(**_kw):
        # The replacement publishes a *new* address; a retry that re-resolved
        # must dial it instead of the dead one.
        return DaemonEndpoint(
            url=(endpoints.pop(0) if len(endpoints) > 1 else endpoints[0]),
            explicit=False, source="state")

    monkeypatch.setattr(exec_client, "daemon_endpoint", _endpoint)
    link = _ExecLink()
    connects = _install_exec_link(monkeypatch, link, fail_first=True)

    response = exec_client.run_on_executor(
        sess, "print('once')", timeout_ms=5000)

    assert connects == [
        "ws://127.0.0.1:19990/exec?session=s-1",
        "ws://127.0.0.1:29990/exec?session=s-1",
    ]
    assert len(link.frames) == 1
    assert link.frames[0]["code"] == "print('once')"
    assert response.console == "ran once\n"


# ---- and never at the cost of running the code twice -----------------------


def test_a_failure_after_the_request_was_sent_is_not_retried(monkeypatch):
    """The retry rule's other half. Once a request frame may be on the wire, a
    replay could execute the agent's code a second time — worse than an error —
    so this path reaps that exact executor and tells the agent to retry."""
    sess, _daemon = _session(
        monkeypatch, "ws://127.0.0.1:19990/control?session=s-1")
    sockets = _install_control(monkeypatch, [
        {"ready": True, "executor_id": "exec-7"},
    ])
    link = _ExecLink(fail_on_recv=ConnectionError("relay went away"))
    connects = _install_exec_link(monkeypatch, link)

    with pytest.raises(exec_client.ExecutorUnavailable) as ei:
        exec_client.run_on_executor(sess, "print('twice?')", timeout_ms=5000)

    # One frame, one connect, one ensureExecutor: nothing was replayed.
    assert len(link.frames) == 1
    assert len(connects) == 1
    assert sockets[0].methods == [
        "BrowserwrightDaemon.ensureExecutor",
        "BrowserwrightDaemon.killExecutor",
    ]
    msg = str(ei.value)
    assert "the daemon" in msg          # names the layer that went away
    assert "retry the command" in msg    # ... and one thing to do about it
    assert "chrome://extensions" not in msg


# ---- the distinction decides, not "any error" ------------------------------


def test_the_no_extension_diagnosis_is_never_retried(monkeypatch):
    """The daemon answered; it did not leave. The next attempt would fail the
    same way, so retrying would only burn the caller's deadline and then report
    the wrong layer — and the extension wording stays exactly as it was."""
    sess, daemon = _session(monkeypatch, "ws://127.0.0.1:19990/control?session=s-1")
    sockets = _install_control(monkeypatch, [
        CDPError(method="BrowserwrightDaemon.ensureExecutor",
                 cdp_message=NO_EXTENSION_ENVELOPE),
    ])

    with pytest.raises(exec_client.ExecutorUnavailable) as ei:
        exec_client.run_on_executor(sess, "print(1)")

    assert len(sockets) == 1          # one attempt: not a daemon departure
    assert daemon.invalidated == 0
    assert "no browserwright extension is connected" in str(ei.value)
    assert "chrome://extensions" in str(ei.value)


# ---- what the agent sees when the retries run out --------------------------


def test_exhausted_retries_name_the_daemon_and_one_instruction(monkeypatch):
    sess, _daemon = _session(
        monkeypatch, "ws://127.0.0.1:19990/control?session=s-1")
    sockets = _install_control(monkeypatch, [
        CDPTransportClosed(
            method="BrowserwrightDaemon.ensureExecutor",
            cdp_message="received 1001 (going away)")
        for _ in range(20)
    ])
    started = time.monotonic()
    with pytest.raises(exec_client.ExecutorUnavailable) as ei:
        exec_client.run_on_executor(sess, "print(1)", timeout_ms=250)
    elapsed = time.monotonic() - started

    err = ei.value
    msg = str(err)
    assert len(sockets) >= 2        # it really did retry
    assert elapsed < 2.0            # ... inside the caller's 250ms budget
    assert "the daemon" in msg
    assert "was replaced or is shutting down" in msg
    assert "no browserwright extension is connected" not in msg
    assert "chrome://extensions" not in msg
    # ADR-0013 rule 3: the message carries what the endpoint answers *now*
    # (probed once), not only our inference about who left.
    assert "Right now nothing is listening at 127.0.0.1:" in msg
    # One instruction, and it is the one that applies to a replaced daemon.
    assert err.fix == "retry the command"
    assert msg.endswith("[fix: retry the command]")


def test_an_endpoint_that_was_never_there_is_reported_without_retrying(
        monkeypatch):
    """The retry is for a daemon that *left*, not for one that never was: a
    first-attempt refusal is a standing condition, already diagnosed by
    `DaemonUnavailable` (probed endpoint + the auto-start rule), and `session
    new` / `recover` own starting a daemon. Waiting 9s to say the same thing
    would just make every "no daemon" call feel hung."""
    gone = "no browserwright daemon answered at ws://127.0.0.1:19990"
    sess, daemon = _session(monkeypatch, DaemonUnavailable(f"{gone}: refused"))
    started = time.monotonic()

    with pytest.raises(DaemonUnavailable) as ei:
        exec_client.run_on_executor(sess, "print(1)", timeout_ms=9000)

    assert gone in str(ei.value)
    assert time.monotonic() - started < 1.0
    # `Session._resolve_ws_url`'s own single in-place retry, and nothing more.
    assert daemon.resolves == 2
