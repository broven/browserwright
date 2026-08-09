"""Repo-wide wall between the test suite and the developer's *real* daemon.

Running `mise run test` used to kill the machine-global daemon and leave a
stray one behind. Two independent vectors, both of which had to be closed:

**A — a test spawns a real daemon on the production ports.** `session_create`
and `ModeBClient` both cold-start `browserwright-daemon serve` on demand, and
with no `BD_EXTENSION_PORT` in the environment that binds **19989**, the port
the user's real Chrome extension dials. The new daemon then takes over the
control socket, the LaunchAgent-managed one exits with "already running", and
because launchd never revives a non-zero exit (see
`test_issue39_launchagent_keepalive`), the user's daemon is simply gone — while
a worktree daemon squats on the port until someone runs `mise run teardown`.

**B — a test deletes the global control socket.** `_ipc.cleanup_endpoint()`
unlinks `${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock`. Unset, that is the
real daemon's socket, and its watchdog self-exits the moment the file
disappears ("control socket removed; self-exiting to release ports", issue #15
2.4) — a clean exit 0, so it looks like nothing went wrong at all.

Both were previously defended against *per test*: eight files monkeypatch
`_ensure_daemon_running` one by one, and `e2e/helpers.py` builds its own port
"isolation wall" and documents this exact hazard. Per-test opt-in means the
protection is only as good as the next author's memory, and two tests in
`test_coverage_cli_runtime.py` had already forgotten it. This makes the wall the
default for the whole suite instead.

`tests/daemon/e2e/` is exempt: it launches real daemons on purpose, through
subprocesses whose environment it controls itself (`TEST_EXT_PORT`, its own
runtime dirs), and in-process monkeypatching would not reach them anyway.

**Boundary.** This wall is in-process. A test that shells out to `browserwright`
/ `browserwright-daemon` gets a child whose own `_ensure_daemon_running` we
cannot patch — such a child could still bind port 19989. It inherits the
redirected `XDG_RUNTIME_DIR` (that part *is* environmental), so it can no longer
evict the real daemon's control socket, which is the lethal half. No test in the
fast gate does this today; if you add one, pin `BD_EXTENSION_PORT` in its child
environment the way `e2e/helpers.py` does.

**Also out of scope:** a *different worktree* running its own suite concurrently.
Nothing in this process can stop that, and it looks identical from the outside —
if the daemon dies during a run, check whether the port holder belongs to
another checkout before suspecting this one.
"""
from __future__ import annotations

import shutil
import tempfile

import pytest


def _is_e2e(item_path, rootpath) -> bool:
    try:
        parts = item_path.relative_to(rootpath).parts
    except ValueError:
        return False
    return len(parts) >= 3 and parts[:3] == ("tests", "daemon", "e2e")


@pytest.fixture(scope="session")
def isolated_runtime_dir() -> str:
    """A short-path runtime dir shared by the whole session.

    Deliberately under `/tmp` and not `tmp_path`: AF_UNIX `sun_path` has a hard
    104-byte budget, and pytest's tmp dirs live under
    `/private/var/folders/...`, which blows it. `_ipc.runtime_dir()` hardcodes
    `/tmp` for the same reason.
    """
    path = tempfile.mkdtemp(prefix="bw-test-rt-", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def never_touch_the_global_daemon(request, monkeypatch, isolated_runtime_dir):
    """Autouse: no test may reach the real control socket or spawn a real daemon.

    Vector B is closed by pointing `XDG_RUNTIME_DIR` somewhere private, so a
    `cleanup_endpoint()` deletes our socket rather than the user's.

    Vector A is closed in two layers: the cold-start *entry points* become
    no-ops, so a test that merely wanders into them keeps working without having
    to know they exist; and the low-level detached spawn **raises**, so a future
    code path that reaches a real `Popen` fails loudly and by name instead of
    leaking a daemon nobody notices until an upgrade breaks.
    """
    if _is_e2e(request.path, request.config.rootpath):
        return

    monkeypatch.setenv("XDG_RUNTIME_DIR", isolated_runtime_dir)

    from browserwright import mode_b_client, session_create

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            f"{request.node.nodeid} tried to spawn a real browserwright-daemon "
            f"({args!r}). That binds the production ports and evicts the "
            "developer's global daemon. Stub the call, or use the e2e harness "
            "under tests/daemon/e2e/ which isolates ports properly."
        )

    # Layer 1: the two cold-start entry points do nothing.
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)
    monkeypatch.setattr(mode_b_client.ModeBClient, "_spawn_daemon",
                        lambda self, backend=None: None)
    # Layer 2: anything that still reaches a real spawn is a bug, not a leak.
    monkeypatch.setattr(session_create, "_spawn_detached", _forbidden)
