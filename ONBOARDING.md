# browserwright — onboarding for new contributors

Stuck inside this repo for the first time? Read this once, top to bottom.
It's the shortest path to "I know what to touch and what to leave alone."

## TL;DR

- **`design.md`** is the spec authority. Disagreements between code and
  design.md → footnote design.md, don't drift the code silently.
- **Talk to the daemon, not the browser.** Skill is Layer 2; raw CDP
  calls live in `browserwright-daemon`. If you find yourself opening a ws to
  Chrome in this repo, you're either writing a test (mock it) or making
  a mistake (don't).
- **Test policy is non-negotiable.** Chrome 144+ accumulates "Allow"
  popups until it freezes. Two related rules:
  1. *Chrome*: iterative tests go through `browserwright-daemon launch-chrome
     --port <X> --profile /tmp/...`. Never short-connect to the user's
     daily Chrome.
  2. *Filesystem*: any code path that writes outside the temp dir
     (e.g. `~/.config/browserwright-daemon/config.toml`) must accept a
     `*_PATH` env override so tests can redirect to `tmp_path`. The
     wizard's `BS_DAEMON_CONFIG_PATH` is the canonical example.

## Pick a backend — decision tree

Most new contributors come in via "I'm setting up to test something on
this machine." Here's how to choose:

```
Are you running scripted / iterative tests?
├── yes → use the isolated profile (wizard option 1 — recommended).
│         `browserwright-daemon launch-chrome --port 9333 --profile /tmp/bs-dev`
│         then `BD_PORT=9333 BD_BACKEND=rdp browserwright ...`
└── no → are you driving the user's daily Chrome?
        ├── yes → option 3 (extension backend) — load the unpacked relay
        │         extension once; subsequent calls reuse the same ws,
        │         zero popups.
        └── do you have a special browser source?
            ├── fingerprint browser (AdsPower / MultiLogin / GoLogin /
            │   比特浏览器) → option 2, supply the port your tool exposes
            └── cloud / remote Chrome (Browser Use, Browserless,
                Hyperbrowser, generic CDP-compatible) → option 4
```

> The legacy `autoconnect` backend (Chrome `--remote-debugging-port=9222`
> with the per-ws Allow popup) was removed in 2026-05. `extension` is the
> only path that drives the user's daily Chrome.

The install wizard codifies this same decision tree —
`browserwright install` and answer the prompts.

## Repo layout (start here, then expand)

```
src/browserwright/
├── cli.py                ← argv dispatch — start here when wiring a new subcommand
├── api.py                ← `from browserwright import *` surface
├── install.py            ← the wizard (~550 LOC; doctor-driven option detection)
├── daemon_client.py      ← Mode A subprocess client
├── mode_b_client.py      ← Mode B socket client + auto_client() factory
├── repl/
│   ├── inline.py         ← P0 #75 popup-cost abort gate; reads doctor JSON
│   └── server.py         ← long-lived REPL daemon
├── primitives/           ← agent-facing API surface (page / interact / inspect / site)
├── memory/
│   ├── global_mem.py     ← `~/.browserwright/global.md` + dotted-key set_preference
│   └── site_mem.py       ← per-host memory; eTLD+1 stems (with legacy fallback)
├── solidify/             ← propose_solidify + scaffold + extract
├── multitask.py          ← run_tasks_concurrent fan-out
└── site_skills_starter/  ← bundled site dirs (names = eTLD+1 stems)

tests/
├── test_install_extension_v04.py        v0.4 wizard wire (12 tests)
├── test_install_cloud_v05.py            v0.5 cloud wizard + config writer (17 tests)
├── test_e2e_bugs_v031.py                4 AI-E2E bug regressions (22 tests)
├── test_solidify.py / test_memory.py / test_multitask.py / ...
└── conftest.py                          ← shared fixtures (tmp_bs_home, fresh_modules)
```

When in doubt, `uv run pytest -q` runs the full suite (140 tests as of
v0.5 first wave). Tests are entirely mocked — no real daemon, no real
Chrome.

## Operating principles (skim, then refer back)

### 1. Doctor as contract (spec H3)

`browserwright-daemon doctor --json` is contract-bound to **zero ws side
effects**. Every wizard option-availability helper
(`_extension_backend_available()`, `_cloud_backend_available()`, future
v0.6+) must consume the doctor JSON dict only. Don't open a CDP ws,
don't subprocess a backend-specific `--probe`, don't curl a cloud
provider. If you need richer signal than doctor provides, extend the
daemon's doctor schema first.

`test_install_cloud_v05.py::test_doctor_probe_is_the_only_detection_channel`
enforces this — it patches `socket.socket` to a tripwire. Any new helper
that touches the network outside doctor will trip this test immediately.

### 2. Spec authority + footnote-as-you-go

When a behaviour evolves, footnote `design.md` instead of letting the
code drift silently. Example: spec §A.1 originally said "auto-suggest
`repl start`"; P0 #75 strengthened that to "abort with exit 2". The
codebase tracks the new behaviour, and the spec entry got expanded so
readers don't have to guess which version of the contract holds.

### 3. Test filesystem isolation

The wizard writes outside the test tree (`~/.config/browserwright-daemon/...`
for the daemon TOML). Tests must override these paths via env vars
*before* the wizard runs. Established pattern in
`tests/test_install_cloud_v05.py::_drive_wizard`:

```python
monkeypatch.setenv("BS_DAEMON_CONFIG_PATH",
                   str(tmp_path / "fake-daemon-config.toml"))
```

Every env override the production code accepts is a deliberate seam for
this purpose. Add a `*_PATH` env override whenever you add a new writer
that targets the user's home — it's not optional.

(This rule is the filesystem analogue of `chrome-popup-test-policy` —
tests must not pollute user state, period.)

### 4. Forward-compat wizard options

A new daemon backend that lists itself in `doctor --json` is
auto-surfaceable by the wizard if you pattern after
`_extension_backend_available()` / `_cloud_backend_available()`:

1. Add a new entry to `_OPTIONS` with a `(coming vX.Y)` suffix.
2. Add `_<name>_backend_available()` that returns
   `_backend_available_from(_wizard_doctor_backends(), "<name>")`.
3. In `run()`, branch the menu label rewrite + the disabled-choice exit
   on `<name>_live`.
4. Implement per-option prompt collection + memory schema extension.

No Skill release is needed to surface the option once daemon reports it
as `available=true`. This is the v0.4 / v0.5 design that made the
two-wave delivery model work, and it's the model future backends should
follow.

## Common failure modes (and the fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| `inline heredoc fails with `Target.createTarget requires sessionId in extension backend`` | `new_tab()` doesn't support the extension backend | use `open_background(url)` or `attach_active()` to bind to an existing tab; or run against `BD_BACKEND=rdp` with an isolated profile |
| `memory show --site=news.ycombinator.com` returns empty but bundled dir exists | pre-v0.3.1 user-written `~/.browserwright/site-skills/news/` shadowing the eTLD+1 stem | The `_read_candidates()` fallback should pick it up automatically; if not, run `browserwright index rebuild` |
| `solidify(...)` raises `AttributeError: 'str' has no attribute 'items'` | args-schema is flat (`{"q": "str"}`) | Use the dict shape: `{"q": {"type": "str", "required": True}}` — `_validate_args_schema` rejects the flat form with a clear `ValueError` in v0.3.1+ |
| `propose_solidify()` returns a dict with `ready: False` but agent expected None | v0.3.1 changed the contract — `propose_solidify` always returns a dict now, check `out["ready"]` and surface `out["reasons"]` / `out["warnings"]` to the user | this is correct behaviour, not a bug |
| Tests pollute `~/.config/browserwright-daemon/` | new code path writes outside tmp_path without an env override | follow the `BS_DAEMON_CONFIG_PATH` pattern; add a `*_PATH` env override to the production code |
| Wizard option 4 / 5 still says "coming vX.Y" after the daemon was upgraded | stale doctor probe cache, or daemon binary not on `PATH` | rerun the wizard; `browserwright-daemon doctor --json` must report `available=true` for the option |

## "I'm new — what should I read first?"

In this order:

1. This file (you're here).
2. `design.md` §0 (user stories) + §A.1 (REPL invocation forms).
3. `HANDOFF-v0.5.md` for the version-by-version delivery history.
4. One existing test file matching the area you're touching — e.g.
   `test_install_cloud_v05.py` if you're adding a wizard option.
5. The CHANGELOG-style sections in `README.md` (`v0.4`, `v0.5`) for the
   user-visible shape of recent work.

Then open an issue or grep `TODO(agent)` for tasks looking for hands.