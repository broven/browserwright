"""The client side of the daemon's lifecycle: is it up, and make it so.

Every Layer 2 question about the daemon *process* — is one answering, is it
ours, is it the installed version, should it be started or replaced — is
answered here and nowhere else. The interface is three calls:

- :func:`diagnose` → one :class:`DaemonVerdict`. Probes the resolved endpoint
  (once, or twice with ``confirm=True`` — ADR-0013's two agreeing probes) and
  classifies what it found. Side-effect free.
- :func:`ensure` → the **only** code that starts or replaces the daemon. It owns
  the executor handoff, the initiator attribution (ADR-0012 rule 5), the
  ``LIFECYCLE`` log line, and the child environment. An explicitly configured
  endpoint is never started or replaced (ADR-0011, CONTEXT.md "endpoint").
- :func:`unreachable_fix` → the agent-facing ``fix`` for "nothing answered",
  built from what the probes observed (ADR-0013 rule 3), never from a guess.

Two adapters sit under the interface, and they are the test seams:

- :func:`probe` — one ``GET /__ping__`` classification of ``host:port``.
- :func:`run_verb` — runs one ``browserwright-daemon <verb>``, always with the
  child environment that carries this process's resolved endpoint.

Constructing a ``Session`` has no lifecycle side effects. Version coherence is
enforced when a caller asks for it — ``session new`` / ``recover`` — never as a
by-product of opening a connection.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from .daemon_url import (
    DEFAULT_DAEMON_URL,
    ENV_VAR,
    DaemonEndpoint,
    _from_state_file,
    _normalize,
    child_env,
    daemon_endpoint,
    unreachable_message,
)

#: The executable every verb runs. Resolved on ``PATH`` like any CLI.
DAEMON_BIN = "browserwright-daemon"

# ---- verdict ---------------------------------------------------------------

#: A browserwright daemon answers and runs the installed version.
UP = "up"
#: A browserwright daemon answers but runs a different (or unknown) version.
STALE = "stale"
#: Nothing listens on the default-regime endpoint.
DOWN = "down"
#: Something answers on the port, but it cannot be proven to be browserwright.
FOREIGN = "foreign"
#: Nothing listens on an explicitly configured endpoint (never auto-started).
UNREACHABLE = "unreachable"
#: ``confirm=True`` and the two probes disagreed; nothing is concluded.
UNDECIDED = "undecided"


@dataclass(frozen=True)
class DaemonVerdict:
    """What the endpoint looked like, classified once."""

    state: str
    endpoint: DaemonEndpoint
    installed: str
    detail: str
    pid: Optional[int] = None
    version: Optional[str] = None
    probes: tuple[str, ...] = field(default=())

    @property
    def up(self) -> bool:
        """A browserwright daemon answers (any version)."""
        return self.state in (UP, STALE)

    @property
    def healthy(self) -> bool:
        """A browserwright daemon answers and runs the installed version."""
        return self.state == UP

    @property
    def replaceable(self) -> bool:
        """Whether :func:`ensure` may start or replace it: proven gone or
        stale, on an endpoint nobody configured by hand."""
        return self.state in (DOWN, STALE) and not self.endpoint.explicit


def probe(host: str, port: int, timeout: float = 1.5):
    """One classified ``/__ping__`` of ``host:port`` (an ``EndpointProbe``).

    The seam every observation in this module goes through, so tests and the
    suite-wide wall in ``tests/conftest.py`` substitute observations without
    touching sockets."""
    from .daemon._ipc import probe_endpoint_sync
    return probe_endpoint_sync(host, port, timeout=timeout)


def _classify(pr, ep: DaemonEndpoint, installed: str) -> tuple[str, str]:
    if pr.kind == "ours":
        if pr.version != installed:
            return STALE, (f"the daemon at {ep.authority} runs "
                           f"{pr.version or 'an unknown version'}, installed "
                           f"is {installed}")
        return UP, (f"the daemon answers at {ep.authority} (pid {pr.pid}, "
                    f"version {pr.version})")
    if pr.kind == "refused":
        return (UNREACHABLE if ep.explicit else DOWN), pr.describe()
    return FOREIGN, pr.describe()


def diagnose(*, confirm: bool = False, endpoint: DaemonEndpoint | None = None,
             expected_version: str | None = None,
             timeout: float = 1.5) -> DaemonVerdict:
    """Classify the daemon at ``endpoint`` (default: the resolved one).

    ``confirm`` takes two probes and only concludes when they agree — the
    ADR-0013 rule for anything that may lead to a replacement. The installed
    version is this package's (``expected_version`` overrides it for the
    LaunchAgent, whose binary may differ from the checkout asking).
    """
    from .version import package_version

    ep = endpoint or daemon_endpoint()
    installed = expected_version or package_version()
    observed = [probe(ep.host, ep.port, timeout=timeout)
                for _ in range(2 if confirm else 1)]
    kinds = tuple(p.kind for p in observed)
    conclusions = [_classify(p, ep, installed) for p in observed]
    if len({c[0] for c in conclusions}) > 1:
        return DaemonVerdict(
            state=UNDECIDED, endpoint=ep, installed=installed, probes=kinds,
            detail=("two consecutive probes disagree "
                    f"({conclusions[0][0]} then {conclusions[1][0]}); "
                    "nothing is concluded from that"))
    last = observed[-1]
    state, detail = conclusions[-1]
    if confirm and state == UP:
        detail += " on both probes"
    return DaemonVerdict(state=state, endpoint=ep, installed=installed,
                         detail=detail, probes=kinds,
                         pid=last.pid if last.kind == "ours" else None,
                         version=last.version if last.kind == "ours" else None)


# ---- ensure ----------------------------------------------------------------


def ensure(reason: str, *, wait: float = 10.0) -> DaemonVerdict:
    """Make a current daemon serve the resolved endpoint; return the verdict.

    ``reason`` is recorded on the ``LIFECYCLE`` line and in the initiator the
    spawned daemon logs (e.g. ``"session new"``, ``"recover session=7"``).

    - **explicitly configured endpoint** — hands off. Nothing answering raises
      ``DaemonUnavailable``; a version skew is reported on stderr, never
      repaired: that daemon belongs to whoever configured the URL.
    - **default endpoint** — ours. ``DOWN`` / ``FOREIGN`` spawn ``serve``
      (which refuses beside a live daemon and reclaims ports only from a
      process it proves is browserwright, issue #15); ``STALE`` hands the
      resident executors over, stops the old daemon and spawns the installed
      one; ``UNDECIDED`` does nothing — a daemon mid-transition is not piled
      on. After a spawn, waits up to ``wait`` seconds for ``UP``.

    Never raises for the default endpoint: the returned verdict says whether
    it worked, and each caller decides what a failure means for it.
    """
    from .daemon import _ipc
    from .errors import DaemonUnavailable

    verdict = diagnose(confirm=True)
    if verdict.healthy:
        return verdict
    ep = verdict.endpoint
    if ep.explicit:
        if not verdict.up:
            raise DaemonUnavailable(unreachable_message(ep))
        if verdict.state == STALE:
            print(f"warning: {verdict.detail}. Because the endpoint was "
                  "configured explicitly, browserwright will not replace it — "
                  "update the daemon on that machine.", file=sys.stderr)
        return verdict
    if verdict.state == UNDECIDED:
        return verdict

    initiator = _ipc.describe_initiator(f"auto-start ({reason})")
    fields = {"reason": reason, "state": verdict.state,
              "probes": ",".join(verdict.probes), "detail": verdict.detail,
              "initiator": initiator}
    if verdict.state == STALE and verdict.pid is not None:
        _ipc.log_lifecycle("replace", pid_before=verdict.pid, **fields)
        # A version replacement, not an operator stop: leave the resident
        # executors for the new daemon to adopt (ADR-0013).
        _ipc.request_executor_handoff(verdict.pid)
        run_verb(["stop"], timeout=10.0, initiator=initiator)
    else:
        _ipc.log_lifecycle("spawn", **fields)
    child = _spawn_detached(["serve"], initiator=initiator)
    return _wait_until_up(child, ep, wait)


def _wait_until_up(child, ep: DaemonEndpoint, wait: float) -> DaemonVerdict:
    """Poll ``ep`` until a daemon answers, ``wait`` passes, or the spawned
    ``serve`` exits (it exits at once when it cannot bind — no point waiting
    the budget out). Returns the last verdict seen.

    Pinned to the endpoint that was diagnosed, never re-resolved: ``stop``
    removes the endpoint state file, and until the new ``serve`` publishes it
    again a fresh resolution falls through to the built-in default — which,
    for an isolated dev daemon, is the machine-global one (ADR-0012)."""
    deadline = time.monotonic() + wait
    while True:
        last = diagnose(endpoint=ep)
        if last.up or time.monotonic() >= deadline:
            return last
        if child is None or child.poll() is not None:
            return diagnose(endpoint=ep)
        time.sleep(0.2)


# ---- the browserwright-daemon adapter --------------------------------------


@dataclass(frozen=True)
class VerbResult:
    """What one ``browserwright-daemon <verb>`` run produced."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    #: The binary is not on ``PATH`` (``returncode`` 1).
    missing: bool = False
    #: The verb outlived its timeout (``returncode`` 3 — the daemon side may
    #: still be finishing, e.g. the issue #32 end-session join).
    timed_out: bool = False

    def json(self) -> dict | None:
        """The last non-empty stdout line as a JSON object, else None."""
        lines = [ln for ln in (self.stdout or "").strip().splitlines() if ln.strip()]
        if not lines:
            return None
        for text in (self.stdout.strip(), lines[-1]):
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        return None


def _verb_env(initiator: str | None) -> dict:
    """The child environment: this process's resolved endpoint (a
    ``--daemon-url`` flag lives in memory and would not reach a child —
    ADR-0011), plus the initiator a spawned daemon logs (ADR-0012 rule 5)."""
    from .daemon._ipc import INITIATOR_ENV
    env = child_env()
    if initiator:
        env[INITIATOR_ENV] = initiator
    return env


def run_verb(args: list[str], *, timeout: float | None = 10.0,
             initiator: str | None = None,
             capture: bool = True) -> VerbResult:
    """Run ``browserwright-daemon <args…>`` to completion. Never raises.

    ``capture=False`` lets the verb write straight to this terminal (the
    ``userscript`` passthrough); stdout/stderr are then empty here.
    """
    cmd = [DAEMON_BIN, *args]
    kwargs: dict = {"env": _verb_env(initiator), "timeout": timeout}
    if capture:
        kwargs.update(capture_output=True, text=True)
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as e:
        return VerbResult(returncode=1, stderr=str(e), missing=True)
    except subprocess.TimeoutExpired:
        return VerbResult(returncode=3, timed_out=True,
                          stderr=f"`{' '.join(cmd)}` timed out after {timeout}s")
    return VerbResult(returncode=proc.returncode,
                      stdout=getattr(proc, "stdout", None) or "",
                      stderr=getattr(proc, "stderr", None) or "")


def _spawn_detached(args: list[str], *, initiator: str):
    """Start ``browserwright-daemon <args…>`` detached from this process.

    Returns the ``Popen`` (so :func:`ensure` can tell a ``serve`` that exited
    at once from one still binding), or None when the binary is missing."""
    try:
        return subprocess.Popen(
            [DAEMON_BIN, *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
            env=_verb_env(initiator),
        )
    except FileNotFoundError:
        return None


# ---- diagnosis text (ADR-0013 rule 3: probe before blaming) ----------------


def _alternatives(ep: DaemonEndpoint) -> list:
    """Probe the resolved endpoint and, when they differ, the addresses a
    local client could have meant — the one the running daemon published, and
    the built-in default. Returns the probes in the order taken."""
    from urllib.parse import urlsplit

    probes = [probe(ep.host, ep.port)]
    seen = {(ep.host, ep.port)}
    candidates = []
    published = _from_state_file()
    if published:
        parts = urlsplit(_normalize(published))
        candidates.append((parts.hostname or "127.0.0.1", parts.port or 19990))
    default = urlsplit(DEFAULT_DAEMON_URL)
    candidates.append((default.hostname or "127.0.0.1", default.port or 19990))
    for host, port in candidates:
        if (host, port) in seen:
            continue
        seen.add((host, port))
        probes.append(probe(host, port))
    return probes


def unreachable_fix(ep: DaemonEndpoint) -> str:
    """The ``fix`` for "nothing answered" on a NON-explicitly-configured
    endpoint, built from what the probes observed.

    Four observed shapes, each with its own next step; none of them is
    "restart the daemon" — a client that could not connect has no evidence
    the daemon is at fault, and on 2026-09-01 that advice restarted a healthy
    daemon out from under another agent (ADR-0012, ADR-0013 rule 3).
    """
    from .version import package_version

    probes = _alternatives(ep)
    first, others = probes[0], probes[1:]
    findings = "; ".join(pr.describe() for pr in probes)

    # 1. A daemon answers somewhere this client did not look.
    for pr in others:
        if pr.answered:
            alt = f"http://{pr.host}:{pr.port}"
            return (
                f"{findings}. This client resolved {ep.url} (from "
                f"{ep.source}); the daemon is at {alt}. Point the client at it "
                f"(`export {ENV_VAR}={alt}`); `browserwright doctor` names the "
                "maintainer-side rebind that makes loopback answer too."
            )
    # 2. Something that is not browserwright holds the resolved port.
    if first.kind == "foreign":
        return (
            f"{findings}. A proxy or another program is answering on the "
            f"daemon's port, so nothing this client sends reaches browserwright. "
            f"Find it with `lsof -nP -iTCP:{first.port} -sTCP:LISTEN` and stop "
            "it, or move the daemon to another port. `browserwright-daemon "
            "status` reports what browserwright itself believes is running."
        )
    # 3. A daemon answers at the resolved address after all (a transient
    #    failure between the caller's attempt and this probe), possibly on
    #    the wrong version.
    if first.answered:
        installed = package_version()
        if first.version and first.version != installed:
            return (
                f"{findings}, but the installed package is {installed}. The "
                "running daemon is stale; `browserwright recover --session "
                "<id>` replaces it. `browserwright version check` shows both."
            )
        return (
            f"{findings} now — the failure was transient (the daemon was "
            "still coming up). Retry the command."
        )
    # 4. Nothing anywhere.
    return (
        f"{findings}. `browserwright session new` and `browserwright recover` "
        "start a daemon on demand on the default endpoint, and none is "
        "running now. "
        "`browserwright doctor` reports whether launchd manages the daemon "
        "and its last exit; `browserwright-daemon logs` holds the startup "
        "error."
    )
