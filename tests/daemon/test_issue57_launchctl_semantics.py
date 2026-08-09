"""Issue #57 — what real launchd does, measured against real launchd.

Every other test in this area asserts against a *model* of launchd written by
whoever wrote the test. `test_issue39_launchagent_keepalive.py` shows how that
fails: its model said "non-zero exit == crash", which made the plist look like
it revived on every exit. It does not, the daemon relies on that being true, and
the test stayed green through a whole release.

So this file asks the operating system instead. It loads throwaway LaunchAgents
under a dedicated label, observes them, and boots them out again. It needs no
privileges and takes a few seconds.

Marked `launchd` and skipped off darwin. CI is `ubuntu-latest`, so this never
runs there — it is a local gate, and that is honest rather than ideal: the
findings it locks in were only ever discoverable on a Mac.

    mise run test:launchd

Safety: the label is `com.browserwright-test-<pid>`, never the real
`com.browserwright-daemon`, and every test boots its job out in a finally.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.launchd,
    pytest.mark.skipif(sys.platform != "darwin", reason="LaunchAgent is macOS-only"),
]


LABEL = f"com.browserwright-test-{os.getpid()}"


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _launchctl(*args) -> tuple[int, str, str]:
    p = subprocess.run(["launchctl", *args], capture_output=True, text=True,
                       timeout=15)
    return p.returncode, p.stdout, p.stderr


def _plist_body(program: list[str], keepalive: str) -> str:
    args = "\n        ".join(f"<string>{a}</string>" for a in program)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'    <key>Label</key><string>{LABEL}</string>\n'
        '    <key>ProgramArguments</key>\n'
        f'    <array>\n        {args}\n    </array>\n'
        '    <key>RunAtLoad</key><true/>\n'
        f'{keepalive}'
        '</dict>\n</plist>\n'
    )


#: The exact KeepAlive dict `build_plist()` emits.
_KEEPALIVE_DICT = (
    '    <key>KeepAlive</key>\n'
    '    <dict>\n'
    '        <key>SuccessfulExit</key><true/>\n'
    '        <key>Crashed</key><true/>\n'
    '    </dict>\n'
)


@pytest.fixture
def job(tmp_path):
    """Install a throwaway LaunchAgent; always boot it out afterwards."""
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)

    def install(program, keepalive=_KEEPALIVE_DICT):
        plist.write_text(_plist_body(program, keepalive))
        _launchctl("load", "-w", str(plist))
        return plist

    try:
        yield install
    finally:
        _launchctl("bootout", _target())
        plist.unlink(missing_ok=True)


def _print_field(field: str) -> str | None:
    rc, out, _ = _launchctl("print", _target())
    if rc != 0:
        return None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith(f"{field} = "):
            return s[len(field) + 3:].strip()
    return None


def _pid(retries: int = 20) -> int | None:
    for _ in range(retries):
        raw = _print_field("pid")
        if raw and raw.isdigit():
            return int(raw)
        time.sleep(0.2)
    return None


def test_legacy_unload_then_load_does_replace_the_process(job):
    """Refutes the issue's stated root cause.

    #57 proposed that `launchctl load`/`unload` are no-ops on modern macOS and
    that `kickstart -k` was needed. On macOS 26.1 the legacy pair replaces the
    process correctly — so switching verbs would have "fixed" the bug by
    changing nothing. This test exists to stop that theory coming back.
    """
    plist = job(["/bin/sleep", "100000"])
    before = _pid()
    assert before is not None

    _launchctl("unload", str(plist))
    _launchctl("load", "-w", str(plist))

    for _ in range(30):
        after = _pid(retries=1)
        if after is not None and after != before:
            break
        time.sleep(0.2)
    assert after is not None and after != before, (
        f"legacy unload+load left pid {before} in place")


def test_launchctl_load_exits_zero_even_when_it_fails(job):
    """Why `if rc != 0: raise` could never fire — the guard `restart()` had.

    Any fix that "surfaces the return code" instead of observing real state is
    not a fix.
    """
    plist = job(["/bin/sleep", "100000"])
    assert _pid() is not None

    rc, _out, err = _launchctl("load", "-w", str(plist))  # already loaded
    assert rc == 0, "if this ever becomes non-zero, rc is finally trustworthy"
    assert "failed" in err.lower(), (
        "expected the failure to be reported on stderr only; got "
        f"rc={rc} stderr={err!r}")


def test_launchctl_unload_exits_zero_when_not_loaded(job):
    plist = job(["/bin/sleep", "100000"])
    _launchctl("bootout", _target())
    rc, _out, err = _launchctl("unload", str(plist))
    assert rc == 0
    assert "failed" in err.lower()


def test_keepalive_revives_a_clean_exit(job):
    """The #39 contract, measured rather than modelled."""
    job(["/bin/sh", "-c", "exit 0"])
    deadline = time.time() + 15
    runs = None
    while time.time() < deadline:
        raw = _print_field("runs")
        if raw and raw.isdigit() and int(raw) >= 2:
            runs = int(raw)
            break
        time.sleep(0.5)
    assert runs and runs >= 2, (
        f"expected launchd to keep respawning an exit-0 job; runs={runs!r}")


def test_keepalive_does_not_revive_a_nonzero_exit(job):
    """The gap `restart()` has to cover on its own side.

    `serve` exits 1 when it finds an incumbent on the control socket. This dict
    satisfies neither `SuccessfulExit` (not a clean exit) nor `Crashed` (not a
    signal death), so launchd files the job under "not running" and walks away —
    which is what made a bounce that raced a live incumbent permanently fatal.
    """
    job(["/bin/sh", "-c", "exit 1"])
    time.sleep(6)
    runs = _print_field("runs")
    state = _print_field("state")
    assert runs == "1", (
        f"launchd revived a plain non-zero exit (runs={runs!r}); if this is a "
        "real macOS behaviour change, restart()'s incumbent handling and the "
        "KeepAlive note in build_plist() both need revisiting")
    assert state == "not running", f"unexpected state={state!r}"


def test_job_state_parses_real_launchctl_print_output(job):
    """`launchagent.job_state()` against real output, not a fixture string."""
    from browserwright.daemon import launchagent

    job(["/bin/sleep", "100000"])
    real_pid = _pid()
    assert real_pid is not None

    monkey_label = launchagent.LABEL
    try:
        launchagent.LABEL = LABEL
        state = launchagent.job_state()
    finally:
        launchagent.LABEL = monkey_label

    assert state["loaded"] is True
    assert state["pid"] == real_pid
    assert state["state"] == "running"


def test_job_state_reports_not_loaded_for_an_absent_job():
    from browserwright.daemon import launchagent

    monkey_label = launchagent.LABEL
    try:
        launchagent.LABEL = "com.browserwright-test-definitely-absent"
        state = launchagent.job_state()
    finally:
        launchagent.LABEL = monkey_label
    assert state == {"loaded": False, "pid": None, "state": None}
