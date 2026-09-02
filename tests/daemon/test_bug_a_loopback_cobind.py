"""Regression: a non-loopback `--facade-host` must not cost LOCAL reachability.

BUG A. The documented remote-access setup is `--facade-host <tailnet-ip>`
(`browserwright-daemon install --facade-host 100.72.20.32` writes it straight
into the LaunchAgent). A bind to a *specific* address listens on that address
ONLY, so `127.0.0.1:19990` was genuinely dead while the daemon was up and
healthy — and every local client that resolves the loopback default
(`DEFAULT_DAEMON_URL`, used whenever the endpoint state file is not visible to
that process) got ECONNREFUSED plus a `fix` string telling it to start a daemon
that was already running.

The fix is a loopback co-bind: remote access must not cost local access.
"""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

import pytest

from browserwright.daemon.config import Config, needs_loopback_cobind
from browserwright.daemon.server.facade import PlaywrightFacade


def _a_non_loopback_ipv4() -> str | None:
    """A real non-loopback IPv4 of this host we can actually bind, or None."""
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:  # pragma: no cover - depends on host DNS config
        addrs = []
    for addr in addrs:
        if addr.startswith("127."):
            continue
        # Only accept one we can really bind on this machine.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((addr, 0))
            return addr
        except OSError:
            continue
        finally:
            s.close()
    return None


@pytest.mark.parametrize(("host", "expected"), [
    ("127.0.0.1", False),   # already loopback
    ("localhost", False),
    ("::1", False),
    ("0.0.0.0", False),     # wildcard already covers loopback
    ("::", False),
    ("", False),
    ("100.72.20.32", True),  # the reported tailnet bind
    ("192.168.1.20", True),  # a LAN bind
])
def test_needs_loopback_cobind_classifies_the_bind_host(host, expected):
    assert needs_loopback_cobind(host) is expected


async def test_specific_non_loopback_bind_also_answers_on_loopback():
    """The regression itself: bind a specific non-loopback IP, dial 127.0.0.1.

    Before the fix this connection was refused — the whole of BUG A.
    """
    host = _a_non_loopback_ipv4()
    if host is None:
        pytest.skip("no bindable non-loopback IPv4 on this host")

    facade = PlaywrightFacade(cfg=Config(backend="cdp"), port=0, host=host)
    port = await facade.start()
    try:
        # The primary (remote-facing) bind answers.
        assert (await _ping(host, port))["pong"] is True
        # ...and so does loopback, which is what every local client dials.
        assert (await _ping("127.0.0.1", port))["pong"] is True
    finally:
        await facade.stop()

    # stop() must close BOTH listeners, not just the primary.
    with pytest.raises(OSError):
        await _ping("127.0.0.1", port)


async def test_loopback_bind_does_not_double_bind():
    """A loopback `--facade-host` needs no second listener (and must not fail)."""
    facade = PlaywrightFacade(cfg=Config(backend="cdp"), port=0,
                              host="127.0.0.1")
    port = await facade.start()
    try:
        assert facade._loopback_server is None
        assert (await _ping("127.0.0.1", port))["pong"] is True
    finally:
        await facade.stop()


async def test_cobind_failure_is_not_fatal(monkeypatch):
    """If loopback:port is taken, the daemon still serves its primary bind.

    Degrading to "remote works, local does not" beats refusing to start.
    """
    host = _a_non_loopback_ipv4()
    if host is None:
        pytest.skip("no bindable non-loopback IPv4 on this host")

    facade = PlaywrightFacade(cfg=Config(backend="cdp"), port=0, host=host)
    real_serve_on = facade._serve_on

    async def flaky(h, p):
        if h == "127.0.0.1":
            raise OSError(48, "Address already in use")
        return await real_serve_on(h, p)

    monkeypatch.setattr(facade, "_serve_on", flaky)
    port = await facade.start()
    try:
        assert facade._loopback_server is None
        assert (await _ping(host, port))["pong"] is True
    finally:
        await facade.stop()


async def _ping(host: str, port: int) -> dict:
    """GET `/__ping__` off the event loop (a blocking fetch would deadlock the
    very server we are asking, since both live in this loop)."""
    def _fetch():
        # Never route the local probe through a system proxy (Surge et al):
        # that is how the original report saw a 503 from the proxy instead of
        # ECONNREFUSED from the actually-dead loopback bind.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/__ping__", timeout=5) as r:
            return json.loads(r.read().decode())

    return await asyncio.to_thread(_fetch)


# --- BUG A, part 2: the `fix` text must not point at a dead end -------------

def _endpoint(url: str, source: str):
    from browserwright.daemon_url import DaemonEndpoint
    return DaemonEndpoint(url=url, explicit=False, source=source)


