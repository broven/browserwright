"""Phase B: daemon-side per-session executor registry + lazy spawn.

The daemon is ALREADY a per-session child-process manager (it spawns + tracks +
SIGTERMs per-session rdp Chrome). The executor is "rdp Chrome v2": same
supervision contract, a different child binary. PR1 builds only what
``ensureExecutor`` needs — a registry keyed by ``session_id`` + a single-flight
spawn guard so two concurrent first-heredocs can't double-spawn. FULL
supervision (idle reap / endSession kill / crash reap / orphan sweep) is PR2;
the registry shape here is deliberately the slot PR2 slots into.

Lifecycle ownership = the daemon (Fork 1a). Transport ownership = the executor's
own socket (Fork 2): the daemon only spawns + waits for the discovery file, then
hands the socket path back to the thin client.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .. import _ipc
from ..supervise import pid_alive as _pid_alive, wait_until

logger = logging.getLogger(__name__)

# How long to wait for a freshly-spawned executor to bind its socket + write its
# discovery file. This is now ONLY the process-start + socket-bind window
# (sub-second in practice) — the executor writes the discovery file BEFORE its
# slow facade cold-start (connect_over_cdp + bind), which is deferred to the
# worker's first execute on the data plane. So `ensureExecutor` returns fast and
# never holds the keepalive-sensitive control-plane RPC open for the connect.
# Kept generously above the bind window to tolerate a loaded interpreter start.
_SPAWN_READY_TIMEOUT_S = 15.0

# Grace window between SIGTERM and SIGKILL when reaping an executor (mirrors the
# rdp-Chrome teardown discipline — terminate, then escalate if it won't die).
_KILL_GRACE_S = 3.0


@dataclass
class ExecutorHandle:
    """A live per-session executor subprocess. PR2 grows this with idle/crash
    tracking; PR1 only needs the pid + socket path + a spawn lock."""

    session_id: str
    proc: subprocess.Popen
    sock_path: str
    executor_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # A spawned process owns the fixed session paths before it publishes its
    # discovery record. Keeping that provisional instance in the registry
    # makes every startup failure use the same exact-instance reaper as kill,
    # sweep, and cancellation paths; it must never be handed to a caller.
    ready: bool = True
    spawned_at: float = field(default_factory=time.monotonic)
    # Wall-clock spawn time, used to floor the discovery-file-mtime idle clock
    # (mtime is wall-clock; spawned_at above is a monotonic clock for races).
    spawned_wall: float = field(default_factory=time.time)

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def idle_seconds(self, *, now: float | None = None) -> float:
        """Seconds since this executor last did work.

        The signal is the executor's discovery-file mtime: the executor touches
        it after every served call (`_executor.process._touch_discovery`). We
        read the file rather than tracking activity daemon-side because the data
        plane bypasses the daemon entirely (Fork 2) — the daemon never sees the
        executes, so the file mtime is the cheapest accurate clock. Falls back
        to the spawn time when the file can't be stat'd (treat as just-spawned,
        never prematurely reaped)."""
        now = time.time() if now is None else now
        try:
            mtime = os.path.getmtime(_ipc.executor_file_path(self.session_id))
        except OSError:
            mtime = self.spawned_wall
        return max(0.0, now - max(mtime, self.spawned_wall))


class ExecutorRegistry:
    """``dict[session_id, ExecutorHandle]`` with single-flight lazy spawn.

    Mirrors ``Daemon.contexts`` keying. Hung off ``Daemon`` (NOT a holder):
    extension sessions multiplex onto one shared holder, so a per-session
    executor cannot live on the shared holder."""

    def __init__(self) -> None:
        self._handles: dict[str, ExecutorHandle] = {}
        # One lock per session id guards its spawn (the rdp `_open_lock`
        # equivalent — prevents the double-spawn race, Fork 1 risk).
        self._locks: dict[str, asyncio.Lock] = {}
        # Successful workspace teardown is terminal for this daemon lifetime.
        # Session ids are durable/unique, so retaining the result is both the
        # tombstone that rejects a queued ensure and an idempotent end result.
        self._terminal_results: dict[str, dict] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def ensure(self, session_id: str) -> str:
        """Get-or-spawn the session's executor; return its socket path.

        Idempotent + single-flight: concurrent callers for the same session
        serialize on the per-session lock, and a live handle short-circuits.

        Robust to a STALE discovery file (Fork 4 / daemon-restart): a fresh
        daemon has no in-memory handle, but a `bw-exec-*.json` from a prior
        (now-dead) executor may linger on disk. We treat such a file as ABSENT
        — `_spawn` unconditionally `cleanup_executor`s it before launching a
        fresh executor, so a dead pid's stale path can never be handed back."""
        async with self._lock_for(session_id):
            return await self._ensure_locked(session_id)

    async def ensure_with_preflight(
        self,
        session_id: str,
        preflight: Callable[[], Awaitable[None]],
    ) -> str:
        """Serialize upstream readiness and spawn with terminal teardown.

        Running readiness outside this lock is unsafe: an authorized ensure
        can reopen the browser after endSession closes it but before spawn wins
        the registry lock. The shared lifecycle gate covers both phases.
        """
        async with self._lock_for(session_id):
            self._raise_if_terminal(session_id)
            await preflight()
            return await self._ensure_locked(session_id)

    def _raise_if_terminal(self, session_id: str) -> None:
        if session_id in self._terminal_results:
            raise RuntimeError(f"session {session_id!r} has ended")

    async def _ensure_locked(self, session_id: str) -> str:
        self._raise_if_terminal(session_id)
        handle = self._handles.get(session_id)
        if handle is not None and handle.is_alive():
            if handle.ready:
                return handle.sock_path
            reap = await self._kill_and_wait_locked(
                session_id, executor_id=handle.executor_id)
            if reap.get("reaped") is not True:
                raise RuntimeError(
                    f"executor for session {session_id!r} failed startup and "
                    "could not be reaped")
            handle = None
        # Dead handle (crashed) → drop it and cold-spawn a fresh one.
        if handle is not None:
            self._handles.pop(session_id, None)
        # No in-memory handle (e.g. just-restarted daemon): a leftover
        # discovery file whose process is dead is worthless. Validate + purge.
        if not _discovery_alive(session_id):
            _ipc.cleanup_executor(session_id)
        handle = await self._spawn(session_id)
        self._handles[session_id] = handle
        return handle.sock_path

    async def terminate_session(
        self,
        session_id: str,
        teardown: Callable[[], Awaitable[dict]],
        *,
        budget: float | None = None,
    ) -> tuple[dict[str, object], dict]:
        """Atomically reap the executor and tear down its browser workspace.

        A successful result installs a terminal tombstone before releasing the
        per-session lock, so an ensure that was already authorized and queued
        cannot create a replacement. Failed/partial teardown is retryable and
        deliberately does not install the tombstone.
        """
        async with self._lock_for(session_id):
            cached = self._terminal_results.get(session_id)
            if cached is not None:
                return ({
                    "killed": False,
                    "reaped": True,
                    "matched": True,
                    "executor_id": None,
                }, dict(cached))
            handle = self._handles.get(session_id)
            executor_id = handle.executor_id if handle is not None else None
            reap = await self._kill_and_wait_locked(
                session_id, executor_id=executor_id)
            if reap.get("reaped") is not True:
                return reap, {}
            # Teardown adapters own their cooperative deadline. Hard-cancelling
            # this callback can land between a browser mutation and its durable
            # ledger checkpoint, creating the exact split-brain this lifecycle
            # lock exists to prevent. ``budget`` remains part of the boundary
            # for compatibility; Router supplies the deadline to its adapter.
            _ = budget
            result = await teardown()
            if isinstance(result, dict) and result.get("ok") is True:
                self._terminal_results[session_id] = dict(result)
            return reap, result

    async def _spawn(self, session_id: str) -> ExecutorHandle:
        """Spawn ``python -m browserwright._executor --session <id>`` detached
        (same ``start_new_session=True`` pattern as launch_chrome) and wait for
        its discovery file."""
        # Clear any stale discovery file from a prior (dead) executor so the
        # readiness wait below can't latch onto it.
        _ipc.cleanup_executor(session_id)
        executor_id = uuid.uuid4().hex
        cmd = [
            sys.executable,
            "-m",
            "browserwright._executor",
            "--session",
            session_id,
            "--executor-id",
            executor_id,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **_spawn_kwargs(),
        )
        handle = ExecutorHandle(
            session_id=session_id,
            proc=proc,
            sock_path=str(_ipc.executor_sock_path(session_id)),
            executor_id=executor_id,
            ready=False,
        )
        # Publish the provisional exact instance before the first await. The
        # caller holds the session lock, so nobody can reuse the fixed paths;
        # status/cleanup code can still identify the process if startup fails.
        self._handles[session_id] = handle
        try:
            sock_path = await self._await_ready(
                session_id, proc, executor_id=executor_id)
        except BaseException:
            # Cancellation is delayed until the same cancellation-resistant
            # exact-instance reaper used by kill/teardown confirms death. A
            # replacement therefore cannot publish at these fixed paths while
            # the old process can still unlink them during exit.
            await self._kill_and_wait_locked(
                session_id, executor_id=executor_id)
            raise
        handle.sock_path = sock_path
        handle.ready = True
        logger.info("spawned executor for session %s (pid=%s)", session_id, proc.pid)
        return handle

    async def _await_ready(
        self, session_id: str, proc: subprocess.Popen, executor_id: str | None = None
    ) -> str:
        """Poll the executor's ``_ipc`` discovery file until it appears (or the
        child dies / we time out).

        The discovery file now signals only that the executor's SOCKET IS
        LISTENING (bound) — NOT that the facade cold-start has completed. The
        executor publishes the file before connecting the facade; the slow
        connect+bind is deferred to the worker's first execute on the data
        plane. So this wait is fast (process start + bind), keeping the
        `ensureExecutor` control-plane RPC well under the daemon keepalive
        window. We still detect a child that dies before binding."""
        deadline = time.monotonic() + _SPAWN_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"executor for session {session_id!r} exited during "
                    f"cold-start (code={proc.returncode})"
                )
            record = _ipc.read_executor_record(session_id)
            if record is not None and (
                executor_id is None or record.get("executor_id") == executor_id
            ):
                return str(record["sock"])
            await asyncio.sleep(0.05)
        raise RuntimeError(
            f"executor for session {session_id!r} never became ready")

    # ---- introspection --------------------------------------------------

    def get(self, session_id: str) -> ExecutorHandle | None:
        return self._handles.get(session_id)

    def all_handles(self) -> list[ExecutorHandle]:
        return list(self._handles.values())

    # ---- PR2 supervision ------------------------------------------------

    async def kill(self, session_id: str) -> bool:
        """Reap the current exact executor before permitting replacement.

        The fixed IPC paths are keyed only by session, so fire-and-forget death
        is unsafe: the old process can run its final cleanup after a concurrent
        ``ensure`` has published a replacement. Delegate to the locked current-
        instance state machine and return only after its bounded reap attempt.
        """
        result = await self.kill_current_and_wait(session_id)
        return bool(result.get("killed"))

    async def kill_current_and_wait(
        self, session_id: str,
    ) -> dict[str, object]:
        """Reap whichever instance is current when the session lock is won.

        Session-terminal paths use this rather than snapshotting an exact id
        before acquiring the lock. That prevents a superseded old-id request
        from reporting success while a replacement is already current.
        """
        async with self._lock_for(session_id):
            handle = self._handles.get(session_id)
            executor_id = handle.executor_id if handle is not None else None
            return await self._kill_and_wait_locked(
                session_id, executor_id=executor_id)

    async def kill_and_wait(
        self,
        session_id: str,
        *,
        executor_id: str | None = None,
    ) -> dict[str, object]:
        """Terminate one exact executor instance and confirm it is gone.

        The per-session spawn lock stays held through reap confirmation.  A
        concurrent ``ensure`` therefore cannot create a replacement at the
        same socket path until the old process is dead and its discovery files
        are removed.

        ``executor_id`` prevents a delayed timeout cleanup from killing a newer
        executor.  When the registry already contains a different instance,
        the requested instance is considered superseded/reaped and the newer
        one is left untouched.
        """
        async with self._lock_for(session_id):
            return await self._kill_and_wait_locked(
                session_id, executor_id=executor_id)

    async def _kill_and_wait_locked(
        self,
        session_id: str,
        *,
        executor_id: str | None,
    ) -> dict[str, object]:
        handle = self._handles.get(session_id)
        if handle is None:
            record = _ipc.read_executor_record(session_id)
            if record is not None:
                recorded_id = record.get("executor_id")
                if executor_id is not None and recorded_id != executor_id:
                    return {
                        "killed": False,
                        "reaped": True,
                        "matched": False,
                        "executor_id": executor_id,
                        "current_executor_id": recorded_id,
                    }
                if _pid_alive(int(record["pid"])):
                    # The daemon cannot honestly confirm death for a live
                    # process it no longer owns with a Popen handle.
                    return {
                        "killed": False,
                        "reaped": False,
                        "matched": True,
                        "executor_id": executor_id,
                    }
            _ipc.cleanup_executor(session_id)
            return {
                "killed": False,
                "reaped": True,
                "matched": True,
                "executor_id": executor_id,
            }
        if executor_id is not None and handle.executor_id != executor_id:
            return {
                "killed": False,
                "reaped": True,
                "matched": False,
                "executor_id": executor_id,
                "current_executor_id": handle.executor_id,
            }

        # Keep the fixed-path lease (handle + session lock) until death is
        # confirmed. asyncio cancellation cannot stop the reaper thread, so it
        # must not release the lease early either.
        reap_task = asyncio.create_task(
            asyncio.to_thread(_terminate_and_wait, handle))
        cancelled: asyncio.CancelledError | None = None
        while not reap_task.done():
            try:
                await asyncio.shield(reap_task)
            except asyncio.CancelledError as e:
                cancelled = e
        reap_task.result()
        reaped = handle.proc.poll() is not None
        if reaped:
            if self._handles.get(session_id) is handle:
                self._handles.pop(session_id, None)
            _ipc.cleanup_executor(session_id)
        logger.info(
            "synchronously reaped executor for session %s "
            "(pid=%s, executor_id=%s, reaped=%s)",
            session_id,
            handle.proc.pid,
            handle.executor_id,
            reaped,
        )
        result = {
            "killed": True,
            "reaped": reaped,
            "matched": True,
            "executor_id": handle.executor_id,
        }
        if cancelled is not None:
            raise cancelled
        return result

    async def kill_all(self) -> None:
        """Reap the current executor for every session in a key snapshot."""
        snapshot = list(self._handles)
        await asyncio.gather(*(
            self.kill_current_and_wait(session_id)
            for session_id in snapshot
        ))

    def reap_dead(self) -> list[str]:
        """Crash-reap: drop every handle whose child has exited (it died on its
        own — e.g. the Fork-4 facade-death self-exit, or a segfault). Returns
        the session ids dropped. The next `ensure()` for those sessions
        cold-starts a fresh executor — mirrors `_on_upstream_closed` →
        `drop_rdp_context`."""
        dead: list[str] = []
        for session_id, handle in list(self._handles.items()):
            if not handle.is_alive():
                self._handles.pop(session_id, None)
                _ipc.cleanup_executor(session_id)
                dead.append(session_id)
                logger.info("reaped dead executor for session %s (code=%s)",
                            session_id, handle.proc.returncode)
        return dead

    async def reap_idle(self, idle_after: float) -> list[str]:
        """Idle-reap: SIGTERM + drop every executor idle longer than
        `idle_after` seconds. Returns the session ids reaped. Mirrors
        `_idle_watchdog` closing idle upstreams."""
        candidates: list[tuple[str, str]] = []
        now = time.time()
        for session_id, handle in list(self._handles.items()):
            if not handle.is_alive():
                continue
            idle_for = handle.idle_seconds(now=now)
            if idle_for >= idle_after:
                logger.info("idle-reap: executor %s idle %.1fs >= %.1fs",
                            session_id, idle_for, idle_after)
                candidates.append((session_id, handle.executor_id))
        results = await asyncio.gather(*(
            self.kill_and_wait(session_id, executor_id=executor_id)
            for session_id, executor_id in candidates
        ))
        return [
            session_id
            for (session_id, _executor_id), result in zip(candidates, results)
            if (result.get("reaped") is True
                and result.get("matched", True) is True)
        ]


