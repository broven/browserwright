"""Per-session recovery state — the daemon's single answer to "which layer
is broken for this session?" (ADR-0013 rule 1).

Before this module eight components each judged "is the other side dead" on
their own budget, and nothing could tell an agent whether to wait for the
extension, rebind a tab, or cold-start an executor. The machine here is fed by
those components (relay hello / close, the executor registry, the recovery
sweep, the executor's per-call ``recovery_event``) and read by `status`,
`doctor` and the `recover` verb.

States (agent-visible strings, see CONTEXT.md "recovery state"):

  healthy                 — extension connected (or cdp upstream open), tab
                            bound, executor alive
  extension-disconnected  — no extension has said hello since the last one
                            went away; nothing extension-backed can be driven
  tab-gone                — the session's tab group / tab could not be found
                            or re-attached; the next call opens a fresh tab
  executor-unbound        — no resident executor (never started, reaped, or
                            deliberately recycled); the next call cold-starts
  executor-dead           — the resident executor exited on its own
  needs-human             — recovery was attempted and failed in a way a
                            retry will not fix; `reason` says what

The state lives on disk beside the ledger row (``recovery`` field) so a
replacement daemon rebuilds it on boot instead of assuming an empty world.
Persistence is best-effort and never raises into the daemon's hot path.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

HEALTHY = "healthy"
EXTENSION_DISCONNECTED = "extension-disconnected"
TAB_GONE = "tab-gone"
EXECUTOR_UNBOUND = "executor-unbound"
EXECUTOR_DEAD = "executor-dead"
NEEDS_HUMAN = "needs-human"

STATES = (HEALTHY, EXTENSION_DISCONNECTED, TAB_GONE, EXECUTOR_UNBOUND,
          EXECUTOR_DEAD, NEEDS_HUMAN)

#: Inputs. Each maps to one transition below; unknown inputs are ignored and
#: logged, never raised — a reporter must not be able to crash the daemon.
EXTENSION_LOST = "extension_lost"
EXTENSION_HELLO = "extension_hello"
TAB_RECOVERED = "tab_recovered"
TAB_RECOVER_FAILED = "tab_recover_failed"
EXECUTOR_READY = "executor_ready"
EXECUTOR_EXITED = "executor_exited"
EXECUTOR_REAPED = "executor_reaped"
RECOVERY_FAILED = "recovery_failed"
SESSION_ENDED = "session_ended"

#: The executor's ``ExecuteResponse.recovery_event`` kinds, as inputs. The
#: executor reports what it saw; this table is where that becomes a recovery
#: input — an in-place rebind is a recovered tab, a failed one a lost tab.
EXECUTOR_RECOVERY_INPUTS = {
    "bound": TAB_RECOVERED,
    "rebound": TAB_RECOVERED,
    "target-gone": TAB_RECOVER_FAILED,
}


class RecoveryStateMachine:
    """``{session_id: {"state", "since", "reason", "generation"}}``.

    ``persist(session_id, record | None)`` is called after every change (None
    on removal); the daemon wires it to the ledger. ``now`` is injectable for
    tests.
    """

    def __init__(self, *, persist: Callable[[str, dict | None], None] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self._persist = persist
        self._now = now
        self._states: dict[str, dict] = {}

    # ---- reads -----------------------------------------------------------

    def get(self, session_id: str) -> dict | None:
        rec = self._states.get(str(session_id))
        return dict(rec) if rec is not None else None

    def state_of(self, session_id: str) -> str | None:
        rec = self._states.get(str(session_id))
        return rec["state"] if rec is not None else None

    def all(self) -> dict[str, dict]:
        return {sid: dict(rec) for sid, rec in self._states.items()}

    # ---- boot ------------------------------------------------------------

    def load(self, rows: list[dict], *, extension_connected: bool,
             executor_alive: Callable[[str], bool]) -> None:
        """Rebuild from ledger rows at daemon start (ADR-0013: the state is on
        disk, the daemon process is replaceable). A persisted ``recovery``
        field is the starting point; then what THIS daemon can observe right
        now overrides it: no extension yet → extension-disconnected for
        extension sessions; an executor we adopted → not executor-dead."""
        for row in rows:
            sid = str(row.get("id") or "")
            if not sid:
                continue
            saved = row.get("recovery") if isinstance(row.get("recovery"), dict) else {}
            saved_state = (saved.get("state") if saved.get("state") in STATES
                           else EXECUTOR_UNBOUND)
            saved_reason = str(saved.get("reason") or "")
            state, reason = saved_state, saved_reason
            if row.get("backend") == "extension" and not extension_connected:
                state, reason = EXTENSION_DISCONNECTED, "no extension connected to this daemon yet"
            elif executor_alive(sid):
                if row.get("backend") == "extension" and state in (
                    EXECUTOR_DEAD, EXECUTOR_UNBOUND
                ):
                    state, reason = HEALTHY, "executor adopted from the previous daemon"
                elif row.get("backend") == "cdp":
                    # A cdp session's browser is daemon-owned (or must be
                    # re-resolved by the replacement).  A live executor alone
                    # cannot prove its upstream or tab survived.
                    state, reason = TAB_GONE, "cdp browser and tab not re-proven after daemon start"
            elif state == HEALTHY:
                state, reason = EXECUTOR_UNBOUND, "no resident executor after daemon start"
            changed_from_disk = state != saved_state or reason != saved_reason
            record = {
                "state": state,
                "since": (self._now() if changed_from_disk else
                          float(saved.get("since") or self._now())),
                "reason": reason,
                "generation": saved.get("generation"),
            }
            self._states[sid] = record
            if changed_from_disk or not saved:
                self._emit(sid, record)

    def ensure(self, row: dict, *, extension_connected: bool,
               executor_alive: Callable[[str], bool]) -> None:
        """Initialize a ledger row created after daemon boot, once."""
        sid = str(row.get("id") or "")
        if sid and sid not in self._states:
            self.load([row], extension_connected=extension_connected,
                      executor_alive=executor_alive)

    # ---- writes ----------------------------------------------------------

    def note(self, session_id: str, event: str, *, reason: str = "",
             generation: int | None = None, executor_alive: bool | None = None) -> str | None:
        """Apply one input; return the resulting state (None when the session
        is unknown to the machine and the event does not introduce it, or when
        the event carries a stale ``generation``)."""
        sid = str(session_id)
        rec = self._states.get(sid)
        if event == SESSION_ENDED:
            if rec is not None:
                self._states.pop(sid, None)
                self._emit(sid, None)
            return None
        if rec is None:
            rec = {"state": EXECUTOR_UNBOUND, "since": self._now(), "reason": "",
                   "generation": None}
            self._states[sid] = rec
        if (generation is not None and rec.get("generation") is not None
                and generation < rec["generation"]):
            logger.debug("recovery: ignoring stale %s for session %s (gen %s < %s)",
                         event, sid, generation, rec["generation"])
            return None
        if generation is not None:
            rec["generation"] = generation
        new = self._transition(rec["state"], event, executor_alive)
        if new is None:
            logger.debug("recovery: no transition for %s in %s (session %s)",
                         event, rec["state"], sid)
            return rec["state"]
        if new != rec["state"] or reason != rec.get("reason"):
            rec["state"] = new
            rec["since"] = self._now()
            rec["reason"] = reason
            self._emit(sid, rec)
        return new

    @staticmethod
    def _transition(state: str, event: str, executor_alive: bool | None) -> str | None:
        if event == EXTENSION_LOST:
            return EXTENSION_DISCONNECTED
        if event == EXTENSION_HELLO:
            # Connected again, but nothing is re-attached until the sweep
            # reports; until then the honest state is "tab unknown".
            return TAB_GONE if state == EXTENSION_DISCONNECTED else state
        if event == TAB_RECOVERED:
            if state == EXTENSION_DISCONNECTED:
                return None  # a sweep result from before the loss; ignore
            return HEALTHY if executor_alive else EXECUTOR_UNBOUND
        if event == TAB_RECOVER_FAILED:
            return TAB_GONE if state != EXTENSION_DISCONNECTED else None
        if event == EXECUTOR_READY:
            if state in (EXECUTOR_DEAD, EXECUTOR_UNBOUND):
                return HEALTHY
            return state
        if event == EXECUTOR_EXITED:
            return (state if state in (EXTENSION_DISCONNECTED, TAB_GONE,
                                       NEEDS_HUMAN) else EXECUTOR_DEAD)
        if event == EXECUTOR_REAPED:
            return (state if state in (EXTENSION_DISCONNECTED, TAB_GONE,
                                       NEEDS_HUMAN) else EXECUTOR_UNBOUND)
        if event == RECOVERY_FAILED:
            return NEEDS_HUMAN
        return None

    def _emit(self, sid: str, rec: dict | None) -> None:
        if self._persist is None:
            return
        try:
            self._persist(sid, dict(rec) if rec is not None else None)
        except Exception as e:  # noqa: BLE001 - persistence must never wedge the daemon
            logger.warning("recovery: could not persist state for %s: %r", sid, e)


def ledger_persist(session_id: str, rec: dict | None) -> None:
    """The production ``persist``: write the record into the ledger row."""
    from ... import session_registry as reg
    if rec is None:
        return
    reg.update(session_id, recovery={
        "state": rec["state"], "since": rec["since"], "reason": rec.get("reason", "")})
