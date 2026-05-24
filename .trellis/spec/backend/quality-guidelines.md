# Quality Guidelines

> What "good" looks like in browserwright: high type-hint density, dataclasses
> over pydantic, async daemon / sync skill layer, session-scoped operations, and
> a layered pytest suite gated by `uv run pytest`. Dependency management is `uv`.

---

## Overview

There is **no ruff / mypy / black config** in `pyproject.toml` and no lint task
in `mise.toml` — formatting and typing are maintained by convention, not CI
enforcement. So matching the surrounding style is the quality bar; the tests and
`version-check` are the automated gates.

Tooling facts:
- **Dependencies**: `uv` (`uv sync --extra ux`, `uv.lock` committed). Use `uv`,
  not bare pip/venv. Dev deps in the `dev` group: `coverage`, `pytest`,
  `pytest-asyncio`.
- **Dev tasks**: `mise` (`mise reinstall` / `dev-link`, `version-check`,
  `restart-daemon`).
- **Python**: `requires-python = ">=3.11"`.

---

## Required Patterns

- **Type hints on signatures.** ~90% of functions are fully annotated. Use
  Python 3.10+ unions (`str | None`, not `Optional[str]`). Router callbacks are
  typed `Callable[[str], Awaitable[None]]`.
- **`from __future__ import annotations`** at the top of new modules.
- **Dataclasses for state**, with `field(default_factory=...)` for mutable
  defaults. No pydantic anywhere in the codebase.
- **Respect the async/sync split**: the daemon (`daemon/server/*`) is
  **async-first** — entrypoints and the CDP routing hot path are `async def`
  (sync `def` helpers coexist, e.g. `listener.py:52`); the skill layer
  (`primitives/`, `session.py`) is synchronous and blocks on
  `session.cdp.send()`. Don't introduce `asyncio` into the skill layer, and
  don't put blocking sync calls on the daemon's async hot path.
- **Session-scoped operations.** Browser operations are bound to a session
  (`SessionBinding`); `NoSession` is raised rather than silently sharing a
  browser. New operations must thread through the session, not reach a global
  browser. (Recent commit `cf4feab "Enforce session-scoped browser operations"`.)
- **Actionable errors**: see `error-handling.md` — new `BrowserwrightError`
  subclasses need a `default_fix`.

---

## Forbidden Patterns

- `print()` for diagnostics in library/daemon code — use the logger.
- pydantic models for new state — use `@dataclass`.
- Adding a relational DB / ORM — extend the JSON ledger or `DaemonState`
  (see `database-guidelines.md`).
- Parsing or synthesizing Chrome `targetId` / upstream `sessionId` strings —
  treat them as opaque.
- Background threads mutating `DaemonState` — breaks the lock-free
  single-threaded-asyncio assumption.

---

## Testing Requirements

Framework: **pytest + pytest-asyncio** with `asyncio_mode = "auto"`
(`pyproject.toml:41`), `pythonpath = ["src"]`, `testpaths = ["tests"]`.

Suite layout (`TESTING.md` is the map):

| Path | Scope |
|------|-------|
| `tests/daemon/` | daemon, CDP proxy, backends, IPC, relay, Chrome launch, observability |
| `tests/skill/` | agent CLI, sessions, primitives, memory, tasks, install flows |
| `tests/daemon/e2e/` | real Chrome + unpacked extension + daemon (`real_chrome` marker, auto-applied in collection) |
| `tests/skill/agent-e2e/` | promptfoo / Claude SDK agent workflows (own deps) |
| `evals/` | text-level command-choice behavior for skill docs |

- **E2E tests carry the `real_chrome` marker and are skipped by default.** The
  marker is applied automatically during collection
  (`pytest_collection_modifyitems` in `tests/daemon/e2e/conftest.py:62`/`:84`),
  not decorated on each test. Run them explicitly via `tests/daemon/e2e/run.sh -v`,
  which uses an isolated
  Chrome-for-Testing profile, a patched relay URL, and test ports
  (`BD_EXTENSION_PORT=29989`, `BD_RDP_PORT=29990`) so they never touch the daily
  Chrome profile or production daemon.
- **Test isolation**: fixtures scrub `BD_*` / `BS_*` / `BU_*` env vars
  (`scrubbed_env()` in `tests/daemon/conftest.py`); use them rather than
  relying on ambient env.
- **No external mocking library** — tests use a hand-rolled `Capture` spy that
  accumulates sent frames, plus a `setup_router()` helper factory.
- **Naming**: descriptive multi-aspect names
  (`test_attach_response_race_variants_and_validation`); coverage-focused
  modules use a `..._dense.py` suffix.

### The fast local gate (run before handing off ordinary changes)

```bash
uv run pytest tests/daemon tests/skill --ignore=tests/skill/agent-e2e -q
python3 evals/run.py --mock
```

For daemon/proxy/extension changes also run `tests/daemon/e2e/run.sh -v`.

Coverage (`pyproject.toml:49`): source `src/browserwright`, omitting
`__main__.py` and `site_skills_starter/*`; report uses `skip_covered`.

---

## Code Review Checklist

- [ ] On the correct layer (sync skill vs async daemon) and async/sync respected.
- [ ] Full type hints with `X | None` unions; `from __future__ import annotations`.
- [ ] State is a dataclass; no pydantic, no new DB/ORM.
- [ ] Browser operations are session-scoped (no global-browser shortcuts).
- [ ] New errors are typed, carry a `default_fix`, and (if they add fields) are
      in the `serialize()` allow-list.
- [ ] No `print()` diagnostics; logging via `getLogger(__name__)`, secrets not logged.
- [ ] Fast gate passes (`uv run pytest … && evals/run.py --mock`); E2E run if
      daemon/proxy/extension touched.
- [ ] Versions agree if anything version-bearing changed (`mise run version-check`).