def _discovery_alive(session_id: str) -> bool:
    """Whether the session's on-disk discovery file names a STILL-LIVE executor.

    Returns False when the file is absent OR names a dead pid — both cases mean
    the file is stale and must be purged before a fresh spawn (Fork 4). This is
    the robustness guard that keeps `ensureExecutor` from handing the thin
    client a dead socket after a daemon restart."""
    _sock, pid = _ipc.read_executor_file(session_id)
    if pid is None:
        return False
    return _pid_alive(pid)


def _spawn_kwargs() -> dict:
    """Detach the spawn from this process group — mirrors
    ``launch_chrome._spawn_kwargs`` so the executor survives independently and
    its pid is ours to reap (PR2)."""
    return {"start_new_session": True}


def _terminate(handle: ExecutorHandle) -> None:
    """SIGTERM the executor's whole process group, escalate to SIGKILL after a
    grace window. Mirrors `listener._kill_rdp_chrome` but signals the GROUP
    (the spawn used `start_new_session=True`, so the executor is a session
    leader — `killpg` reaps any grandchildren too). Best-effort + never raises.

    The initial SIGTERM is synchronous; the grace-wait + SIGKILL escalation +
    zombie reap run on a short-lived BACKGROUND daemon thread so we NEVER block
    the daemon's asyncio event loop (this is called from `_handle_end_session`,
    `_idle_watchdog`, and `_graceful_shutdown`, all on the loop — unlike
    `_kill_rdp_chrome` which only fire-and-forgets a SIGTERM, we additionally
    guarantee escalation without stalling the loop for the grace window)."""
    proc = handle.proc
    if proc.poll() is not None:
        return  # already gone
    _signal_terminate(proc)
    _escalate_async(proc, lambda: _killpg_or_kill(proc, signal.SIGKILL))


