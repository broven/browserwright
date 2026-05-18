# browser stack — v0.5 handoff

For the next agent picking up the cloud-backend work. Read top-to-bottom
once; the repo navigation table is at the bottom.

## Versions shipped

| Component | Version | Tests | Live-verified |
|---|---|---|---|
| `browser-daemon` | **0.5.3** | 283 ✓ | extension backend end-to-end + cloud backend with `AuthProvider` abstraction (bearer / basic / mtls) + observability/stats CLI; 12/12 daemon-side REVIEW.md findings closed |
| `browser-skill` | **0.5.1** | 229 ✓ | inline-abort, install wizard (incl. v0.5 cloud-option **schema-aligned** with daemon 0.5.0 — `[backends.cloud]` + per-kind `[backends.cloud.auth.<kind>]` subtables, `provider_hint`, header-mode basic auth via env-var names, mtls `cert_file`/`key_file`), all four AI-E2E bug fixes (all via mocks per [chrome-popup-test-policy] **plus 3 live `browser-daemon doctor --json` tests** that gracefully skip when binary not found) |

Both packages target Python ≥ 3.11. Use `uv run pytest -q` in each repo.

### Milestone — 4/4 user stories passed in live AI run

`ai-e2e-tests` ran a real Claude agent (OAuth fallback) against all four
spec-defined user stories on the v0.4 stack and **all four passed**:

| US | Story | Verdict |
|---|---|---|
| US1 | Active-tab follow + one-shot ad-hoc | ✓ |
| US2 | New tab + in-flight `remember()` write | ✓ |
| US3 | `propose_solidify()` → review → `solidify()` commit | ✓ |
| US4 | `remember_preference()` → install wizard persists | ✓ |

That run also surfaced the 7 bugs listed below; all fixed in
`browser-skill 0.3.1` (Skill 1-4) and `browser-daemon 0.4.0` (Daemon 1-3).
The framework is now harness-validated as an AI-agent platform — see
`ai-e2e-tests/AI-E2E-REPORT.md` (canonical, hand-curated) and
`ai-e2e-tests/AI-E2E-REPORT.auto.md` (auto-generated from the harness
run) for the full transcript and bug provenance.

## What each version landed

### v0.1 — Mode A baseline

- Skill primitives — 36 `EXPORTS` names matching the browser-harness
  surface 1:1 (v0.5.1 F-4 catch-up shipped 13 primitives that were
  documented but missing in v0.5.0: `type_text` / `press_key` / `fill_input`
  / `scroll` / `dispatch_key` / `upload_file` / `wait_for_element` /
  `wait_for_network_idle` / `drain_events` / `ensure_real_tab` /
  `iframe_target` / `http_get` plus 3 Layer-3 re-exports
  `list_site_skills` / `load_site_skill` / `run_task`). Two primitives
  remain deferred to v0.6: `handle_dialog`, `try_recover_from_drift` —
  see design.md §A.2 footnote.
- 3 REPL shapes: inline heredoc, long-lived `repl start` daemon, one-off
  `task` invocation — all share one namespace.
- Mode A daemon client (`subprocess "browser-daemon url"` → CDP ws URL).
- Bundled site-skills for 5 sites: github, google, ycombinator, producthunt,
  wikipedia.
- Three-tier memory (global / site / repl), append-only with redaction.

### v0.2 — Mode B + project-local site-skills

- Long-lived unix-socket / TCP+token daemon endpoint (`browser-daemon
  serve`). `auto_client()` factory picks Mode B → Mode A fallback.
- Project-local `./site-skills/` overrides bundled.
- `selftest` cache, OUTPUT_SCHEMA, `memory forget/replace`, `solidify
  like=<site>/<task>` analogy seeding.

### v0.3 — Layer 3 fan-out + popup defense

- `run_tasks_concurrent()` fan-out via `ThreadPoolExecutor`; daemon v0.3
  added multi-client mux.
