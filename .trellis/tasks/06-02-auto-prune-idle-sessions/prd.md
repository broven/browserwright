# Auto prune idle sessions

## Requirement

Implement automatic session cleanup for Browserwright's durable session ledger.
Session ids remain monotonic and are not reused. The idle clock is
`ledger.last_seen`, meaning the last time a user or agent issued a command for a
session.

## Decisions

- Keep `next_id` monotonic; cleanup removes ledger entries only.
- Prune on daemon startup and periodically while the daemon runs.
- Default prune threshold is 24 hours; allow disabling with config.
- Do not use executor process liveness as a session keepalive signal. A stuck
  executor can remain alive forever.
- Touch `ledger.last_seen` when a new executor-routed instruction arrives,
  before waiting on the executor, so a wedged executor cannot prevent the idle
  clock from advancing.
- When auto-pruning a session, reap its executor first, then apply existing
  session ownership teardown semantics.

## Files

- `src/browserwright/session_registry.py`
- `src/browserwright/session_create.py`
- `src/browserwright/_executor/client.py`
- `src/browserwright/daemon/config.py`
- `src/browserwright/daemon/server/listener.py`
- focused daemon/session tests
