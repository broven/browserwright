"""Issue #39 — the LaunchAgent plist must revive the daemon after a clean exit.

The plist's ``KeepAlive`` dictionary is the daemon's only contract with
launchd about resurrection. Two failure modes have to stay told apart, not
traded (issue #39):

- #15 crash-loop: ``Crashed=true`` revives a non-zero exit. That was the
  original respawn trigger — a half-alive port-holding daemon used to loop
  forever on EADDRINUSE. The loop is healed in ``serve`` (stale-port
  reclaim, issue #15 2.2), so the plist must keep reviving crashes.
- #39 silent death: ``SuccessfulExit=false`` classifies a clean exit-0 (a
  graceful `stop`, the control-socket watchdog self-exit) as "finished,
  never revive" — launchd deliberately never restarts it, and the global
  daemon stays dead until a human runs `restart` or `launchctl kickstart`.

launchd's dictionary semantics: each key is an independent keep-alive
condition, OR-ed together.

``Crashed`` does NOT mean "exited non-zero" — per ``launchd.plist(5)`` it means
the job "exited due to a signal which is typically associated with a crash".
This file used to model it as `exit_code == 0 -> SuccessfulExit, else Crashed`,
which made ``SuccessfulExit=true`` + ``Crashed=true`` look like "revive on every
exit". It is not, and the difference is load-bearing: measured on macOS 26.1
with exactly this dict, a job exiting 0 comes back (``runs = 2``) and a job
exiting 1 does not (``runs = 1``, ``state = not running``). A test that models
launchd wrongly reports green while the property it guards is violated — the
same shape of failure as issue #57 itself, one layer up.
"""
from __future__ import annotations

import plistlib

from browserwright.daemon import launchagent


def _build_plist(monkeypatch) -> dict:
    monkeypatch.setattr(launchagent, "resolve_daemon_bin",
                        lambda: "/bin/browserwright-daemon")
    return plistlib.loads(
        launchagent.build_plist(extension_port=None).encode("utf-8"))


def _revives_on_exit(keepalive, *, exit_code: int = 0,
                     signaled: bool = False) -> bool:
    """launchd's KeepAlive decision for one exit.

    Three outcomes, not two: a clean exit (``SuccessfulExit``), a signal-death
    (``Crashed``), and a non-zero exit with no signal — which satisfies neither
    condition and is therefore never revived.
    """
    if keepalive is True:
        return True
    if not isinstance(keepalive, dict):
        return False
    if signaled:
        return keepalive.get("Crashed") is True
    if exit_code == 0:
        return keepalive.get("SuccessfulExit") is True
    return False


def test_plist_keepalive_revives_clean_exit0(monkeypatch):
    """A graceful exit-0 must NOT be classified "finished, never revive".

    Before the fix the plist said ``SuccessfulExit=false``: launchd treats a
    clean exit as the job finishing successfully and deliberately never
    restarts it — permanent silent death (issue #39).
    """
    plist = _build_plist(monkeypatch)
    assert plist["RunAtLoad"] is True
    keepalive = plist["KeepAlive"]
    assert _revives_on_exit(keepalive, exit_code=0), (
        "KeepAlive must revive a clean exit-0 shutdown; got "
        f"{keepalive!r} — launchd would classify the exit as finished and "
        "never restart the daemon")


def test_plist_keepalive_still_revives_crashes(monkeypatch):
    """A crash (signal death) must still revive — the #15 mode stays covered."""
    keepalive = _build_plist(monkeypatch)["KeepAlive"]
    assert _revives_on_exit(keepalive, signaled=True), (
        "KeepAlive must revive a crashed (signal-killed) job; got "
        f"{keepalive!r}")


def test_plist_keepalive_does_not_revive_a_nonzero_exit(monkeypatch):
    """The gap this plist does NOT cover, asserted so nobody re-forgets it.

    A non-zero exit with no signal satisfies neither ``SuccessfulExit`` nor
    ``Crashed``, so launchd files the job under "not running" and walks away.
    ``serve``'s "already running (pid N)" self-deferral (``listener.py``) exits
    exactly like that, which is why a bounce racing a live incumbent used to
    leave the LaunchAgent permanently dead while `restart` reported
    ``ok: true`` (issue #57).

    This is a characterisation test, not a wish: it pins the *current* contract
    so that changing it is a deliberate act. `restart` covers the gap on its
    side by stopping the incumbent before bouncing, rather than by widening
    KeepAlive here and re-opening the #15 crash-loop question.
    """
    keepalive = _build_plist(monkeypatch)["KeepAlive"]
    assert not _revives_on_exit(keepalive, exit_code=1, signaled=False), (
        "This plist is not expected to revive a plain non-zero exit. If you "
        "changed KeepAlive to cover it, re-read the #15 crash-loop reasoning "
        "in build_plist() and update restart()'s incumbent handling to match.")