def _terminate_and_wait(handle: ExecutorHandle) -> None:
    """Blocking reap used only through ``asyncio.to_thread``."""
    proc = handle.proc
    if proc.poll() is not None:
        return
    _signal_terminate(proc)
    _wait_or_kill(proc, lambda: _killpg_or_kill(proc, signal.SIGKILL))


def _signal_terminate(proc: subprocess.Popen) -> None:
    """SIGTERM a detached executor process group, with pid fallback."""
    pid = proc.pid
    with _quiet():
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()


def _escalate_async(proc: subprocess.Popen, escalate) -> None:
    """Background-watch a SIGTERMed process: if it doesn't exit within the grace
    window, escalate (SIGKILL), then reap the zombie. Runs on a daemon thread so
    the caller (often the asyncio event loop) returns immediately."""
    t = threading.Thread(
        target=_wait_or_kill, args=(proc, escalate),
        name="bw-executor-reaper", daemon=True)
    t.start()


def _wait_or_kill(proc: subprocess.Popen, escalate) -> None:
    """Wait up to the grace window for the process to exit; escalate if it
    won't. Runs on a background thread (see `_escalate_async`), so the polling
    `time.sleep` never touches the event loop.

    After escalating (SIGKILL) we poll a few more times to REAP the zombie — the
    handle is dropped from the registry by the caller, so nothing else will
    `poll()`/`wait()` this pid; without a final reap a SIGKILLed child lingers as
    a zombie until the daemon exits.

    The waiting is `supervise.wait_until`; the signalling is NOT
    `supervise.terminate`, because this path must signal the process *group*
    (the executor is a session leader by design) and the SIGTERM was already
    sent by the caller before it offloaded to this thread."""
    def exited() -> bool:
        return proc.poll() is not None

    if wait_until(exited, _KILL_GRACE_S, interval=0.05):
        return
    with _quiet():
        escalate()
    # Reap the zombie now that it has been SIGKILLed (bounded short wait).
    wait_until(exited, 1.0, interval=0.02)


