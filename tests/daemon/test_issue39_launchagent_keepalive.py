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
condition, OR-ed together. So ``SuccessfulExit=true`` + ``Crashed=true``
means "revive on every exit" — exactly the always-on service the install
verb promises, with the anti-loop protection living in ``serve`` where it
can actually heal the cause.
"""
from __future__ import annotations

import plistlib

from browserwright.daemon import launchagent


def _build_plist(monkeypatch) -> dict:
    monkeypatch.setattr(launchagent, "resolve_daemon_bin",
                        lambda: "/bin/browserwright-daemon")
    return plistlib.loads(
        launchagent.build_plist(extension_port=None).encode("utf-8"))


def _revives_on_exit(keepalive, exit_code: int) -> bool:
    """launchd's KeepAlive decision for one exit: 0 = successful, else crash."""
    if keepalive is True:
        return True
    if isinstance(keepalive, dict):
        if exit_code == 0:
            return keepalive.get("SuccessfulExit") is True
        return keepalive.get("Crashed") is True
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
    assert _revives_on_exit(keepalive, 0), (
        "KeepAlive must revive a clean exit-0 shutdown; got "
        f"{keepalive!r} — launchd would classify the exit as finished and "
        "never restart the daemon")


def test_plist_keepalive_still_revives_crashes(monkeypatch):
    """A crash (non-zero exit) must still revive — the #15 mode stays covered."""
    keepalive = _build_plist(monkeypatch)["KeepAlive"]
    assert _revives_on_exit(keepalive, 1), (
        "KeepAlive must revive a crashed (non-zero) exit; got "
        f"{keepalive!r}")
