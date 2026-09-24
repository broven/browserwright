"""The wall in `tests/conftest.py` is armed. (issue #57 follow-up)

Without these, the wall can rot silently — and its failure mode is not a red
test but a developer's global daemon quietly dying mid-run, which is exactly the
kind of invisible breakage issue #57 was about. The symptom appears hours later
as "upgrade-global says success but the daemon is a version behind".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_runtime_dir_is_not_the_real_one():
    """Vector B: the runtime dir owns the pid + endpoint-state files.

    Pointed at the default `/tmp`, a test's `cleanup_endpoint()` would delete
    the live daemon's files, and its resolution would find the live daemon's
    published endpoint.
    """
    from browserwright.daemon import _ipc

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    assert runtime, "the autouse wall should have set XDG_RUNTIME_DIR"
    assert Path(runtime) != Path("/tmp")
    assert _ipc.pid_path() != Path("/tmp/browserwright-daemon.pid")


def test_runtime_dir_is_short_enough_for_af_unix():
    """`sun_path` is 104 bytes on macOS, which is why this is not `tmp_path`.

    The client-facing socket is gone (ADR-0011) but the per-session executor
    sockets still live here, so the budget still binds.
    """
    from browserwright.daemon import _ipc

    assert len(str(_ipc.executor_sock_path("some-session")).encode()) < 104


def test_the_endpoint_the_suite_resolves_is_not_the_real_one():
    """ADR-0011 vector B: isolation is port-based now.

    Nothing in the fast gate may resolve to `http://127.0.0.1:19990` — that is
    the developer's daemon, and an `is_alive()` landing there would silently
    couple a unit test to a live browser.
    """
    from browserwright.daemon_url import daemon_endpoint

    ep = daemon_endpoint()
    assert ep.url != "http://127.0.0.1:19990"
    # ...and not by way of an *explicit* source, which would also change the
    # auto-start regime the tests exercise.
    assert ep.explicit is False
    assert ep.source == "state_file"


def test_cold_start_entry_point_is_neutralised():
    """Vector A, layer 1: wandering into it must not start a daemon."""
    from browserwright import daemon_lifecycle

    assert daemon_lifecycle.ensure("test").healthy


def test_a_real_detached_spawn_is_a_loud_failure():
    """Vector A, layer 2: a *new* path to a real spawn fails by name.

    The entry point above is a no-op so ordinary tests need not know it
    exists; this backstop is what turns "someone found another way to Popen a
    daemon" into a red test instead of a leaked process.
    """
    from browserwright import daemon_lifecycle

    with pytest.raises(AssertionError) as e:
        daemon_lifecycle._spawn_detached(["serve"], initiator="test")
    assert "spawn a real browserwright-daemon" in str(e.value)


def _root_conftest():
    """Load `tests/conftest.py` by path.

    Not `import conftest`: three files in this repo are named `conftest`, and
    which one that resolves to depends on collection order.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_tests_root_conftest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_wall_exempts_the_e2e_suite():
    """e2e launches real daemons deliberately, with its own port isolation."""
    root_conftest = _root_conftest()

    root = Path("/repo")
    assert root_conftest._is_e2e(root / "tests/daemon/e2e/test_x.py", root)
    assert not root_conftest._is_e2e(root / "tests/daemon/test_x.py", root)
    assert not root_conftest._is_e2e(root / "tests/skill/test_x.py", root)
