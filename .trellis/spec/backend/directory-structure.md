# Directory Structure

> How backend code is organized in browserwright. Python 3.11+, single package
> under `src/browserwright/`. The daemon is async-first; the agent/skill layer
> is synchronous.

---

## Overview

Browserwright is a single Python package (`pyproject.toml` → `name = "browserwright"`,
`package-dir = {"" = "src"}`) that ships **two console scripts**:

- `browserwright` → `browserwright.cli:main` — the agent-facing CLI / REPL / session tools.
- `browserwright-daemon` → `browserwright.daemon.cli:main` — the long-running browser-resolving daemon (CDP proxy + backends).

The two halves are layered (see `TESTING.md`):

```text
AI agent / Claude Code
        ↓
skill/                     Agent-facing skill docs (SKILL.md, examples)
        ↓
src/browserwright/         Layer 2: agent API, sessions, memory, primitives  (SYNC)
        ↓
src/browserwright/daemon/  Layer 1: browser/CDP connection, proxy, backends   (ASYNC)
        ↓
Chrome / extension / RDP / cloud browser
```

Keep new code on the correct side of this boundary: agent-facing helpers stay
synchronous in the Layer-2 tree; anything touching the live CDP websocket /
relay belongs in `daemon/`. The daemon is **async-first** — its entrypoints and
the CDP routing hot path are `async def`, but the tree also has plenty of
ordinary synchronous `def` helpers (e.g. `listener.py:52` `make_context`,
`extension_upstream.py` id-builders). "async-first" ≠ "everything is async".

---

## Directory Layout

```text
src/browserwright/
├── api.py                 # canonical EXPORTS list — primitives re-exported into REPL/exec globals
├── cli.py                 # `browserwright` entrypoint (agent-facing CLI)
├── errors.py              # skill exception hierarchy (BrowserwrightError + subclasses)
├── session.py             # Session class: lazy CDPSession, current_target_id, site-memory cache, contextvars
├── session_registry.py    # file-locked JSON ledger (~/.browserwright/sessions/ledger.json)
├── session_ctx.py         # session lifecycle context
├── session_create.py      # `session new` CLI subcommand
├── mode_b_client.py       # skill ↔ daemon client (talks to the running daemon)
├── cdp.py                 # synchronous CDP wrapper over the `websockets` lib
├── task_runner.py         # task invocation
├── output_schema.py       # minimal JSON-Schema validator for task outputs
├── primitives/            # the agent-callable verbs
│   ├── page.py            #   navigation / tabs (new_tab, attach_active, wait_for_load)
│   ├── interact.py        #   click / type / scroll
│   ├── inspect.py         #   screenshot / describe / snapshot
│   ├── site.py            #   site memory (remember / recall)
│   ├── http.py            #   http_get and friends
│   └── discovery_api.py   #   tasks / skills discovery
├── memory/                # site / global / repl / decision memory (YAML + Markdown backing)
├── repl/
│   └── inline.py          # REPL execution harness
└── daemon/                # Layer 1 — the running daemon
    ├── cli.py             #   `browserwright-daemon` argparse dispatcher (serve/stop/stats/...)
    ├── errors.py          #   daemon exception hierarchy (DaemonError + exit-code mapping)
    ├── observability.py   #   Metrics counters + JSONLogFormatter
    ├── server/
    │   ├── listener.py    #     WebSocket listener + lifecycle (async entry: run_serve())
    │   ├── daemon.py      #     global Daemon + per-upstream UpstreamContext
    │   ├── proxy.py       #     Router: multi-client CDP id/sessionId/attacher translation
    │   ├── state.py       #     DaemonState dataclass (ClientState, SessionBinding, ...)
    │   ├── upstream.py    #     UpstreamConnection (CDP websocket handler)
    │   ├── extension_upstream.py  # extension-specific relay + routing
    │   └── relay.py       #     RelayServer (extension relay)
    └── backends/          #   resolver backends: extension, rdp, cloud, env
```

---

## Module Organization

- **Agent verbs** go in `primitives/` and are re-exported through `api.py`'s
  `EXPORTS` list so they land in the REPL/exec global namespace. A new
  agent-callable verb is added in the appropriate `primitives/*.py` module and
  registered in `api.py` — not exposed ad hoc.
- **Business logic / state** for a session lives on the `Session` class
  (`session.py`); durable cross-session records live in `session_registry.py`.
- **Daemon routing logic** is split by concern: `proxy.py` (Router) owns CDP
  frame translation, `state.py` owns the in-memory data model, `upstream.py` /
  `extension_upstream.py` / `relay.py` own transport. Don't fold transport into
  the Router or vice versa.
- **Backend resolution** (how a ws URL is found) is isolated in
  `daemon/backends/` so a new browser source is a new backend module, not edits
  scattered across the server.

---

## Naming Conventions

- **Modules**: `snake_case` (`proxy.py`, `upstream.py`, `session_registry.py`).
- **Classes**: `PascalCase` (`DaemonState`, `Router`, `UpstreamConnection`, `Session`).
- **Functions / variables**: `snake_case`; leading underscore for private
  (`_json_safe`, `_locked`, `_next_client_id`).
- **Constants**: `UPPER_CASE` (`PRE_OPEN_BUFFER_LIMIT`, `EVENT_RING_LIMIT`).
- New modules start with `from __future__ import annotations` (see
  `daemon/errors.py:9`, `daemon/observability.py:33`).

---

## Examples

- Well-organized daemon module: `src/browserwright/daemon/server/proxy.py` (Router) +
  `src/browserwright/daemon/server/state.py` (the data model it operates on).
- Agent verb done right: `src/browserwright/primitives/page.py` (e.g.
  `attach_active()` at `page.py:37`) exported via `api.py`.
- Layer boundary in action: `src/browserwright/session.py` (sync) holds a
  client to the async daemon via `mode_b_client.py`.
