"""v0.3 concurrency refactor: ContextVar-backed sessions + with_session().

Verifies:
  - ``current_session()`` returns the singleton when no override is pushed.
  - ``with_session(sess)`` overrides the singleton for the block.
  - Threads / asyncio tasks each see their own pushed session — no cross-talk.
  - The singleton is restored after the block exits.
"""
from __future__ import annotations

import asyncio
import threading

import pytest


def _make_session():
    # Stub daemon → never actually touched in these tests.
    from browser_skill.session import Session

    class _Daemon:
        def resolve_ws_url(self):
            raise AssertionError("test should not touch the daemon")

        def invalidate(self):
            pass

    return Session(daemon=_Daemon())


def test_default_singleton_returned_with_no_override():
    from browser_skill.session import current_session

    a = current_session()
    b = current_session()
    assert a is b


def test_with_session_overrides_inside_block():
    from browser_skill.session import current_session, with_session

    default = current_session()
    fresh = _make_session()
    assert fresh is not default
    with with_session(fresh):
        assert current_session() is fresh
    # Reverts.
    assert current_session() is default


def test_with_session_nested_lifo():
    from browser_skill.session import current_session, with_session

    outer = _make_session()
    inner = _make_session()
    default = current_session()
    with with_session(outer):
        assert current_session() is outer
        with with_session(inner):
            assert current_session() is inner
        assert current_session() is outer
    assert current_session() is default


def test_threads_have_independent_overrides():
    """Each thread sees its own ``ContextVar`` override; no cross-talk.

    We use ``contextvars.copy_context().run(...)`` to make sure each thread
    starts with its own context (the documented pattern for thread isolation
    with ``ContextVar``).
    """
    import contextvars

    from browser_skill.session import current_session, with_session

    s1 = _make_session()
    s2 = _make_session()
    observed: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(label, sess):
        def _body():
            with with_session(sess):
                # Wait until the other thread has also pushed its session so
                # we're observing them simultaneously — that's the actual
                # concurrency invariant we care about.
                barrier.wait(timeout=2)
                observed[label] = current_session()

        contextvars.copy_context().run(_body)

    t1 = threading.Thread(target=worker, args=("a", s1))
    t2 = threading.Thread(target=worker, args=("b", s2))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert observed["a"] is s1
    assert observed["b"] is s2


def test_asyncio_tasks_have_independent_overrides():
    """ContextVar is the supported asyncio task-locals primitive — verify
    overrides survive ``await`` boundaries and don't leak to siblings."""
    from browser_skill.session import current_session, with_session

    s1 = _make_session()
    s2 = _make_session()
    observed: dict[str, object] = {}

    async def task(label, sess):
        with with_session(sess):
            await asyncio.sleep(0)
            observed[label] = current_session()

    async def main():
        await asyncio.gather(task("a", s1), task("b", s2))

    asyncio.run(main())
    assert observed["a"] is s1
    assert observed["b"] is s2


def test_session_close_does_not_break_default():
    """Closing a pushed session must not affect the default singleton."""
    from browser_skill.session import current_session, with_session

    default = current_session()
    fresh = _make_session()
    with with_session(fresh):
        fresh.close()
    # Default still usable afterwards.
    assert current_session() is default


def test_run_task_isolated_uses_pushed_session(tmp_bs_home, monkeypatch):
    """``task_runner.run_task(..., isolated=True)`` should push a fresh
    Session for the duration of run(). We verify by having the task
    snapshot ``current_session()`` and confirming it's not the singleton."""
    import importlib
    import sys

    # Reset modules so the fixture-modified $BS_HOME is honored.
    for k in list(sys.modules):
        if k.startswith("browser_skill"):
            del sys.modules[k]

    from browser_skill.memory.site_mem import site_skills_root
    from browser_skill.session import current_session, Session

    # Stub the auto_client picker so Session() never tries to spawn a daemon.
    class _StubDaemon:
        def resolve_ws_url(self):
            return "ws://stub/never-used"

        def invalidate(self):
            pass

    monkeypatch.setattr(
        "browser_skill.mode_b_client.auto_client",
        lambda *_a, **_k: _StubDaemon(),
    )

    # Plant a task that records which session is active when it runs.
    site_dir = site_skills_root() / "isotest"
    (site_dir / "tasks").mkdir(parents=True)
    (site_dir / "memory.md").write_text("# isotest\n", encoding="utf-8")
    (site_dir / "tasks" / "checksess.py").write_text(
        '"""record current session id."""\n'
        'ARGS = {}\n'
        'def selftest(): return True\n'
        'def run(args, ctx=None):\n'
        '    from browser_skill.session import current_session\n'
        '    return id(current_session())\n',
        encoding="utf-8",
    )

    from browser_skill.task_runner import run_task

    # Capture default session id from outside.
    default_id = id(current_session())
    # Non-isolated: should match the default.
    same_id = run_task("isotest", "checksess")
    assert same_id == default_id
    # Isolated: should differ.
    iso_id = run_task("isotest", "checksess", isolated=True)
    assert iso_id != default_id
