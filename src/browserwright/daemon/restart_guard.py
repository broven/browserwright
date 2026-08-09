"""Who would a daemon restart interrupt? (issue #57)

`restart` is destructive in a way that is easy to miss. Every executor is a
child of the daemon, so bouncing the daemon zeroes out **every** session's live
Playwright state at once. The session rows survive (the ledger outlives every
process, see CONTEXT.md `session`) and so do the tab groups, but the `page` /
`context` / `state` objects an agent has been accumulating do not — and the
agent that loses them sees only "my page is gone", with nothing to tell it that
a *different* agent restarted the daemon underneath it. Its natural next move is
to call `restart` itself, which is how two agents end up bouncing each other.

So `restart` refuses when someone is working, and `--force` is the override.

**What counts as "working" is deliberately NOT executor liveness.** CONTEXT.md's
`idle clock` entry already settled that question for the neighbouring problem
(auto-prune): `last_seen` advances when a new *instruction* arrives,
"deliberately not with executor liveness (a stuck executor must not keep a
session alive forever)". Reusing that clock keeps one definition of "busy" in
the repo rather than inventing a second one — and it is load-bearing here, not
just tidy: `idle_close_after` defaults to ``None`` (`config.py`), so the idle
reaper in `listener.py` never runs and "has a live executor" stays true forever
after a single command. A gate that is always closed teaches agents to always
pass `--force`, which is indistinguishable from having no gate at all.

Explicitly NOT activity:
  - **connected extensions** — the user's Chrome is always connected;
  - **ledger sessions on their own** — they survive a restart by design;
  - **tab_count** — tabs survive too.

**Indeterminate is not blocked.** A daemon that cannot answer its own status RPC
within the probe timeout is not serving anyone, and that is precisely when an
agent legitimately needs to restart it. `status.snapshot()` is built to stay
answerable on a sick daemon (it takes no locks and mutates nothing) and the
executor data plane bypasses the daemon entirely, so a *busy* daemon still
replies — a timeout really does mean wedged. The report carries
``determinate=False`` so a caller can tell "nobody is working" apart from
"could not ask".
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

#: How recently a session must have received an instruction for its live
#: executor to count as someone working. Five minutes: agent steps are seconds
#: to tens of seconds apart, but a run waiting on a slow page or on a human
#: confirmation can go quiet for a couple of minutes without being abandoned.
#: Override with ``BD_RESTART_ACTIVE_WITHIN`` (seconds; <= 0 disables the
#: session limb of the predicate entirely).
ACTIVE_WITHIN_S = 300.0

#: Short on purpose. This runs in front of an operation a human or an agent is
#: waiting on, and a wedged daemon must not make `restart` hang before it has
#: even started.
PROBE_TIMEOUT_S = 2.0


def active_within_default() -> float:
    """The configured freshness window. Env beats the constant."""
    raw = os.environ.get("BD_RESTART_ACTIVE_WITHIN")
    if raw is None:
        return ACTIVE_WITHIN_S
    try:
        return float(raw)
    except ValueError:
        # A typo in an env var must not silently widen or disable the gate.
        from .errors import UserError

        raise UserError(
            f"BD_RESTART_ACTIVE_WITHIN must be a number, got {raw!r}") from None


@dataclass(frozen=True)
class Activity:
    """Whether a restart would interrupt someone, and the evidence for it."""

    #: True when at least one reason fired. `--force` overrides this, nothing else.
    blocked: bool
    #: False when the daemon could not be asked. Never blocks; see module docstring.
    determinate: bool
    #: Human-readable, one per firing signal, ordered most-specific first.
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "determinate": self.determinate,
            "reasons": list(self.reasons),
        }


def _fetch_snapshot(cfg, *, timeout: float) -> dict | None:
    """`BrowserwrightDaemon.status`, or None when the daemon can't answer.

    Every failure collapses to None on purpose: "refused", "no socket" and
    "timed out" all mean the same thing to this caller — we could not ask.
    """
    import asyncio

    from . import _rpc

    try:
        return asyncio.run(_rpc.call(
            cfg, "BrowserwrightDaemon.status", {},
            client_label="cli-restart", timeout=timeout))
    except Exception:  # noqa: BLE001 — the gate must never be the thing that fails
        return None


def _last_seen_by_session(sessions: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for rec in sessions or ():
        sid = rec.get("id")
        if sid is None:
            continue
        last_seen = rec.get("last_seen")
        if isinstance(last_seen, (int, float)):
            out[str(sid)] = float(last_seen)
    return out


def probe(cfg, *, active_within: float | None = None,
          timeout: float = PROBE_TIMEOUT_S,
          snapshot: dict | None = None,
          sessions: list[dict] | None = None,
          now: float | None = None) -> Activity:
    """Decide whether a restart would interrupt live work.

    ``snapshot`` / ``sessions`` / ``now`` are injection points for tests; in
    production all three are read here. Passing ``snapshot`` explicitly also
    lets a caller that already fetched one avoid a second RPC.
    """
    if active_within is None:
        active_within = active_within_default()
    if now is None:
        now = time.time()

    if snapshot is None:
        snapshot = _fetch_snapshot(cfg, timeout=timeout)
        if snapshot is None:
            return Activity(blocked=False, determinate=False,
                            reasons=["daemon did not answer its status RPC "
                                     f"within {timeout:g}s — treating it as "
                                     "serving nobody"])
    if sessions is None:
        from .. import session_registry

        try:
            sessions = session_registry.list_all()
        except Exception:  # noqa: BLE001 — a damaged ledger must not wedge restart
            sessions = []

    reasons: list[str] = []

    # --- 1. someone is mid-call right now -----------------------------------
    # Three hops, three id spaces (see `status.snapshot`). Any of them holding a
    # request means a client is blocked on an answer this restart would eat.
    relay = snapshot.get("relay") or {}
    relay_inflight = relay.get("inflight") or []
    if relay_inflight:
        reasons.append(
            f"{len(relay_inflight)} extension-relay call(s) in flight")

    executors = snapshot.get("executors") or []
    running = [e for e in executors if e.get("inflight")]
    if running:
        ids = ", ".join(str(e.get("session_id")) for e in running)
        reasons.append(f"executor(s) running code right now: {ids}")

    pending_total = 0
    for ctx in snapshot.get("contexts") or []:
        pending_total += len(ctx.get("pending_requests") or [])
    if pending_total:
        reasons.append(f"{pending_total} router request(s) awaiting an upstream "
                       "response")

    # --- 2. another downstream is holding a control connection --------------
    # Our own probe connection is labelled `cli-restart`; excluding it by label
    # is why `_fetch_snapshot` pins that label.
    others = []
    for ctx in snapshot.get("contexts") or []:
        for client in ctx.get("clients") or []:
            if client.get("label") == "cli-restart":
                continue
            others.append(client.get("label") or
                          f"client {client.get('client_id')}")
    if others:
        reasons.append(f"{len(others)} other client(s) connected: "
                       + ", ".join(sorted(set(others))))

    # --- 3. a live executor whose session got an instruction recently -------
    # The ledger's own idle clock, not executor liveness. See module docstring.
    if active_within > 0:
        last_seen = _last_seen_by_session(sessions)
        recent = []
        for row in executors:
            if row.get("alive") is False:
                continue
            sid = row.get("session_id")
            if sid is None:
                continue
            seen = last_seen.get(str(sid))
            if seen is None:
                continue
            idle = now - seen
            if idle < active_within:
                recent.append((str(sid), idle))
        if recent:
            recent.sort(key=lambda p: p[1])
            listed = ", ".join(f"{sid} (idle {idle:.0f}s)"
                               for sid, idle in recent)
            reasons.append(
                f"{len(recent)} session(s) with a live executor active in the "
                f"last {active_within:g}s: {listed}")

    return Activity(blocked=bool(reasons), determinate=True, reasons=reasons)
