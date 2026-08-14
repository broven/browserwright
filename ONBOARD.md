# Onboarding — run browserwright locally

> For humans and code agents. Follow top to bottom to get a full
> edit → run → test loop. No secrets or owner hand-offs required: the dev/test
> path is fully mocked and touches no credentials.
>
> Deeper docs once you're running: **[docs/architecture.md](docs/architecture.md)**
> (architecture + what to touch / leave alone), **[TESTING.md](TESTING.md)** (full
> test-suite map), **[AGENTS.md](AGENTS.md)** (single source of truth for agents).

## 1. Install tools & dependencies

```bash
mise trust          # trust this repo's mise.toml ([tools] + [tasks])
mise install        # install pinned runtimes: python 3.11 + uv
mise run install    # uv sync --extra ux  (project + dev group + rich)
```

## 2. Run the dev loop

```bash
mise run test       # fast gate: daemon + skill unit tests + mocked evals + pi-extension
mise run lint       # ruff check (pinned ruff + pinned rule set)
```

There is **no long-running dev server**. browserwright is a CLI + an on-demand
background daemon + an agent-facing skill. To exercise it after install:

```bash
uv run browserwright --help          # agent-facing CLI
uv run browserwright-daemon --help   # the browser-resolving daemon (CDP proxy + backends)
```

To make your in-progress checkout the machine-global install (symlinks into
`~/.local/bin` and the agent skill dirs):

```bash
mise run dev-link    # development only; a broken checkout can break global agents
```

## Task reference

| Command | What it does |
|---------|--------------|
| `mise run install` | Install all deps (`uv sync --extra ux`) — clone's first command |
| `mise run test` | Fast local gate: `test:daemon` + `test:skill` + `test:evals` + `test:pi` |
| `mise run test:daemon` | Daemon / CDP / proxy / backend / relay unit tests |
| `mise run test:skill` | Agent-layer tests (CLI, sessions, primitives, memory, install) |
| `mise run test:evals` | Mocked skill command-choice evals (deterministic) |
| `mise run test:pi` | `pi-extension/` chain + predicate unit tests (`node --test`, no network) |
| `mise run test:e2e` | Real-Chrome + unpacked-extension E2E (opt-in; see below) |
| `mise run teardown` | Reclaim **this worktree's** leaked e2e daemons / orphaned Chrome / test artifacts. Idempotent; never touches the global daemon or a sibling worktree. Worktree tooling runs it before deleting a worktree |
| `mise run lint` | `ruff check` — ruff version pinned in `[tools]`, rule set pinned in `[tool.ruff.lint]` |
| `mise run format` | Apply `ruff format` repo-wide. Opt-in: the repo has never been format-clean, so this rewrites ~135 files — do it as its own commit, never inside an unrelated change |
| `mise run build` | Build wheel/sdist (`uv build`) |
| `mise run dev` | (no-op) no dev server — see step 2 |
| `mise run dev-link` | Symlink this checkout into global PATH + skill dirs (dev only) |
| `mise run upgrade-global` | Sync global install to PyPI latest + matching extension artifact + pi npm extension ([RELEASING.md](RELEASING.md)); production users install the extension from the Chrome Web Store instead |
| `mise run version-check` | Verify package / skill / daemon / extension versions agree |

Cutting a release, fixing the PyPI publish, or updating the global install when
PyPI is behind? See **[RELEASING.md](RELEASING.md)**.

## Heavy / opt-in suites

These are **not** part of `mise run test` because they need extra setup:

```bash
# Real Chrome + unpacked extension E2E — needs Chrome for Testing:
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
mise run test:e2e
```

## Secrets / environment

The dev and test loops need **no secrets** — tests are fully mocked and launch
no real browser or network. There is intentionally no `.env` / `.env.example`,
and the secret inventory below is empty.

### Secret inventory

| Bitwarden entry (verbatim) | Field | Purpose |
|----------------------------|-------|---------|
| _(none)_ | — | No task in this repo needs a credential |

A few **optional runtime** env vars only matter when you point browserwright at a
real browser (end-user supplied, not needed to develop or test):

| Var | When |
|-----|------|
| `BD_PORT` / `BD_BACKEND` | Targeting a specific daemon port / backend (e.g. `rdp` with an isolated profile) |
| `BD_CDP_WS` / `BD_CDP_URL` | Binding the `env` backend to an externally-owned browser's CDP endpoint |

If a future task introduces a *required* secret, wire it through the
approved-secret broker rather than any plaintext file: the owner stores the real
value in Bitwarden, the task wraps its command as

```bash
approved-secret exec '<Bitwarden entry>' password=SOME_ENV -- <cmd>
```

(which prompts for approval on the owner's phone and injects the value into that
one child process only), and the entry gets a row in the table above. See the
global `use-approved-secrets` skill for the full command contract. Today none is
needed.

## Project skill note

The root **`skill/`** directory is a **shipped product artifact** (packaged into
the PyPI distribution and linked into agent skill dirs by `mise run dev-link`),
not a "skill for developing this repo." This repo therefore does **not** use the
`.agents/skills/` ↔ `.claude/skills` symlink convention — there are no
vendor-neutral repo-dev skills to share. Edit `skill/` to change the product
skill; don't relocate it.
