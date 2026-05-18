# Campaign Review — browser-daemon + browser-skill

**Reviewer**: reviewer-1 (independent fresh teammate, no prior involvement)
**Date**: 2026-05-18
**Scope**: design + code + tests + AI-E2E harness + CI + docs
**Versions audited**: browser-daemon 0.5.0 (228 tests), browser-skill 0.3.1 (146 tests)
**Methodology**: 4 parallel deep-dive subagents (daemon-drift, skill-drift, bug-fix-verification, user-vision-E2E) + direct foreground audit of P1/P2 angles. Read-only mandate — no framework code modified.

## Executive Summary

The campaign delivers a working, well-tested AI-agent browser harness on time, with serious thought given to test policy (Chrome popup defense), config layering (XDG-aware, env-overridable), and forward-compat surfaces (AuthProvider Protocol, doctor schema). Two unambiguously good design choices: **doctor-only detection contract** for the install wizard, and the **inline-heredoc abort gate (P0 #75)** that protects the user's real Chrome from popup accumulation.

However, the audit surfaces three classes of issue that the team's internal reviews missed:

1. **Schema-lock breaches** — `schema_version=1` doctor contract was bumped silently. Both a 5th `ux_cost` enum value (`"auth-required"`) and a brand-new `extras` field were added without bumping the version. Any v0.1-compliant Skill that hard-codes the documented enum will see unknowns.
2. **Documented features that don't exist** — README/design.md advertise primitives, config keys, and event names that the code never implements. The previously-found `default_backend` drift was actually the tip of the iceberg: `fallback_chain`, `profile_paths`, `relay_url`, plus ~15 spec-named Skill primitives (`type_text`, `fill_input`, `wait_for_element`, `iframe_target`, `http_get`, etc.) are silently absent. The "96 surface items adopted" handoff claim is materially wrong.
3. **Spec-documented protocol events never fire** — `BrowserDaemon.upstreamConnecting` / `upstreamReady` are documented in design-v2.md §6.4 but emitted by no code path. Any Skill subscriber is dead code.

Net assessment: the visible 80% works well. The hidden 20% (spec/doc accuracy, schema discipline, advertised-but-unparsed config) deserves a targeted v0.6 sprint before promoting the framework outside the original authors. **None of these findings block current users, but several would break a v2 client written strictly to spec.**

---

## Critical findings (P0) — must fix before v0.6

### F-1: `schema_version=1` lock breached by silent enum extension

**Where**: `browser-daemon/src/browser_daemon/backends/base.py:26-29`; `backends/cloud.py:40`; `tests/test_doctor.py:27-28`.
**Issue**: `design-v2.md` §5.2 defines `schema_version=1` as a hard contract — "在 v0.x 内永远不变，break 必须 bump major." The `ux_cost` enum is locked to four values: `"none" | "banner" | "popup-per-ws+banner" | "extension-permission"`. Code added a fifth value `"auth-required"` for the cloud backend without bumping `SCHEMA_VERSION` in `doctor.py:22`. The test fixture `KNOWN_UX_COSTS` was edited to accept it, hiding the breach.
**Evidence**:
```
backends/base.py:26-29  UxCost = Literal["none", "banner", …, "auth-required"]
backends/cloud.py:40    ux_cost = "auth-required"  # v0.5 — new enum value
doctor.py:22            SCHEMA_VERSION = 1           # unchanged
```
**Impact**: A Skill v0.1 client doing `if e["ux_cost"] not in KNOWN_VALUES: raise` will hard-fail against v0.5 daemon. Schema-version trust is now broken.
**Recommended fix**: Either (a) bump `SCHEMA_VERSION` to 2 and document the v2 contract, or (b) re-classify cloud as one of the existing four (cloud's true UX cost is "none" — credentials are config-time, not popup-time).

### F-2: `schema_version=1` also breached by adding `extras` field

**Where**: `browser-daemon/src/browser_daemon/backends/base.py:64`; `doctor.py:123`; `tests/test_doctor.py:22-25`.
**Issue**: design-v2.md §5.2 lock lists exactly 7 keys per backend entry (`name, available, ws_url, detail, ux_warning, needs_user_action, ux_cost`). Code adds an 8th key `extras` for cloud-backend wizard-prefill (HANDOFF-v0.5.md:108 expects it), again without bumping `schema_version`. `EXPECTED_BACKEND_KEYS` in the test was updated to the 8-key set, masking the breach.
**Impact**: Same as F-1 — strict-shape Skill clients break silently.
**Recommended fix**: Bump `schema_version` to 2, or move `extras` to a sibling top-level (`extras_by_backend: {name → extras}`) so backend entries remain spec-compatible.

### F-3: Spec-documented lifecycle events never fire

**Where**: `browser-daemon/src/browser_daemon/server/proxy.py`, `listener.py`.
**Issue**: design-v2.md:550-551 documents two Mode-B events — `BrowserDaemon.upstreamConnecting` and `BrowserDaemon.upstreamReady`. `grep -rn "upstreamConnecting\|upstreamReady" src/ tests/` returns **zero matches**. Only `upstreamClosed` (listener.py:556) and `activeTabChanged` (proxy.py:728) actually emit.
**Impact**: Any Skill code subscribing to these events (per spec) is dead. The whole "wait for upstream ready" pattern documented in the design is unobservable in practice.
**Recommended fix**: Either implement the two events at the points where the upstream state transitions to `connecting` / `ready` in `_UpstreamHolder`, or remove them from the spec.

### F-4: ~15 spec-documented Skill primitives are not implemented

**Where**: `browser-skill/src/browser_skill/api.py:21-44`; `primitives/{page,interact,inspect,site}.py`.
**Issue**: `design.md` §A.2 + `HANDOFF-v0.5.md` claim "96 surface items from browser-harness adopted as-is". `EXPORTS` actually exposes ~17 primitives. Missing names referenced elsewhere in the same spec / README §A.3 ("Screenshot-first vs DOM-first" downgrade table) include:

  - Navigation: `ensure_real_tab`, `iframe_target`
  - Input: `type_text`, `press_key`, `fill_input`, `scroll`, `dispatch_key`, `upload_file`
  - Waiting: `wait_for_element`, `wait_for_network_idle`
  - Events: `drain_events` (exists on CDP transport but unwrapped), `http_get`
  - Dialogs: `handle_dialog`
  - Site/Layer 3: `list_site_skills`, `load_site_skill`, `run_task`, `try_recover_from_drift`

**Impact**: An agent reading the design or README §A.3 will hit `NameError` on every named downgrade path. The "Screenshot-first vs DOM-first" guidance is partially aspirational. This is the largest user-facing drift in the campaign.
**Recommended fix**: Either ship the missing primitives (most are thin wrappers over `cdp.send()`), or footnote design.md / README to mark v0.1 scope explicitly. Update HANDOFF to retract the "96 surface items" claim.

### F-4b: Chrome popup "3-layer defense" lives in the test harness, not production code

**Where**: `ai-e2e-tests/harness.py:204-246` (`assert_safe_environment`); `harness.py:249-282` (`assert_daemon_resolves_to_isolated`).
**Issue**: HANDOFF + memory describe a 3-layer defense (assert_safe / chain-lock / daemon-url assertion). Reality: layer 1 and layer 3 live **only** in the AI-E2E test harness, not in `browser-skill` or `browser-daemon`. Production users do not get these guards. Only the inline-abort gate (P0 #75, `repl/inline.py:74-130`), the daemon rate-limit (autoconnect.py:190-222), and `launch-chrome` refusing the default profile are real production defenses. Two of the three "layers" the team feels protected by are test-only.
**Evidence**: `grep -rn assert_safe_environment browser-skill/src browser-daemon/src` returns no matches.
**Impact**: A user-run `browser-skill` invocation has no equivalent of `assert_safe_environment` to catch a bad `BS_CDP_WS` or stale daemon. The defense is real in CI; aspirational in production.
**Recommended fix**: Either (a) port the two assertions into `browser-skill` startup (idempotent, fast) under an opt-in env flag, or (b) update HANDOFF/memory to honestly describe the layered defense as "test-time harness + production rate-limit + inline abort". Don't let the team believe in protections that only exist in CI.

### F-4c: `BD_PORT` typo silently collapses to default Chrome — the v0.4 incident root cause is NOT actually mitigated

**Where**: `browser-daemon/src/browser_daemon/config.py:190-209`; `grep -rn "BD_PORT\b" browser-daemon/src/` → **zero matches**.
**Issue**: The v0.4 emergency-stop incident (Allow popups on the user's daily Chrome) was root-caused per HANDOFF:219-224 to `BD_PORT=9444` collapsing to default `9222`. The fix was to introduce a **new** env name `BD_RDP_PORT`. But there is **no warning, deprecation message, or alias** for the old (and intuitive) name `BD_PORT`. A user who today re-types `BD_PORT=9444 BD_BACKEND=rdp browser-skill ...` gets the **exact same silent collapse** as the original incident.
**Impact**: Same emergency, one env-var typo away. Memory `chrome-popup-accumulation-bug.md` says "framework must defend, not just doc". This is undefended.
**Recommended fix**: Add a startup-time warning in `config.py:load()`: if `BD_PORT` is present but `BD_RDP_PORT` is not, emit a clear "did you mean BD_RDP_PORT? `BD_PORT` is not read." stderr message. Or alias `BD_PORT` → `BD_RDP_PORT` outright.

### F-4d: `BS_CDP_WS` short-circuits the popup-abort gate without validating the target

**Where**: `browser-skill/src/browser_skill/repl/inline.py:74-130`; `daemon_client.py:67-69`.
**Issue**: The inline-abort gate (P0 #75) explicitly **proceeds** if `BS_CDP_WS` is set ("direct ws override → no daemon, no popup" per comment). But `BS_CDP_WS` is returned verbatim with no host/port validation — if a stale `.env` or shell rc sets `BS_CDP_WS=ws://127.0.0.1:9222/...`, the inline path opens that ws and triggers a popup on every invocation. The bypass is too permissive.
**Recommended fix**: If `BS_CDP_WS` host/port matches `127.0.0.1:9222` (or any port discovered to be the user's daily Chrome via `DevToolsActivePort`), refuse to short-circuit — fall through to the abort gate. Or require `BS_FORCE_AUTOCONNECT_INLINE=1` to bypass even with `BS_CDP_WS` set when the target looks like the default Chrome.

### F-4e: Extension backend has no AI-E2E coverage — "live-verified" claim is mocks only

**Where**: `ai-e2e-tests/transcripts/` (zero `BD_BACKEND=extension` runs); HANDOFF-v0.5.md:10.
**Issue**: HANDOFF claims extension backend is "live-verified end-to-end". The extension is the **only** backend that delivers zero-popup against the user's daily Chrome (the user-vision-critical case). Yet no AI-E2E transcript drives it. Unit + relay tests are present, but no Claude-agent run touches the extension path. The "live" claim is doctor probes + mocked install-wizard tests.
**Impact**: The vision-bar "works on user's daily Chrome with no popup" is unverified end-to-end. Whatever bugs lurk in the Skill ↔ daemon ↔ relay ↔ extension chain only show under real agent load.
**Recommended fix**: Add a US-Ext story to ai-e2e-tests/harness.py. Either drive a real loaded extension or wrap the relay in a fake that the harness controls. Even one 30-second Claude run touching the path is enough proof.

### F-5: `default_backend` drift was incomplete — `fallback_chain`, `profile_paths`, `relay_url` still silently ignored

**Where**: `browser-daemon/src/browser_daemon/config.py:67-69` (BackendsConfig has only `rdp` and `cloud`); `README.md:253-264`.
**Issue**: Task #14 fixed `default_backend` parse. But three other config keys are advertised in `README.md` and never read by the parser:
```
config.toml example (README L253-264)        Parser status
default_backend = "autoconnect"              ✓ parsed
fallback_chain = ["env","rdp","autoconnect"] ✗ never read
[backends.autoconnect] profile_paths = […]   ✗ never read (autoconnect.py uses hardcoded platforms.profile_paths())
[backends.extension] relay_url = "…"         ✗ never read (extension.py uses hardcoded DEFAULT_RELAY_PORT)
```
This is the same silent-drift class as the original Task #14 bug.
**Impact**: Users following the README template believe these knobs work. They don't.
**Recommended fix**: Either parse all three (and add precedence tests as for `default_backend`) or strip them from README. Note: existing backlog task #15 already covers `fallback_chain`. The other two are not tracked.

---

## Important findings (P1)

### F-5b: AI-E2E-REPORT.auto.md is a stale dry-run, not a live agent run

**Where**: `ai-e2e-tests/AI-E2E-REPORT.auto.md` header says `Mode: dry-run (no Claude agent)`.
**Issue**: HANDOFF treats `AI-E2E-REPORT.auto.md` as auto-generated proof. The actual live agent runs are in `AI-E2E-REPORT.v05.auto.md` / `.rerun.auto.md`. Anyone reading top-down ("see auto-generated for full transcript") risks treating dry-run output as live evidence.
**Recommended fix**: Either re-generate `AI-E2E-REPORT.auto.md` live or delete it; HANDOFF should point to the live report.

### F-5c: User stories US3R + US5 are post-hoc, not in design.md §0

**Where**: `browser-skill/design.md:38-92` (only US1–US4); `AI-E2E-REPORT.md:111-273` (US3R + US5 added during E2E).
**Issue**: US3R was created because US3 inline-heredoc can't produce a usable `propose_solidify` (each heredoc is a fresh process; no REPL history). US5 is the cloud-backend dogfood. Both are good additions but they reveal real spec gaps:
- design.md §0 promises US3 works via "the framework" without distinguishing inline vs REPL. The inline path is actually broken for US3 by design (no persistent history). This should be documented as a Skill-level constraint.
- US5 has no design.md entry at all — the cloud backend has no user-story-level proof of correctness.
**Recommended fix**: Either fold US3R / US5 into design.md §0 (preferred — make the spec match reality), or move them to an appendix titled "post-design discovered stories" so future readers know they're not v0.1 baseline.

### F-5d: Stale Mode-B daemon can quietly serve the wrong backend

**Where**: `browser-skill/src/browser_skill/mode_b_client.py:66-102`.
**Issue**: `ModeBClient.discover()` trusts that a socket at `/tmp/browser-daemon-<name>.sock` matches the expected backend. If `BD_NAME=foo` exists from a previous session and the daemon was started with `--backend autoconnect`, the skill silently talks to it. `is_alive()` confirms socket liveness, not backend identity. No assertion that the daemon's `backend_name` matches what the skill expects.
**Recommended fix**: Add a daemon-identity assertion in `ModeBClient.connect()` — call `BrowserDaemon.doctor` or a new `BrowserDaemon.info`, verify `backend_name` matches the user's preference, raise/log otherwise.

### F-6: Undocumented Mode-B RPC methods + payload extensions

**Where**: `proxy.py:697-708`.
**Issue**: `BrowserDaemon.version` and `BrowserDaemon.stats` are wired but not in spec §6.4. `BrowserDaemon.uiState` returns 4 keys (adds `"client_count"` at proxy.py:678) instead of the spec-documented 3.
**Recommended fix**: Either update design-v2.md or remove the undocumented surface.

### F-7: `OUTPUT_SCHEMA` half-shipped

**Where**: `browser-skill/src/browser_skill/solidify/scaffold.py:13-35`; `task_runner.py:88`.
**Issue**: design.md:686 / line 1610 says v0.2 ships optional `OUTPUT_SCHEMA`. `task_runner.py` validates it if present, but the scaffold template only emits a plain `OUTPUT = "..."` string and `propose_solidify` never emits a `draft_output_schema`. There is no code path that produces a solidified task with the schema attached.
**Impact**: An advertised v0.2 feature has no production code path. Hand-written tasks are the only way to use it.
**Recommended fix**: Add `OUTPUT_SCHEMA = {...}` block to the scaffold template (commented-out optional), and an extracted `draft_output_schema` field in `propose_solidify` when types can be inferred.

### F-8: Skill memory `fallback_chain` documented but not implemented

**Where**: `browser-skill/src/browser_skill/memory/global_mem.py`; `design.md:917-922` (daemon block in global.md).
**Issue**: Spec lists `fallback_chain` as part of the `daemon:` frontmatter block. `grep -rn fallback_chain src/` → 0 hits.
**Recommended fix**: Same as F-5 — either implement read/write or remove the field from the docs.

### F-9: Bug-fix coverage gaps

Verifying the 8 known bug fixes:

| Bug | Status | Gap |
|---|---|---|
| #1 host_stem eTLD+1 | ⚠️ partial | 30 TLDs in set (not 26 as brief claims). Parametrize tests cover only 2 (`co.uk`, `com.cn`); `gov.uk`, `ac.jp`, `com.br`, `com.au`, `co.in` not exercised. No IDN/uppercase/trailing-dot/IP tests. |
| #2 args-schema | ⚠️ partial | `draft_args_schema` as None/string/tuple/set, schema with non-string keys, nested malformed entries — all untested. **Note**: team-lead brief's mental model (`{name, type}` list shape) doesn't match actual `{argname: {type:…}}` dict shape — clarify which is right. |
| #3 propose dict | ⚠️ partial | 3+ `ready=False` paths untested: `>30 success steps` penalty, captcha branch, `host=None`, empty-history-without-`like`. All construct the dict correctly, but the regression guards are bounded. |
| #4 dotted-key docs | ✅ thorough | Edge: overwriting non-dict scalar with dotted key silently destroys old value — undocumented. |
| #5 BD_RDP_PORT | ⚠️ partial | TOML-vs-env precedence not directly tested for the port (only for `default_backend`). |
| #6 poll race | ✅ correct test | Late write is faked via `asyncio.create_task`, not a real grandchild — subtle ordering not exercised end-to-end. |
| #7 `--remote-allow-origins=*` | ✅ argv asserted | No live ws integration test confirming 403 doesn't return. |
| #11 refuse default profile | ✅ 4 paths covered | Symlinked default profile (samefile vs str equality) untested. Env values `"yes"`/`"on"`/`"TRUE"` silently treated as false. |
| #12 homebrew wrapper | ✅ thorough | Hung binary case (subprocess times out at 3s) untested. |
| #14 `default_backend` | ✅ thorough | Invalid value (`"garbage"`, integers) silently ignored — runtime failure path not asserted. |

**Recommended fix**: Add parametrized tests covering the 6 listed TLDs for #1; the 3+ ready=False branches for #3; a TOML-vs-env case for #5. Document the dotted-key scalar-overwrite behavior in #4 docstring.

### F-10: `_pick_recommended` hardcodes stale v0.1 exclusion

**Where**: `browser-daemon/src/browser_daemon/doctor.py:127-143`.
**Issue**: `_pick_recommended` excludes `extension` from recommended candidates with comment "v0.1 hard-coded false". But v0.4 shipped the extension backend with `available=True`. The exclusion is now stale.
**Recommended fix**: Drop the `!= "extension"` filter; let `_UX_COST_RANK` (extension is `extension-permission`) decide naturally.

### F-11: `_needs_action` for `extension` says "planned v0.4" — v0.4 shipped

**Where**: `doctor.py:160`.
**Issue**: Static hint text in `list-backends` still reads "planned v0.4" months after v0.4 + v0.5 shipped. Also no `cloud` entry in `_needs_action`.
**Recommended fix**: Replace with current install hint; add a `cloud` row pointing to `browser-skill install` option 5.

### F-12: Mode-A client missing `disconnect_upstream`

**Where**: `browser-skill/src/browser_skill/daemon_client.py` vs `mode_b_client.py:220`.
**Issue**: Mode B exposes `disconnect_upstream()`; Mode A does not. If `Session`/REPL idle-disconnect logic ever runs against Mode A, it `AttributeError`s.
**Recommended fix**: Add a no-op `disconnect_upstream()` on Mode A (or raise a `NotSupported` with a clear message). Document the asymmetry.

### F-13: TOML escape is conservative but undocumented gaps

**Where**: `browser-skill/src/browser_skill/install.py:241-252`.
**Issue**: `_toml_escape` handles `\\ \" \n \r \t` only. TOML basic strings also require `\u00XX` for all other control chars (`0x00-0x08`, `0x0B`, `0x0C`, `0x0E-0x1F`, `0x7F`). Acceptable in practice (input domain is env-var names + paths from `input()`), but a malicious or weird value could produce invalid TOML.
**Recommended fix**: Either reject input containing any control char with a clear error, or extend the escape to cover the full set. Add a test with a `\x00` byte.

---

## Minor findings (P2)

### F-14: observability docstring claims 5 counter areas, code has 4

**Where**: `observability.py:54` (docstring) vs README:234.
**Issue**: Docstring lists `upstream/client/proxy/relay/auth`. README correctly says 4 groups. No `relay_*` counter exists.
**Recommended fix**: One-line docstring edit.

### F-15: `BD_LOG_JSON` env-var documented in feature text but missing from env-var table

**Where**: `browser-daemon/README.md:238` (feature) vs the env-var summary table (around L271-282) which omits it.
**Recommended fix**: Add a row.

### F-16: CLI `solidify` subcommand is actually named `save`

**Where**: `browser-skill/src/browser_skill/cli.py:454`.
**Issue**: design.md / README narrative uses "solidify"; the CLI subcommand is `save`. Listed in HELP block (cli.py:39) but not in README's example commands. Agents grepping for `solidify` find the Python primitive only.
**Recommended fix**: Either alias `solidify` → `save` in cli.py, or update narrative to use `save` consistently.

### F-17: `warm_upstream` parameter in `multitask.py` is dead code

**Where**: `multitask.py:93,118` accepts `warm_upstream` kwarg and silently ignores (the `_ = warm_upstream # accepted-but-ignored` line). Deprecation note acknowledges it's a no-op since daemon v0.3.
**Recommended fix**: Schedule removal in v0.6 / v0.7. Currently a foot-gun for anyone reading the signature.

### F-18: launch-chrome `--detach` flag is documented-as-reserved

**Where**: `browser-daemon/src/browser_daemon/cli.py:152-153` — flag accepted, help text says "ignored in v0.1 (always detaches); reserved." Either implement or remove.

### F-19: Naming convention inconsistency for backend selection envs

**Where**: `BD_BACKEND` (daemon), `BS_DAEMON_BACKEND` (skill side preference), `BS_FORCE_AUTOCONNECT_INLINE` (skill), `BD_FORCE_AUTOCONNECT_RECONNECT` (daemon). The `BD_` (daemon) vs `BS_` (skill) prefix split is logical but the cross-references are inconsistent. Document the full env-var matrix in one place (currently scattered across `daemon_client.py:9-15`, `install.py:509`, `autoconnect.py:78-82`).

---

## Strengths (carry these forward)

- **Doctor-as-contract pattern** is a beautiful idea, well-executed: `doctor --json` is the single channel for backend availability detection, with a tripwire test (`test_doctor_probe_is_the_only_detection_channel`) preventing socket/curl/subprocess regressions. Pattern after this for all future cross-layer surfaces.
- **Inline-heredoc abort (P0 #75)** + daemon rate-limit + launch-chrome refuse-default-profile is a textbook 3-layer defense for the Chrome popup accumulation bug. The fact that the team **memory-encoded** the policy (`chrome-popup-test-policy.md`, `chrome-popup-accumulation-bug.md`) so future agents inherit it is exemplary.
- **AuthProvider Protocol** in `auth.py` is genuinely well-designed: pure functions for static auth, `refresh()` hook for OAuth2, `ssl_context()` for mTLS, single dispatch table in `build_auth_provider()`. Adding a new auth_kind is one switch case + one dataclass. Forward-compat is real here.
- **Hand-rolled TOML writer with section-name constants** (`_CLOUD_TOML_TOP_SECTION` + `_CLOUD_TOML_AUTH_SECTION_FMT`) — avoids a tomli_w dep, keeps the writer surgical (only owns its sections), and uses single-source-of-truth constants. A future schema rename is one-place + one-grep.
- **The 3 live `browser-daemon doctor --json` subprocess tests** in `test_install_cloud_v05.py` are the right pattern for catching daemon-↔-skill schema drift early. Worth promoting to a general pattern: every Skill helper that consumes a daemon contract should have at least one live test.
- **228 + 146 = 374 tests** with subprocess-level integration in addition to mocks. Test pyramid shape is healthy.
- **HANDOFF-v0.5.md "Operating principles"** cross-references actual enforcement code (regression-guard tripwire, `*_PATH` env override pattern, `chrome-popup-test-policy` memory). Future contributors will inherit context, not just words.

## Recommendations for future work (v0.6+ priority order)

1. **(P0, week 1)** Bump `schema_version` to 2 OR retract the `ux_cost="auth-required"` + `extras` field additions. Add a `tests/test_schema_lock.py` that asserts schema fields against a frozen dict; that test then forces a deliberate bump on next change.
2. **(P0, week 1)** Either implement or retract the ~15 missing Skill primitives (F-4). If retracting, footnote design.md §A.2 explicitly. Update HANDOFF-v0.5.md "96 surface items" claim.
3. **(P0, week 1)** Implement `BrowserDaemon.upstreamConnecting` / `upstreamReady` events (F-3), OR remove from spec. Add Mode-B subscription tests.
4. **(P0, week 1)** Add `BD_PORT` typo warning (F-4c). One-liner in `config.py:load()` — but it closes the v0.4 incident's actual footgun rather than just renaming the variable.
5. **(P0, week 1–2)** Add a US-Ext AI-E2E story so the extension backend is no longer mock-only "live-verified" (F-4e). This is the only backend that delivers true zero-popup on user's daily Chrome.
6. **(P1)** Port `assert_safe_environment` / `assert_daemon_resolves_to_isolated` from `harness.py` into `browser-skill` startup (F-4b). Behind an opt-in flag so it's not always-on, but real production code.
7. **(P1)** Validate `BS_CDP_WS` target before short-circuiting the inline gate (F-4d). If it points at default-Chrome :9222, treat it like autoconnect and abort.
8. **(P1)** Parse `fallback_chain`, `profile_paths`, `relay_url` config keys (F-5; existing backlog task #15 covers fallback_chain — extend to all three).
9. **(P1)** Ship `OUTPUT_SCHEMA` scaffold emit + `propose_solidify` `draft_output_schema` extraction (F-7). Half-shipped today.
10. **(P1)** Mode-B daemon-identity assertion (F-5d) — `ModeBClient.connect()` should verify `backend_name` matches expectation.
11. **(P1)** Close the bug-fix coverage gaps in F-9.
12. **(P2)** Fix all P2 findings — mostly one-line docstring / README edits.
13. **(P2)** Document the full env-var matrix (BD_* vs BS_*) in one place. Fold US3R / US5 into design.md §0 or move to an appendix.
14. **(Forward-compat)** When OAuth2Auth is implemented in v0.6, ensure the Skill wizard's "coming v0.6" hint flips to live (pattern-matched after extension/cloud wave).

## User-vision delivery verdict: 7/10

The campaign delivers the core: 4 spec user stories pass with real Claude agents through `rdp + isolated profile`, with reproducible transcripts and a multi-layer popup defense documented as non-negotiable in memory. The 4-backend `doctor` schema makes "anything with CDP" extensible without skill changes. 374 tests; bug provenance traceable; AuthProvider abstraction is genuinely well-designed.

**But the campaign hides three real gaps**: (a) the celebrated "3-layer hardening" is half test-harness, not production code; (b) the v0.4 incident's actual footgun (`BD_PORT` typo) is undefended — only renamed; (c) the extension backend (the only one delivering true zero-popup on user's daily Chrome) has no AI-E2E coverage. The vision-bar "works with anything CDP" is aspirationally delivered; empirically delivered only for `rdp + launch-chrome` + mocked cloud + mocked extension. Confirmation-bias hazard: the auto-generated reports look thorough, but `AI-E2E-REPORT.auto.md` is a dry-run, US3R/US5 are post-hoc, and the multi-layer defense is one renamed env var away from re-incident.

## Methodology notes

- **Time-boxed.** ~30 min wall-clock with 4 parallel subagents + foreground audit.
- **Read-only.** No framework files modified. Only `REVIEW.md` written.
- **What I covered**:
  - All HANDOFF-listed files (design-v2.md, design.md, READMEs, ONBOARDING).
  - Daemon: `config.py`, `doctor.py`, `auth.py`, `cli.py`, all backend files, `server/listener.py`, `server/relay.py`, `server/proxy.py` (spot-checked).
  - Skill: `install.py`, `api.py`, `__init__.py`, `cli.py`, `memory/global_mem.py`, `memory/site_mem.py`, `solidify/*` (spot-checked).
  - All 10 bug-fix locations + their tests.
  - The 6 AI-E2E-REPORT*.md files (US coverage cross-check).
- **What I did NOT fully audit** (acknowledged limits):
  - **Tests didn't actually run** — verdicts are from code-reading + `pytest --collect-only` (228 daemon, 146 skill confirmed). A few "thorough" verdicts could hide flaky/skipped cases.
  - **Chrome extension code** (`chrome-extension/background.js`, `popup.js`, `manifest.json`) — only confirmed presence, not message-protocol cross-check against `extension_upstream.py`.
  - **Multi-client mux v0.3 depth** — registered as P1 in brief but not deeply audited. The `tests/test_multiclient.py` count was sighted (file exists), individual sessionId-translation + event-fanout-arbitration assertions not enumerated.
  - **AuthProvider mTLS** — code reviewed, but no actual TLS-handshake-against-peer test confirmed.
  - **AI-E2E transcripts/** — only the AI-E2E-REPORT*.md summaries were cross-referenced. Did not deep-read individual transcript files.
  - **Forward-compat angle 7** (v0.6/v0.7 plausible extensions) — covered only via the AuthProvider OAuth2 stub. MCP-server reachability not assessed.
  - **Test isolation between subprocess tests and `~/.config/`** — relied on the `BS_DAEMON_CONFIG_PATH` env-override pattern claim; did not trace every test that writes outside tmpdir.
- **Confidence**: High on P0 findings (each has file:line evidence). Medium on F-9 coverage-gap details (verified for the listed gaps but did not enumerate all 374 tests). Low on what's missing from my audit scope itself — there are surely findings in the un-audited corners.
