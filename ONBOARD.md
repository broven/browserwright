# Onboarding — run browserwright locally

> For humans and code agents. Follow top to bottom to get a full
> edit → run → test loop. No secrets or owner hand-offs required: the dev/test
> path is fully mocked and touches no credentials.
>
> Deeper docs once you're running: **[ONBOARDING.md](ONBOARDING.md)** (architecture
> + what to touch / leave alone), **[TESTING.md](TESTING.md)** (full test-suite map),
> **[AGENTS.md](AGENTS.md)** (single source of truth for agents).

## 1. Install tools & dependencies

```bash
mise trust          # trust this repo's mise.toml ([tools] + [tasks])
mise install        # install pinned runtimes: python 3.11 + uv
mise run install    # uv sync --extra ux  (project + dev group + rich)
```

## 2. Run the dev loop

```bash
mise run test       # fast gate: daemon + skill unit tests + mocked skill evals
mise run lint       # ruff check + format check
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
| `mise run test` | Fast local gate: `test:daemon` + `test:skill` + `test:evals` |
| `mise run test:daemon` | Daemon / CDP / proxy / backend / relay unit tests |
| `mise run test:skill` | Agent-layer tests (CLI, sessions, primitives, memory, install) |
| `mise run test:evals` | Mocked skill command-choice evals (deterministic) |
| `mise run test:e2e` | Real-Chrome + unpacked-extension E2E (opt-in; see below) |
| `mise run lint` | `ruff check` + `ruff format --check` |
| `mise run build` | Build wheel/sdist (`uv build`) |
| `mise run dev` | (no-op) no dev server — see step 2 |
| `mise run dev-link` | Symlink this checkout into global PATH + skill dirs (dev only) |
| `mise run upgrade-global` | Sync global install to PyPI latest + matching extension artifact |
| `mise run version-check` | Verify package / skill / daemon / extension versions agree |

## Heavy / opt-in suites

These are **not** part of `mise run test` because they need extra setup:

```bash
# Real Chrome + unpacked extension E2E — needs Chrome for Testing:
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
mise run test:e2e

# Agent E2E (promptfoo / Claude SDK) — has its own deps, run from its dir:
cd tests/skill/agent-e2e && PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
  npx promptfoo eval -c promptfooconfig.yaml --no-cache
```

## Secrets / environment

The dev and test loops need **no secrets** — tests are fully mocked and launch
no real browser or network. There is intentionally no `fnox.toml` / `.env.example`.

A few **optional runtime** env vars only matter when you point browserwright at a
real cloud/remote browser backend (end-user supplied, not needed to develop or
test):

| Var | When |
|-----|------|
| `BROWSER_USE_API_KEY` | Using the Browser Use / cloud CDP backend |
| `BD_PORT` / `BD_BACKEND` | Targeting a specific daemon port / backend (e.g. `rdp` with an isolated profile) |

If a future task introduces a *required* secret, harness this repo with `fnox`
at that point (see the repo-harness skill); today none is needed.

## Project skill note

The root **`skill/`** directory is a **shipped product artifact** (packaged into
the PyPI distribution and linked into agent skill dirs by `mise run dev-link`),
not a "skill for developing this repo." This repo therefore does **not** use the
`.agents/skills/` ↔ `.claude/skills` symlink convention — there are no
vendor-neutral repo-dev skills to share. Edit `skill/` to change the product
skill; don't relocate it.