- **P0 #75** — inline heredoc *aborts* (exit 2) when the daemon would
  pick the autoconnect backend and no shared ws is available. Bypasses:
  Mode B alive, `BS_CDP_WS` set, `BS_FORCE_AUTOCONNECT_INLINE=1`.
- Daemon-side popup rate-limit (#74) + pre-open buffer fix (#76); Skill
  side deprecated the `warm_upstream` workaround.

### v0.4 — Browser-extension relay

- Daemon `chrome-extension/` Manifest V3 extension + `--backend extension`
  relay (Mode B only; Mode A raises `DaemonUnavailable`).
- Skill `install` wizard option 4 wires through `_extension_backend_available()`
  doctor probe — automatically surfaces as live when daemon's `doctor
  --json` lists `extension` with `available=true`.
- `chrome_extension_path()` three-tier resolution helper (env override →
  `browser-daemon extension-path --json` → walk from binary).

### v0.3.1 — AI E2E bug fix patch

All four skill bugs from the agent-sdk-tester live run, mock-tested only:

| Bug | Surface | Fix |
|---|---|---|
| 1 | `host_stem` too aggressive | eTLD+1 algorithm + 26 multi-label TLDs + `_legacy_host_stem` read fallback + bundled HN dir renamed `news.ycombinator.com` → `ycombinator.com` + `find_task_path` normalisation |
| 2 | `solidify()` malformed schema → `AttributeError` | `_validate_args_schema()` at `commit()` entry, raises `ValueError` with correct shape example |
| 3 | `propose_solidify` returned `None` below threshold | Always returns dict with `ready`/`readiness_score`/`threshold`/`reasons`/`warnings`/`name_hint` keys; `ready=True` adds scaffold seed |
| 4 | `remember_preference("a.b.c", ...)` nested-write undocumented | Docstring + README `Memory: dotted-key preferences` section, regression test guards docstring content |

## v0.5 scope — cloud backend

Per `browser-daemon/design-v2.md` §7 v0.5 + §8.1.1:

**Daemon side:**
- New `cloud` backend (Browser Use, Browserless, Hyperbrowser, etc.) —
  separate from `env` because `env` is constrained to URL-embedded auth.
  See §8.1.1 auth matrix.
- Built-in auth provider abstraction: per-backend `Authorization: Bearer`,
  `X-API-Key`, OAuth refresh, mTLS client cert. Daemon manages the
  credential lifecycle, not Skill.
- Observability/metrics/structured logging hooks (also v0.5 scope).
- Doctor report quality polish.

**Skill side (~150 LOC + 6-10 tests; per team-lead brief):**

**First wave — already in `browser-skill 0.3.1`** (doctor-driven, mock-tested):

- `install.py` wizard option 5 "Cloud/Remote browser (Browser Use /
  Browserless / Hyperbrowser)", gated by `_cloud_backend_available()`
  (mirrors v0.4 `_extension_backend_available()` — single shared
  `_wizard_doctor_backends()` probe at wizard entry, no ws side effects).
- `_cloud_backend_entry()` returns the full doctor entry; the wizard
  reads its `extras: {provider, endpoint, auth_kind, configured, ...}`
  block and uses those values as **prompt defaults**, so re-running
  `browser-skill install` against a daemon that already has cloud
  configured is a press-Enter-through experience.
- Provider prompt (browser-use / browserless / hyperbrowser / generic) +
  auth_kind prompt (bearer / basic / mtls; **oauth2 rejected with
  "coming v0.6" hint** so users who typed it on purpose learn when to
  expect it). Per-kind credential-*reference* collection via
  `_collect_cloud_fields()`.
- Memory writes under the existing `daemon:` frontmatter block —
  `cloud_provider`, `cloud_auth_kind`, plus auth-kind-specific keys
  (`cloud_token_env` | `cloud_endpoint` | `cloud_auth_cert_path` +
  `cloud_auth_key_path`).
- **Daemon `config.toml` writer**: `_write_daemon_cloud_config()` emits
  a `[cloud]` section to `~/.config/browser-daemon/config.toml` (XDG-
  aware; `$BS_DAEMON_CONFIG_PATH` env override for tests). Wholesale
  replacement of the existing `[cloud]` block; **other sections
  preserved untouched** (server, logging, etc.). Hand-rolled TOML emit
  — no `tomli_w` dependency. Failures are non-fatal (memory still
  persists; re-run can retry).
- Auto-flips from "(coming v0.5 — not yet available)" to live label as
  soon as daemon-impl-2's cloud backend lists itself in `doctor --json`
  with `available=true`. Pre-defined doctor contract documented inline
  in `_cloud_backend_entry()` docstring.
- Test file `tests/test_install_cloud_v05.py` (17 tests, ~365 LOC):
  doctor-available detection ×3, menu live/coming label ×2, choice-5-
  when-disabled blocks ×1, bearer / basic / mtls happy paths ×3,
  validation rejects ×3, **oauth2-rejected-as-coming-v0.6 ×1**,
  **extras-prefill from doctor ×1**, **config.toml writer in
  isolation ×2** (minimal block + section preservation),
  detection-contract regression guard ×1.

**Second wave — closed**. After daemon-impl-2 shipped the real cloud
backend in `browser-daemon 0.5.0`, three things landed in
`browser-skill 0.3.1`:

1. **Live verification (3 tests)** — `tests/test_install_cloud_v05.py`
   subprocess-invokes the real `browser-daemon doctor --json` binary
   (auto-discovered via `shutil.which` or sibling `.venv/bin/`; skips
   gracefully if not found). One baseline, one env-driven flip
   (`BD_CLOUD_ENDPOINT`), and one **end-to-end certification** that
   feeds the wizard's emitted TOML config back into a real daemon and
   asserts the cloud entry flips to `available=true` with
   `provider_hint` surfaced.
2. **Schema realignment** — running Item 1 surfaced a 4-point gap
   between my forward-prep TOML emit and the real daemon 0.5.0 schema
   (provider→provider_hint, flat fields → per-kind
   `[backends.cloud.auth.<kind>]` subtables, basic auth env-var
   references instead of URL-embedded creds, mtls `cert_file`/`key_file`
   instead of `cert_path`/`key_path`). Fixed fully in v0.3.1: 23 cloud
   tests pass (was 17), and the end-to-end live test certifies the
   wizard's output is daemon-parseable.
3. **Daemon README cross-ref** ✅ — `browser-skill/README.md` v0.5 section
   ends with a Markdown link to
   [`../browser-daemon/README.md#v05-cloud-backend`](../browser-daemon/README.md#v05-cloud-backend),
   pairing with the daemon's own `## v0.5 cloud backend` heading. Symmetric
   naming with `## v0.5 observability` (`#v05-observability`) on the
   daemon side.

Everything else is done: README v0.5 section (4-backend comparison + 3
auth-kind walkthrough + persistence schema), ONBOARDING.md (decision
tree + test policies + failure-mode table), TOML writer with
single-source-of-truth section-name constants
(`_CLOUD_TOML_TOP_SECTION` + `_CLOUD_TOML_AUTH_SECTION_FMT`).
- Provider prompt: `browser-use` / `browserless` / `hyperbrowser` /
  `generic`.
- Auth-kind prompt + per-kind field collection:

  | auth_kind | wizard collects |
  |---|---|
  | `bearer` | name of env var holding the token (e.g. `BROWSER_USE_KEY`) |
  | `basic`  | endpoint URL with `user:pass@` embedded |
  | `mtls`   | absolute paths to cert + key files |

  **Never store the secret itself in Skill memory** — only the env-var
  name / file path / URL stays in `global.md`. Actual credentials live
  in env, on disk, or (longer-term) the daemon's keychain integration.
- Memory schema extension on `global.md` frontmatter `daemon:` block:
  ```yaml
  daemon:
    preferred_backend: cloud
    cloud_provider: browser-use         # one of the 4 above
    cloud_endpoint: wss://api.example/...
    cloud_auth_kind: bearer             # bearer / basic / mtls
    cloud_auth_envvar: BROWSER_USE_KEY  # only when auth_kind=bearer
    cloud_auth_cert_path: /path/foo.crt # only when auth_kind=mtls
    cloud_auth_key_path:  /path/foo.key # only when auth_kind=mtls
  ```
  Update README's `Memory: dotted-key preferences` table with one cloud
  example row.
- Write the corresponding daemon `config.toml` block (whatever shape
  daemon-impl-2 lands — coordinate when their stats CLI / config writer
  surfaces).
- Tests: cloud doctor=available + bearer flow / basic flow / mtls flow
  (3 wizard end-to-end), cloud doctor=unavailable → option 5 disabled
  (1 negative). Plus 2-3 unit tests on the new helper / memory schema
  validators. All mocked.

**Skill side gotchas inherited from v0.4 work:**
- `install.py:_extension_backend_available()` probes doctor on wizard
  *entry* — keep the analogous `_cloud_backend_available()` lazy if
  possible (only probe when the user is about to consider option 5),
  since cloud-doctor probes might cost more than extension-doctor (e.g.
  daemon may ping the cloud provider's API).
- The `chrome-popup-test-policy` memory is non-negotiable: any
  iterative/scripted Chrome testing goes through `browser-daemon
  launch-chrome --port <isolated> --profile /tmp/...`. **Never hammer
  the user's daily Chrome.**

> ### ⚠️ Detection contract for any new wizard option
>
> The v0.4 EMERGENCY-STOP incident (Allow popups on the user's daily
> Chrome) was root-caused to a misconfigured `BD_PORT=9444` collapsing
> to default `9222` and hitting the user's Chrome directly — **not** to
> the wizard's doctor probe, which is contract-bound to zero ws side
> effects (spec H3).
>
> However, that incident proves the failure mode is one Skill mistake
> away. **Every new wizard option `_<backend>_backend_available()`
> helper in v0.5+ MUST detect availability through
> `DaemonClient().doctor()` only.** Do not open a CDP ws, do not
> subprocess a backend's `--probe` command, do not curl a cloud API.
> If a future detection needs richer signal than doctor provides,
> extend the daemon's doctor schema first.

## E2E bug status (post-AI-agent-sdk-tester run)

| # | Side | Severity | Status |
|---|---|---|---|
| 1 | Skill | P1 | ✅ host_stem eTLD+1 (v0.3.1) |
| 2 | Skill | P2 | ✅ scaffold schema validation (v0.3.1) |
| 3 | Skill | P2 | ✅ propose always-dict (v0.3.1) |
| 4 | Skill | P3 | ✅ remember_preference docs (v0.3.1) |
| 5 | Daemon | P1 | ✅ `BD_RDP_PORT` env var support |
| 6 | Daemon | P2 | ✅ launch-chrome Chrome 148 poll race |
| 7 | Daemon | P2 | ✅ launch-chrome `--remote-allow-origins=*` |

All 7 fixed. The E2E harness can be re-run as a regression gate before
shipping v0.5 (entry point: `ai-e2e-tests/harness.py` — see daemon-impl-2's
recent SendMessage for details).

## Review remediation summary (v0.5.1)

After agent-sdk-tester's 4/4 LIVE pass, an independent reviewer-1 pass
produced REVIEW.md with **28 findings** across the three teammates. Status
as of the v0.5.1 / daemon-0.5.3 / ai-e2e-0.2.x ship window:

| Side | Closed | Deferred | Total |
|---|---|---|---|
| skill (this repo) | **12** | 0 | 12 |
| daemon | **12** | 1 (Task #15, P3) | 13 |
| ai-e2e harness | **2** | 1 (Task #29, v0.6 path-B real-extension E2E) | 3 |
| **Total** | **26** | **2** | **28** |

Skill-side findings closed in v0.5.1 (this release):

| ID | Tier | Title | Where |
|---|---|---|---|
| F-4 | P0 | Primitive surface drift (96 ↔ 17) | `api.py`, `primitives/*` (+13 wrappers, +3 re-exports) |
| F-4b | P0 | Production hardening only in harness | new `_hardening.py` (port 9222 listener + daemon-url cross-check) |
| F-4d | P0 | `BS_CDP_WS` short-circuit bypassed gate | `repl/inline.py` (refuse :9222) |
| F-5d | P0 | Stale Mode-B daemon serving wrong backend | `errors.DaemonBackendMismatch` + `ModeBClient.assert_backend_matches()` |
| F-5c | P1 | US3R + US5 missing from design.md §0 | `design.md` §0 (6-US horizontal map) |
| F-7 | P1 | OUTPUT_SCHEMA half-shipped | `scaffold.py` template + `propose._infer_output_schema()` |
| F-8 | P1 | `fallback_chain` documented but never implemented | `design.md` retraction footnote |
| F-9 | P1 | Bug-fix coverage gaps | 14 extra `host_stem` cases / 5 args-schema variants / 4 propose paths / docstring caveat |
| F-12 | P1 | Mode-A missing `disconnect_upstream` | `daemon_client.py` no-op stub |
| F-13 | P1 | TOML escape control-char gap | `install._toml_escape()` reject 0x00-0x1F/0x7F |
| F-16 | P2 | CLI `save` ↔ narrative `solidify` | `cli.py` alias |
| F-17 | P2 | `warm_upstream` dead code | docstring removal target v0.6 |

Two **incidental real bugs** surfaced while broadening test coverage and
were fixed alongside:

- `host_stem("github.com.")` previously didn't strip the FQDN trailing
  dot — now `strip(".")` in `_split_host`.
- args-schema validator didn't reject non-string keys (e.g.
  `{("q",): {...}}`) — now explicit `ValueError` with the corrected
  shape example.

Deferred:

- **Task #15 (daemon, P3)**: `fallback_chain` config.toml respect vs.
  remove-from-docs. Skill side already deleted the schema reference
  (F-8); daemon decision can wait for a v0.6 design discussion.
- **Task #29 (ai-e2e, v0.6)**: real Chrome-extension load E2E. Path-B
  needs `chrome.debugger` permission grant in headless / CI Chrome,
  which is a non-trivial harness rewrite — explicit v0.6 milestone.

## Repo navigation

```
browser-skill/                  ← skill (Layer 2)
  design.md                     ← authoritative spec (1667 lines)
  README.md                     ← user-facing docs incl. v0.4 + v0.5 sections
  ONBOARDING.md                 ← new-contributor decision tree + test policies
  src/browser_skill/
    cli.py / __main__.py        ← argv dispatch
    api.py                      ← re-exports for `from browser_skill import *`
    daemon_client.py            ← Mode A subprocess client
    mode_b_client.py            ← Mode B socket client + auto_client() factory
    multitask.py                ← run_tasks_concurrent() fan-out
    install.py                  ← wizard (5 options; cloud option 5 + TOML emit)
    _hardening.py               ← v0.5.1 F-4b production checks (port 9222 + daemon-url)
    repl/inline.py              ← P0 #75 popup-cost abort gate + F-4d BS_CDP_WS validate
    primitives/{page,interact,inspect,site,http,discovery_api}.py
    memory/{site_mem,global_mem,_md,_yaml}.py
    solidify/{propose,extract,scaffold}.py
    site_skills_starter/        ← bundled sites (eTLD+1 directory names)
  tests/                        ← 229 tests; uv run pytest -q
    test_autoconnect_inline_abort.py     ← P0 #75
    test_install_extension_v04.py        ← v0.4 wizard wire
    test_install_cloud_v05.py            ← v0.5 cloud + live verify
    test_e2e_bugs_v031.py                ← Bug 1-4 fixes
    test_primitives_f4_catchup.py        ← v0.5.1 F-4 primitive ports
    test_p0_hardening.py                 ← v0.5.1 F-4b/F-4d/F-5d
    test_p1_coverage_gaps.py             ← v0.5.1 F-7/F-9/F-12/F-13/F-16/F-17

browser-daemon/                 ← daemon (Layer 1)
  design-v2.md                  ← authoritative spec (1001 lines)
  chrome-extension/             ← v0.4 unpacked extension (Manifest V3)
  src/browser_daemon/
    backends/{autoconnect,rdp,env,extension}.py
    server/{proxy,relay}.py     ← Mode B + extension relay
    cli/                        ← serve, url, doctor, launch-chrome, etc.
  tests/                        ← 153 tests

ai-e2e-tests/                   ← live AI-agent harness (Claude OAuth)
  harness.py                    ← entry; dry-run flag available
```

## Operating principles (carry these forward)

- **Test policy — Chrome**: `chrome-popup-test-policy` memory +
  `chrome-popup-accumulation-bug` memory. All iterative tests run
  against an isolated Chrome profile. Never short-connect ws to the
  user's daily Chrome — Chrome 144+ accumulates Allow popups and
  eventually freezes.
- **Test policy — filesystem**: any code path that writes outside the
  test tree (`~/.config/browser-daemon/...`, `~/.browser-skill/...`,
  etc.) MUST accept a `*_PATH` env override so tests can redirect to
  `tmp_path`. The canonical example is `install._daemon_config_path()`
  reading `BS_DAEMON_CONFIG_PATH`; the test driver
  `tests/test_install_cloud_v05.py::_drive_wizard` shows the
  monkeypatch pattern. This is the filesystem analogue of the Chrome
  popup rule — tests must not pollute user state, period.
- **Spec authority**: `design.md` (skill) and `design-v2.md` (daemon)
  are the source of truth. Footnote when behavior evolves.
- **Doctor as contract**: `browser-daemon doctor --json` is contract-bound
  to zero side effects (Spec H3). Skill features that key on doctor
  output (install wizard option 4, option 5, planned option 6+) rely
  on this property. The regression guard in
  `test_install_cloud_v05.py::test_doctor_probe_is_the_only_detection_channel`
  patches `socket.socket` to a tripwire — any new helper that touches
  the network outside doctor will trip this test immediately.
- **Forward-compat install wizard**: any new daemon backend that lists
  itself in `doctor --json` is auto-surfaceable by the wizard if you
  pattern after `_extension_backend_available()` /
  `_cloud_backend_available()`. No Skill ship needed to expose a
  daemon-side backend addition once the wizard option is wired.
- **Schema lock as test enforcement**: contract drift is best caught by
  a test that asserts the contract shape, not by a docstring. The
  daemon repo's `test_schema_lock.py` (REVIEW.md F-1+F-2) does this
  for doctor JSON; the skill side's cousin is the **detection-contract
  socket tripwire** in `tests/test_install_cloud_v05.py::test_doctor_probe_is_the_only_detection_channel`
  — it patches `socket.socket` to a `RuntimeError` and asserts no
  helper opens a network connection while consuming `doctor --json`.
  When you add a new daemon-shaped contract (per-backend extras, new
  RPC, etc.), write the equivalent lock test alongside it. A
  contract test that runs in CI is the only enforcement that survives
  the next refactor.

## Open items for v0.5

- Auth provider abstraction shape (per-backend or generic?). Daemon
  decides; Skill consumes via a doctor-reported `auth: ...` field.
- `browser-skill memory show --global` should probably mask credential
  handles. Currently it dumps frontmatter verbatim.
- v0.5 may want a `browser-skill credentials` subcommand to set/list/rotate
  cloud credentials via daemon — out of scope for the wizard alone.
- ai-e2e-tests harness should gain a "cloud backend" test case (mocked
  upstream) to catch regressions in the credential flow.