def test_fix_names_the_divergence_instead_of_saying_start_the_daemon(
        monkeypatch, tmp_path):
    """The reported failure: daemon up on a tailnet IP, client on 127.0.0.1.

    The old text was `start the single global daemon: browserwright-daemon
    serve` — for a daemon that was already running. It must instead name both
    addresses and how to reconcile them.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": "http://100.72.20.32:19990", "pid": 31305}))

    import browserwright.daemon_url as du
    from browserwright.daemon._ipc import EndpointProbe

    # The daemon really was up on the tailnet address in the report; the
    # diagnosis probes it and finds it (ADR-0013 rule 3).
    monkeypatch.setattr(du, "probe", lambda host, port, timeout=1.5: EndpointProbe(
        kind="ours" if host == "100.72.20.32" else "refused",
        host=host, port=port, pid=31305, version="0.17.1"))

    fix = du.local_unreachable_fix(
        _endpoint("http://127.0.0.1:19990", "default"))
    assert "browserwright-daemon serve" not in fix
    assert "100.72.20.32:19990" in fix
    assert "127.0.0.1:19990" in fix
    assert "BW_DAEMON_URL" in fix


def test_fix_for_a_stale_non_loopback_state_file_explains_the_interface(
        monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": "http://100.72.20.32:19990", "pid": 31305}))

    from browserwright.daemon_url import local_unreachable_fix

    fix = local_unreachable_fix(
        _endpoint("http://100.72.20.32:19990", "state_file"))
    # ADR-0013 rule 3: the text reports what the probes found (nothing at
    # the published address, nothing on loopback) and points at doctor /
    # the log — it never tells an agent to restart a daemon it could not
    # even reach.
    assert "100.72.20.32:19990" in fix
    assert "nothing is listening" in fix
    assert "restart" not in fix
    assert "browserwright doctor" in fix


def test_fix_with_no_daemon_at_all_points_at_the_on_demand_start(
        monkeypatch, tmp_path):
    """No state file, nothing running: the default endpoint starts a daemon
    on demand, so the fix explains why that did not happen (doctor, logs)
    rather than telling the agent to `serve` one (ADR-0013 rule 3)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    from browserwright.daemon_url import local_unreachable_fix

    fix = local_unreachable_fix(
        _endpoint("http://127.0.0.1:19990", "default"))
    assert "browserwright-daemon serve" not in fix
    assert "on demand" in fix
    assert "browserwright-daemon logs" in fix


def test_fix_names_a_foreign_responder_on_the_port(monkeypatch, tmp_path):
    """The 2026-09-01 shape: a proxy (Surge) answered 503 on 127.0.0.1:19990.
    The old text said "nothing answered … then restart"; the agent restarted
    a healthy daemon. Now the probe's HTTP status and Server header are in
    the message and the next step is to find the squatter, not restart."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    import browserwright.daemon_url as du
    from browserwright.daemon._ipc import EndpointProbe

    monkeypatch.setattr(du, "probe", lambda host, port, timeout=1.5: EndpointProbe(
        kind="foreign", host=host, port=port,
        status_line="HTTP/1.1 503 Service Unavailable", server="Surge/5.0"))

    fix = du.local_unreachable_fix(_endpoint("http://127.0.0.1:19990", "default"))
    assert "something other than browserwright" in fix
    assert "503" in fix
    assert "Surge/5.0" in fix
    assert "lsof" in fix
    assert "restart" not in fix
    assert "serve" not in fix


def test_session_unreachable_carries_the_actionable_fix(monkeypatch, tmp_path):
    """End-to-end through the real raise site, not just the helper."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("BW_DAEMON_URL", raising=False)
    monkeypatch.delenv("BD_CONFIG", raising=False)
    (tmp_path / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": "http://100.72.20.32:19990", "pid": 31305}))

    from browserwright.session import Session

    sess = Session.__new__(Session)
    err = sess._unreachable("ws://127.0.0.1:19990/control?client=skill-s1172",
                            OSError(61, "Connection refused"))
    assert "100.72.20.32" in (err.fix or "")
    assert (err.fix or "").strip() != ""


# --- BUG A, part 3: doctor must be able to SEE this failure -----------------

def test_doctor_fails_when_the_daemon_answers_but_loopback_does_not(
        monkeypatch):
    """`browserwright doctor` was fully green during the outage.

    It only echoed the advertised address. A check that cannot observe the
    reported failure is the gap, not a passing check.
    """
    from browserwright import health

    monkeypatch.setattr(
        health, "daemon_endpoint",
        lambda: _endpoint("http://100.72.20.32:19990", "state_file"),
        raising=False)
    import browserwright.daemon_url as du
    monkeypatch.setattr(
        du, "daemon_endpoint",
        lambda **_k: _endpoint("http://100.72.20.32:19990", "state_file"))

    def _probe(host, port, timeout=1.5):
        return None if host == "100.72.20.32" else "Connection refused"

    monkeypatch.setattr(health, "_probe_tcp", _probe)

    (check,) = health._endpoint_reachability_checks()
    assert check["name"] == "endpoint_reachable"
    assert check["status"] == "fail"
    assert "127.0.0.1" in check["message"]
    assert check["fix"].strip()


def test_doctor_passes_once_loopback_is_co_bound(monkeypatch):
    from browserwright import health
    import browserwright.daemon_url as du

    monkeypatch.setattr(
        du, "daemon_endpoint",
        lambda **_k: _endpoint("http://100.72.20.32:19990", "state_file"))
    monkeypatch.setattr(health, "_probe_tcp",
                        lambda host, port, timeout=1.5: None)

    (check,) = health._endpoint_reachability_checks()
    assert check["status"] == "pass"


def test_doctor_fails_when_nothing_answers_anywhere(monkeypatch):
    from browserwright import health
    import browserwright.daemon_url as du

    monkeypatch.setattr(
        du, "daemon_endpoint",
        lambda **_k: _endpoint("http://127.0.0.1:19990", "default"))
    monkeypatch.setattr(health, "_probe_tcp",
                        lambda host, port, timeout=1.5: "Connection refused")

    (check,) = health._endpoint_reachability_checks()
    assert check["status"] == "fail"
    assert "nothing answered" in check["message"]
