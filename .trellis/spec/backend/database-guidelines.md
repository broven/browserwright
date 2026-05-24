# Persistence & State Guidelines

> Browserwright has **no relational database and no ORM**. This file documents
> the persistence layer that actually exists: a file-locked JSON ledger for
> durable session records, plus in-memory dataclass state inside the daemon.

---

## Overview

There are two state tiers. Keep them distinct:

1. **Durable session ledger** — survives across processes. One JSON file,
   guarded by `fcntl.flock()`. Source: `src/browserwright/session_registry.py`.
2. **Ephemeral daemon state** — lives only for the daemon process lifetime.
   Plain `@dataclass` graphs, mutated single-threaded under asyncio. Source:
   `src/browserwright/daemon/server/state.py` (`DaemonState`).

No SQLite, no SQLAlchemy, no migrations framework. Do **not** introduce a DB or
ORM for incidental state — extend the existing ledger or in-memory model.

---

## The Session Ledger (durable)

- **Location**: `~/.browserwright/sessions/ledger.json` (root overridable via
  the `BS_HOME` env var — `session_registry.py:14`).
- **Shape**: `{"next_id": N, "sessions": {id → {id, backend, workspace, owner, name, created_at, last_seen}}}`.
- **Concurrency**: every **read-modify-write** goes through a single
  `@contextmanager _locked()` that takes an `fcntl.flock(LOCK_EX)` on a sidecar
  `.lock` file (`session_registry.py:27`+) and writes `ledger.json` inside the
  lock. Plain reads (`get()`, `list_all()`) read `ledger.json` **without** the
  lock — that's intentional (a stale read is acceptable; a torn write is not).
  So: any code path that **mutates** the ledger must do so inside `_locked()`.
- **Immutability rule**: a session's `backend` is fixed at creation. Mutations
  that would change the backend of an existing session are rejected — this is a
  load-bearing invariant, not a nicety (sessions must not silently switch the
  browser they drive).

---

## In-Daemon State (ephemeral)

`DaemonState` is a single global dataclass instance holding the routing tables:

- `clients: dict[int, ClientState]` — one entry per connected downstream client.
- `upstream_to_locals: dict[upstream_sid, list[SessionBinding]]` — reverse
  lookup for fanning CDP events back out to the right clients.
- `attachers: dict[target_id, AttachOwnership]` — single-owner enforcement per target.
- `pending_requests: dict[upstream_id, PendingRequest]` — id translation for
  routing CDP responses back to the originating client.
- `targets: dict[target_id, dict]` — heuristic active-tab table.

These are mutated inline without locks **because the daemon is single-threaded
asyncio** — that assumption is the reason locks are absent, so don't add a
thread that mutates `DaemonState` without revisiting it.

---

## Naming Conventions

- **Session id**: short numeric string assigned from `next_id` — `"1"`, `"2"`, …
- **Local sessionId** (client-facing CDP): `c{client_id}-{random-hex}`.
- **Upstream sessionId** / **targetId**: opaque strings from Chrome — never
  parse or synthesize them; treat as cookies.
- State counters/fields use `snake_case`; dataclasses use `field(default_factory=...)`
  for mutable defaults.

---

## Common Mistakes

- **Mutating** `ledger.json` without the `_locked()` context manager — a torn
  read-modify-write corrupts the ledger. (Plain reads via `get()`/`list_all()`
  are unlocked by design; don't add a write to those paths.)
- Mutating a session's `backend` after creation — rejected by design; create a
  new session instead.
- Adding background threads that touch `DaemonState` — breaks the lock-free
  single-threaded-asyncio assumption.
- Reaching for SQLite/an ORM for new state — extend the ledger or `DaemonState`
  rather than introducing a database.
