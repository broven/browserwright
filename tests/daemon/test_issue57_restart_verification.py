"""Issue #57 — `restart` must prove the process was replaced, not assume it.

The bug: `restart()` was `unload` + `load` + `return {"ok": True}`, where `ok`
meant only "`launchctl load` exited 0". `upgrade-global` printed a completely
successful upgrade while the previous release's daemon kept serving.

Each test here is one of the ways that actually happened, and each one used to
return `ok: True`.
"""
from __future__ import annotations

import pytest

from browserwright.daemon import __version__ as INSTALLED
from browserwright.daemon import launchagent
from browserwright.daemon.launchagent import LaunchAgentError
from browserwright.daemon.restart_guard import Activity


#: A version that is definitely not the installed one. Derived rather than
#: hard-coded so this can never accidentally equal `INSTALLED`.
STALE = f"{INSTALLED}-stale"


class _Cfg:
    """Only the sliver of Config that `restart()` touches."""


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Wire `restart()` up to scriptable launchd + daemon stand-ins.

    `job_pids` is consumed one entry per `job_state()` call after the bounce, so
    a test can say "pid never changes" or "pid changes on the third poll".
    """
    plist = tmp_path / "com.browserwright-daemon.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(launchagent, "plist_path", lambda: plist)
    monkeypatch.setattr(launchagent, "require_darwin", lambda verb: None)
    monkeypatch.setattr(launchagent, "_stop_incumbent", lambda timeout: {"stopped": 111})
    monkeypatch.setattr(launchagent, "_log_tail", lambda lines=12: "")
    monkeypatch.setattr(launchagent, "time", _NoSleepClock())

    state = {
        "calls": [],
        "job_pids": [222],
        "live_version": INSTALLED,
        "activity": Activity(blocked=False, determinate=True, reasons=[]),
    }

    def fake_launchctl(*args):
        state["calls"].append(args)
        # Both legacy shims exit 0 even when they fail — measured on macOS 26.1.
        # Nothing in `restart()` may depend on this return code.
        return 0, "", "Load failed: 5: Input/output error"

    def fake_job_state():
        if not state["calls"]:
            return {"loaded": True, "pid": 111, "state": "running"}
        pids = state["job_pids"]
        pid = pids.pop(0) if len(pids) > 1 else pids[0]
        return {"loaded": True, "pid": pid,
                "state": "running" if pid is not None else "not running"}

    monkeypatch.setattr(launchagent, "launchctl", fake_launchctl)
    monkeypatch.setattr(launchagent, "job_state", fake_job_state)
    monkeypatch.setattr(launchagent, "_live_daemon_version",
                        lambda cfg: state["live_version"])
    monkeypatch.setattr("browserwright.daemon.restart_guard.probe",
                        lambda cfg, **kw: state["activity"])
    return state


class _NoSleepClock:
    """monotonic that advances only when someone sleeps — keeps tests instant."""

    def __init__(self):
        self._t = 0.0

    def monotonic(self):
        return self._t

    def sleep(self, seconds):
        self._t += seconds


def test_restart_returns_ok_when_pid_changed_and_version_matches(harness):
    harness["job_pids"] = [222]
    report = launchagent.restart(_Cfg(), timeout=2.0)
    assert report["ok"] is True
    assert report["pid_before"] == 111
    assert report["pid_after"] == 222
    assert report["daemon_version"] == INSTALLED


def test_restart_fails_when_pid_never_changes(harness):
    """The headline symptom: `lsof` showed the same pid before and after."""
    harness["job_pids"] = [111]
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg(), timeout=1.0)
    assert "unchanged" in str(e.value)
    assert e.value.exit_code == 3


def test_restart_fails_when_launchctl_lies_with_rc_zero(harness):
    """`load` exits 0 while printing `Load failed: 5` — proven on macOS 26.1.

    This is why the old `if rc != 0: raise` could essentially never fire, and
    why re-adding an rc check must not be mistaken for a fix.
    """
    harness["job_pids"] = [111]
    with pytest.raises(LaunchAgentError):
        launchagent.restart(_Cfg(), timeout=1.0)
    # It really did run the commands and really did get rc=0 back.
    assert ("load", "-w", str(launchagent.plist_path())) in harness["calls"]


def test_restart_fails_when_new_process_dies_on_startup(harness):
    """`serve` self-defers with exit 1 and launchd does not revive it.

    The job stays loaded with no pid. Before #57 this returned ok, and the
    operator was left with no daemon at all.
    """
    harness["job_pids"] = [None]
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg(), timeout=1.0)
    msg = str(e.value)
    assert "no running process" in msg
    assert "does not revive a non-zero exit" in msg


def test_restart_fails_when_live_daemon_reports_the_old_version(harness):
    """A new pid is not new code — the `uv tool install` window (defect 3).

    A daemon revived while the tool tree was being rewritten imports the old
    code and holds it for life. Only the version the *live process* reports
    settles it.
    """
    harness["job_pids"] = [222]
    harness["live_version"] = STALE
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg(), timeout=1.0)
    msg = str(e.value)
    assert STALE in msg and INSTALLED in msg
    # It must point at the foreign-daemon possibility rather than start
    # signalling strangers (issue #44 B).
    assert "#44 B" in msg


def test_restart_fails_when_nothing_answers_status(harness):
    harness["job_pids"] = [222]
    harness["live_version"] = None
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg(), timeout=1.0)
    assert "nothing answered" in str(e.value)


def test_restart_surfaces_the_daemon_log_tail(harness, monkeypatch):
    """The daemon narrates its own refusal; #57 never read it back."""
    monkeypatch.setattr(
        launchagent, "_log_tail",
        lambda lines=12: "browserwright-daemon already running (pid 85506)")
    harness["job_pids"] = [111]
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg(), timeout=1.0)
    assert "already running (pid 85506)" in str(e.value)