def _killpg_or_kill(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


class _quiet:
    """Swallow OS errors from best-effort signal/terminate calls."""

    def __enter__(self) -> "_quiet":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def cleanup_orphan_executors() -> None:
    """Startup orphan-sweep (mirrors `listener._cleanup_orphan_rdp_chrome`).

    A hard daemon crash / SIGKILL leaves executor subprocesses running + their
    `bw-exec-*.json` discovery files + `bw-exec-*.sock` sockets on disk. On the
    next daemon start we: read each discovery file, SIGTERM the pid it names (if
    that process is still alive), then unlink the stale socket + discovery file
    so a fresh `ensureExecutor` cold-starts clean.

    Conservative: we ONLY signal a pid we read from one of OUR discovery files —
    we never scan the system process table. Every step is wrapped so a
    permission error / race never crashes serve."""
    runtime_dir = _ipc._runtime_dir()
    if not runtime_dir.is_dir():
        return
    # C1: in-flight sidecars belong to processes we are about to signal below;
    # a leftover one would otherwise make `ps` report a call that ended when the
    # prior daemon died.
    try:
        inflight_dir = _ipc._ensure_executor_inflight_dir()
    except OSError:
        inflight_dir = None
    if inflight_dir is not None:
        for stale in inflight_dir.glob("bw-exec-*.inflight"):
            try:
                stale.unlink()
            except (FileNotFoundError, IsADirectoryError, OSError):
                pass
    for entry in runtime_dir.glob("bw-exec-*.json"):
        pid: int | None = None
        started: str | None = None
        try:
            import json
            d = json.loads(entry.read_text())
            raw = d.get("pid")
            if isinstance(raw, int) and 0 < raw < (1 << 31):
                pid = raw
            sock = d.get("sock")
            raw_started = d.get("start_time")
            started = raw_started if isinstance(raw_started, str) else None
        except (OSError, ValueError, TypeError):
            sock = None
        if pid is not None and not _terminate_orphan_and_wait(pid, started):
            # Fixed per-session paths cannot be reused while the old process
            # may still run its unconditional SIGTERM cleanup handler.
            logger.warning(
                "orphan-cleanup: executor pid %d did not exit; retaining %s",
                pid, entry.name)
            continue
        # Remove the stale discovery file + its socket.
        for p in (entry, entry.with_suffix(".sock"),
                  *( [_to_path(sock)] if isinstance(sock, str) else [] )):
            if p is None:
                continue
            try:
                p.unlink()
            except (FileNotFoundError, IsADirectoryError, OSError):
                pass


def _terminate_orphan_and_wait(pid: int, start_time: str | None = None) -> bool:
    """Bounded TERM→KILL reap for a process without a ``Popen`` handle.

    ``start_time`` is the fingerprint recorded when the discovery file was
    written. A crash can leave that file behind while its executor exits, and
    the OS is free to hand the pid to anything — so liveness alone does not
    say the pid is still ours, and signalling its whole *group* on that basis
    can take out an unrelated process tree. Three cases, deliberately graded:

    - recorded and matching  → ours; full group escalation.
    - recorded and differing → someone else's; do not signal at all, and
      report the orphan as gone so its stale files are cleaned up.
    - not recorded (a discovery file written before this field existed)
      → unverifiable; signal only the exact pid, never the group.
    """
    if not _pid_alive(pid):
        return True
    verified = False
    if start_time is not None:
        from ..platforms import proc_start_time
        current = proc_start_time(pid)
        if current is not None and current != start_time:
            logger.info(
                "orphan-cleanup: pid %d was recycled (start-time mismatch); "
                "not signalling", pid)
            return True
        verified = current is not None
    try:
        try:
            if not verified:
                raise PermissionError("unverified pid: exact-pid signal only")
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)
        logger.info("orphan-cleanup: SIGTERM stray executor pid %d", pid)
    except (ProcessLookupError, PermissionError, OSError):
        return not _pid_alive(pid)
    if wait_until(lambda: not _pid_alive(pid), _KILL_GRACE_S, interval=0.05):
        return True
    try:
        try:
            if not verified:
                raise PermissionError("unverified pid: exact-pid signal only")
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return wait_until(lambda: not _pid_alive(pid), 1.0, interval=0.02)


def _to_path(s: str):
    from pathlib import Path
    try:
        return Path(s)
    except (TypeError, ValueError):
        return None
