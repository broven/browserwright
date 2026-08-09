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
    """Vector B: `_ipc.cleanup_endpoint()` unlinks the socket in this dir.

    Pointed at the default `/tmp`, that deletes the live daemon's control socket
    and its watchdog self-exits with status 0 — a death that looks like a clean
    shutdown (issue #15 2.4).
    """
    from browserwright.daemon import _ipc

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    assert runtime, "the autouse wall should have set XDG_RUNTIME_DIR"
    assert Path(runtime) != Path("/tmp")
    assert _ipc.sock_path() != Path("/tmp/browserwright-daemon.sock")


def test_runtime_dir_is_short_enough_for_af_unix():
    """`sun_path` is 104 bytes on macOS, which is why this is not `tmp_path`."""
    from browserwright.daemon import _ipc

    assert len(str(_ipc.sock_path()).encode()) < 104


def test_cold_start_entry_points_are_neutralised():
    """Vector A, layer 1: wandering into them must not start a daemon."""
    from browserwright import mode_b_client, session_create

    assert session_create._ensure_daemon_running() is None
    assert mode_b_client.ModeBClient._spawn_daemon(object()) is None


def test_a_real_detached_spawn_is_a_loud_failure():
    """Vector A, layer 2: a *new* path to a real spawn fails by name.

    The two entry points above are no-ops so ordinary tests need not know they
    exist; this backstop is what turns "someone found a third way to Popen a
    daemon" into a red test instead of a leaked process.
    """
    from browserwright import session_create

    with pytest.raises(AssertionError) as e:
        session_create._spawn_detached(["browserwright-daemon", "serve"])
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