def test_restart_refuses_when_someone_is_working(harness):
    harness["activity"] = Activity(
        blocked=True, determinate=True,
        reasons=["1 session(s) with a live executor active in the last 300s: "
                 "7 (idle 4s)"])
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg(), timeout=1.0)
    msg = str(e.value)
    assert "refusing to restart" in msg
    assert "--force" in msg
    assert "7 (idle 4s)" in msg
    assert e.value.exit_code == 4
    # And it must not have touched launchd at all.
    assert harness["calls"] == []


def test_force_overrides_the_gate_and_reports_what_it_broke(harness):
    harness["activity"] = Activity(
        blocked=True, determinate=True, reasons=["executor(s) running: 7"])
    harness["job_pids"] = [222]
    report = launchagent.restart(_Cfg(), force=True, timeout=2.0)
    assert report["ok"] is True
    assert report["forced"] is True
    # Destructive and silent is the disease; destructive and itemised is the fix.
    assert report["interrupted"] == ["executor(s) running: 7"]


def test_expected_version_comes_from_the_launchagents_own_binary(tmp_path):
    """Not from whoever is running `restart`.

    `mise run restart-daemon` in a checkout runs the worktree's CLI while the
    plist points at the global install. Verifying against the caller's version
    would fail a restart that succeeded — after having already stopped the
    daemon, which is strictly worse than the bug being fixed.
    """
    import plistlib

    plist = tmp_path / "p.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "x",
        "ProgramArguments": ["/bin/echo", "serve"],
    }))
    assert launchagent.plist_program(plist) == "/bin/echo"
    # `/bin/echo version` prints "version"; the parser takes the last token.
    assert launchagent.expected_version(plist, "fallback") == "version"


def test_expected_version_falls_back_when_the_plist_is_unreadable(tmp_path):
    plist = tmp_path / "p.plist"
    plist.write_text("not a plist at all")
    assert launchagent.plist_program(plist) is None
    assert launchagent.expected_version(plist, "fallback") == "fallback"


def test_expected_version_falls_back_when_the_binary_fails(tmp_path):
    import plistlib

    plist = tmp_path / "p.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "x", "ProgramArguments": ["/bin/false"]}))
    assert launchagent.expected_version(plist, "fallback") == "fallback"


def test_restart_without_a_plist_is_a_distinct_error(harness, tmp_path,
                                                     monkeypatch):
    missing = tmp_path / "gone.plist"
    monkeypatch.setattr(launchagent, "plist_path", lambda: missing)
    with pytest.raises(LaunchAgentError) as e:
        launchagent.restart(_Cfg())
    assert e.value.exit_code == 2
