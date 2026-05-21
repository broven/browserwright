"""S6 client-side gates.

(A2-a) version coherence: when the *running* daemon is older than the
*installed* `browser-daemon` package, the connect/ensure path stops + respawns
the daemon. A MISSING running version (a daemon too old to advertise one) is
also treated as stale — one needless restart on first upgrade beats silent
failure.

(A2-b) `-32601` rewrite: an "unknown method" JSON-RPC error is rewritten into a
clear "daemon is stale, restart it" message that names the offending method,
instead of leaking the raw JSON-RPC envelope.

All seams are mocked — no live browser, no live daemon, no real subprocess.
"""
import pytest

from browser_skill.mode_b_client import ModeBClient


# ============================================================================
# (A2-a) version coherence
# ============================================================================

def _client_with_versions(monkeypatch, running, installed):
    """Build a ModeBClient whose version-probe seams are stubbed. Records every
    stop()/serve() the coherence path triggers so tests can assert restarts."""
    c = ModeBClient(name="default")
    monkeypatch.setattr(c, "running_daemon_version", lambda: running)
    monkeypatch.setattr(c, "installed_daemon_version", lambda: installed)
    calls = []
    monkeypatch.setattr(c, "_stop_daemon", lambda: calls.append("stop"))
    monkeypatch.setattr(c, "_spawn_daemon", lambda: calls.append("serve"))
    return c, calls


def test_coherent_versions_no_restart(monkeypatch):
    c, calls = _client_with_versions(monkeypatch, running="0.5.3", installed="0.5.3")
    restarted = c.ensure_version_coherent()
    assert restarted is False
    assert calls == []


def test_stale_running_version_triggers_stop_then_serve(monkeypatch):
    c, calls = _client_with_versions(monkeypatch, running="0.5.1", installed="0.5.3")
    restarted = c.ensure_version_coherent()
    assert restarted is True
    # Must stop the stale daemon BEFORE spawning the fresh one.
    assert calls == ["stop", "serve"]


def test_missing_running_version_treated_as_stale(monkeypatch):
    # Daemon too old to advertise a version → version probe returns None.
    c, calls = _client_with_versions(monkeypatch, running=None, installed="0.5.3")
    restarted = c.ensure_version_coherent()
    assert restarted is True
    assert calls == ["stop", "serve"]


def test_no_running_daemon_does_not_restart(monkeypatch):
    """If there's no daemon at all (not even reachable), coherence is a no-op —
    the normal ensure/spawn path owns cold-start, not the staleness guard."""
    c = ModeBClient(name="default")
    monkeypatch.setattr(c, "running_daemon_version", lambda: None)
    monkeypatch.setattr(c, "installed_daemon_version", lambda: "0.5.3")
    monkeypatch.setattr(c, "is_alive", lambda: False)
    calls = []
    monkeypatch.setattr(c, "_stop_daemon", lambda: calls.append("stop"))
    monkeypatch.setattr(c, "_spawn_daemon", lambda: calls.append("serve"))
    restarted = c.ensure_version_coherent()
    assert restarted is False
    assert calls == []


def test_unknown_installed_version_skips_check(monkeypatch):
    """If we can't determine the installed package version, we can't compare —
    fall through silently rather than thrash-restart."""
    c, calls = _client_with_versions(monkeypatch, running="0.5.1", installed=None)
    restarted = c.ensure_version_coherent()
    assert restarted is False
    assert calls == []


def test_version_probe_reads_status_json(monkeypatch):
    """running_daemon_version() must read the daemon's advertised version out
    of `status --json`, not invent it."""
    import browser_skill.mode_b_client as m

    class _Proc:
        returncode = 0
        stdout = '{"alive": true, "pid": 9, "version": "0.4.0"}'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    c = ModeBClient(name="default")
    assert c.running_daemon_version() == "0.4.0"


def test_version_probe_none_when_field_absent(monkeypatch):
    import browser_skill.mode_b_client as m

    class _Proc:
        returncode = 0
        stdout = '{"alive": true, "pid": 9}'  # legacy daemon, no version
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    c = ModeBClient(name="default")
    assert c.running_daemon_version() is None


def test_installed_version_reads_version_subcommand(monkeypatch):
    import browser_skill.mode_b_client as m

    class _Proc:
        returncode = 0
        stdout = "browser-daemon 0.5.3\n"
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _Proc())
    c = ModeBClient(name="default")
    assert c.installed_daemon_version() == "0.5.3"


# ============================================================================
# (A2-b) -32601 rewrite — must be GENERIC (any method, any future RPC)
# ============================================================================

def test_rewrite_unknown_method_mentions_method_and_restart():
    c = ModeBClient(name="default")
    err = {"code": -32601, "message": "unknown BrowserDaemon method: BrowserDaemon.fooBar"}
    msg = c.explain_rpc_error("BrowserDaemon.fooBar", err)
    low = msg.lower()
    assert "BrowserDaemon.fooBar" in msg          # names the offending method
    assert "stale" in low                          # diagnoses staleness
    assert "browser-daemon stop" in low            # actionable restart hint
    assert "browser-daemon serve" in low
    # Must NOT just leak the raw JSON-RPC code.
    assert "-32601" not in msg


def test_rewrite_is_generic_across_methods():
    """No hardcoded method name in the logic — works for any RPC."""
    c = ModeBClient(name="default")
    for method in ("BrowserDaemon.userscript.install",
                   "BrowserDaemon.totallyNewMethod",
                   "Page.navigate"):
        err = {"code": -32601, "message": f"unknown method: {method}"}
        msg = c.explain_rpc_error(method, err)
        assert method in msg
        assert "stale" in msg.lower()


def test_non_32601_error_passed_through_untouched():
    """Other error codes are real protocol errors, not staleness — don't
    rewrite them into a misleading 'restart the daemon' message."""
    c = ModeBClient(name="default")
    err = {"code": -32602, "message": "invalid params: missing url"}
    msg = c.explain_rpc_error("BrowserDaemon.openBackgroundTab", err)
    assert "invalid params: missing url" in msg
    assert "stale" not in msg.lower()


def test_is_stale_method_error_predicate():
    c = ModeBClient(name="default")
    assert c.is_stale_method_error({"code": -32601, "message": "x"}) is True
    assert c.is_stale_method_error({"code": -32000, "message": "x"}) is False
    assert c.is_stale_method_error({}) is False
    assert c.is_stale_method_error(None) is False
