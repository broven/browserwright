"""The one graceful-then-forced process termination skeleton.

Four places in this daemon end a process's life, and before this module each
had rewritten the same loop:

  1. ``cli stop``                       — SIGTERM the daemon we just pinged.
  2. ``_stale.reclaim_ports``           — SIGTERM a confirmed stale daemon.
  3. ``executor_registry._terminate``   — SIGTERM a session's executor.
  4. ``listener._kill_rdp_chrome``      — SIGTERM a daemon-owned rdp Chrome.

What they genuinely share is the *shape*: signal, poll a death predicate until
a deadline, escalate to SIGKILL, poll again. That shape lives here as
:func:`terminate` (steps 1-2) and :func:`wait_until` (steps 1-3).

What they do **not** share — and why this module deliberately stops short of a
single ``kill_everything()``:

* **What gets signalled.** (3) must signal a process *group* (`killpg`): the
  executor is spawned with ``start_new_session=True`` precisely so its
  grandchildren die with it. (1), (2) and (4) hold a bare pid and must signal
  exactly that pid.
* **What "dead" means.** (1) polls the daemon's IPC ping, (2) polls whether the
  TCP ports freed, (3) polls ``Popen.poll()``. These are three different
  observables, and each is the *right* one for its caller — a daemon that has
  stopped answering is done for `stop`'s purposes even if the process lingers a
  few ms. Hence ``is_dead`` is injected, never assumed.
* **Who owns the pid.** (3) owns the ``Popen`` it is killing, so it can reap the
  zombie and never needs to ask "is this still the process I meant?". (1) and
  (2) got their pid from an untrusted source (a ping response, an ``lsof``
  line), so they need an identity re-check between the SIGTERM and the SIGKILL
  — that is the ``guard`` parameter, and it is why the pid-reuse defence is not
  a wart on ``stop`` but an intrinsic part of signalling a pid you don't own.
* **Blocking model.** (3) is called from the asyncio event loop and must not
  block it, so it runs its escalation on a daemon thread. (1) is a CLI process
  with nothing better to do. (2) runs at daemon cold start, before the loop.
* **(4) has no escalation at all, on purpose.** It fire-and-forgets one SIGTERM
  and returns; waiting for Chrome to finish its profile writeback is what the
  orphan sweep at the next startup is for. Folding it into :func:`terminate`
  would give it an escalation it does not want.

So (1) and (2) share :func:`terminate` end to end; (3) shares :func:`wait_until`
and keeps its own group-signalling, thread-offloading body; (4) shares nothing
and stays a five-line function. That is the honest boundary.
"""
from __future__ import annotations

import enum
import os
import signal
import time
from typing import Callable


class Outcome(enum.Enum):
    """How a :func:`terminate` call ended."""

    ALREADY_GONE = "already_gone"
    """The pid did not exist when we tried to SIGTERM it."""

    EXITED = "exited"
    """SIGTERM was enough — ``is_dead`` went true within the grace window."""

    KILLED = "killed"
    """Needed SIGKILL, and ``is_dead`` went true after it (or was not checked;
    see ``kill_grace=0``)."""

    REFUSED = "refused"
    """``guard`` reported the pid is no longer the process we meant, *before* any
    signal was sent. Nothing was signalled."""

    REFUSED_ESCALATION = "refused_escalation"
    """SIGTERM went out, then ``guard`` reported the pid had been recycled before
    we could escalate. The SIGKILL was withheld. Distinct from
    :attr:`REFUSED` because the caller has already perturbed the system."""

    SIGNAL_FAILED = "signal_failed"
    """SIGTERM raised an OSError that is not ProcessLookupError (e.g. EPERM)."""

    ALIVE = "alive"
    """SIGKILL was sent and ``is_dead`` still never went true."""


def wait_until(pred: Callable[[], bool], timeout: float,
               *, interval: float = 0.15,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll ``pred`` until it returns True or ``timeout`` seconds elapse.

    Checks first, then sleeps, then checks once more after the deadline — so a
    ``timeout`` shorter than ``interval`` still gets a real answer rather than
    an automatic False. ``sleep`` is injected so callers on a thread that must
    not actually sleep (and tests) can pass their own.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        sleep(interval)
    return pred()


def pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process (POSIX ``kill(pid, 0)`` probe).

    ``PermissionError`` counts as alive: the process exists, it just isn't ours
    to signal. Non-positive pids are never alive (0 and negatives mean "process
    group" to ``kill(2)`` — asking about them is always a caller bug).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def terminate(pid: int, *, is_dead: Callable[[], bool],
              grace: float,
              kill_grace: float = 2.0,
              interval: float = 0.15,
              guard: Callable[[], bool] | None = None,
              sleep: Callable[[float], None] = time.sleep) -> Outcome:
    """SIGTERM ``pid``, wait for ``is_dead``, escalate to SIGKILL if needed.

    Args:
      is_dead: the caller's death observable. Called repeatedly; must be cheap
        and must never raise.
      grace: seconds to wait after SIGTERM before escalating.
      kill_grace: seconds to wait after SIGKILL to confirm death. Pass ``0`` to
        fire the SIGKILL and return :attr:`Outcome.KILLED` without confirming —
        appropriate when the caller is about to exit anyway.
      guard: identity re-check for a pid the caller does not own. Called before
        the SIGTERM and again before the SIGKILL; a False answer means the pid
        was recycled and *nothing* gets signalled. Callers that own the process
        (a ``Popen`` handle) should pass None.
      sleep: injected for tests / non-blocking contexts.
    """
    if guard is not None and not guard():
        return Outcome.REFUSED
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return Outcome.ALREADY_GONE
    except OSError:
        return Outcome.SIGNAL_FAILED
    if wait_until(is_dead, grace, interval=interval, sleep=sleep):
        return Outcome.EXITED
    if guard is not None and not guard():
        return Outcome.REFUSED_ESCALATION
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    if kill_grace <= 0:
        return Outcome.KILLED
    return (Outcome.KILLED
            if wait_until(is_dead, kill_grace, interval=interval, sleep=sleep)
            else Outcome.ALIVE)
