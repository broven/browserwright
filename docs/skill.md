# browserwright — Layer 2 of the browser stack

`browserwright` is the AI-agent-facing layer of the browser automation stack:

- **REPL** in three shapes (inline heredoc, long-lived unix-socket daemon,
  one-off task invocation).
- **Primitives** that talk CDP directly (no Playwright, no Selenium): screenshot,
  click, JS evaluation, navigation, raw CDP escape hatch.
- **Site-skills** — per-host directories with `SKILL.md` + `memory.md` + a `tasks/*.py`
  bundle. Five sites ship bundled: google, github, hacker news, wikipedia,
  producthunt.
- **Three-tier memory** — global preferences, per-site notes, in-process REPL state.
  Append-only by default; preference writes require explicit user confirm.

See `design.md` for the full specification.

## Install

```bash
uv sync --extra ux
```

The console scripts `browserwright` and `browserwright-daemon` are both
registered automatically — they ship from this single package.

## Usage

```bash
# inline heredoc (zero ceremony) — works with any backend.
browserwright <<'PY'
print(page_info())
PY

# Long-lived REPL daemon — single shared upstream ws for the whole session.
browserwright repl start
browserwright exec "print(page_info())"
browserwright repl stop

# Isolated background Chrome (zero popups, zero banner) — preferred for
# scripted / iterative work and the install wizard's default pick:
browserwright-daemon launch-chrome --port 9333 --profile /tmp/bs-isolated &
BD_PORT=9333 BD_BACKEND=rdp browserwright <<'PY'
print(page_info())
PY

# Extension backend — drives the user's daily Chrome via the unpacked
# relay extension, zero popups. See ../browserwright-daemon/README.md for setup.
BD_BACKEND=extension browserwright <<'PY'
print(page_info())
PY

# task invocation:
browserwright task wikipedia.org/lookup --title="Wikipedia"

# install wizard (defaults to isolated profile):
browserwright install

# daemon health:
browserwright doctor

# discoverability:
browserwright list-tasks
browserwright list-tasks --query="search the web"

# memory inspection:
browserwright memory show --global=true
browserwright memory show --site=github.com
```

### Memory: dotted-key preferences

`remember_preference(key, value)` interprets `key` as a YAML frontmatter
*path*, not a literal flat key. Dots open new mapping levels:

| Call | Frontmatter shape |
|---|---|
| `remember_preference("dark_mode", True)` | `dark_mode: True` |
| `remember_preference("daemon.preferred_backend", "extension")` | `daemon:`<br>&nbsp;&nbsp;`preferred_backend: extension` |
| `remember_preference("ui.theme.accent", "#ff0")` | `ui:`<br>&nbsp;&nbsp;`theme:`<br>&nbsp;&nbsp;&nbsp;&nbsp;`accent: '#ff0'` |

That's why the install wizard's `daemon.preferred_backend` write produces
a nested `daemon:` block in `global.md` instead of a flat
`daemon.preferred_backend:` line. Verify with
`browserwright memory show --global=true` — the output is the full
frontmatter tree.

## Tests

```bash
uv sync               # dev group brings in pytest + pytest-asyncio
uv run pytest tests/
```

30+ unit + integration tests; all green on macOS / Python 3.11. The default
suite uses subprocess mocks and an env-overridden CDP URL, so it never touches
real Chrome.

### Testing policy — do **not** hammer the user's daily Chrome

Chrome 144+ requires a fresh "Allow remote debugging?" popup for **every**
new CDP WebSocket on the default profile, with no memory between popups.
Iterative or scripted test runs that open dozens of WebSockets will spam the
user with that dialog. Two safe paths:

1. **Recommended for CI / iterative testing — `launch-chrome` isolated profile.**
   Spin a hidden background Chrome on a dedicated user-data-dir; nothing in
   the user's daily Chrome is touched and the popup never appears.

   ```bash
   browserwright-daemon launch-chrome \
       --port 9333 \
       --profile bs-test-profile \
       --persistent &
   # run a daemon against that isolated Chrome, then create an rdp session
   # so the skill talks to it over the daemon socket (Mode B):
   browserwright-daemon serve &
   browserwright session new --backend=rdp
   pytest tests/
   ```

   Note: on macOS Chrome 148, if the user's daily Chrome is already running,
   `launch-chrome` may not see `DevToolsActivePort` written even though the
   new Chrome IS up on the requested port. Workaround: `open -na
   "Google Chrome" --args --user-data-dir=/tmp/<unique> --remote-debugging-port=9333`
   and grab the ws URL from `http://127.0.0.1:9333/json/version`.

2. **One-shot live "does it actually work" verify against the user's real
   Chrome** uses the `extension` backend — load the unpacked relay
   extension once and subsequent calls all reuse the same upstream ws,
   zero popups.

## v0.4 — Browser-extension relay (zero popups, zero banner)

`browserwright-daemon` v0.4 ships a Chrome-extension backend that relays CDP
through the extension's `chrome.debugger` permission. It bypasses both the
"Allow remote debugging?" popup and the persistent CDP banner because the
extension itself holds the debugger handle. The Skill side is wired through
`browserwright install` option 3; the daemon side requires Mode B
(`browserwright-daemon serve` — one global daemon, no `--backend`/`--name`).

End-to-end setup (run once, then forget it):

```bash
# 1. Install the unpacked extension. browserwright install prints the
#    absolute path; alternatively ask the daemon directly:
browserwright-daemon extension-path --json
# → {"path": "/.../browserwright-daemon/chrome-extension"}
# Then in Chrome: chrome://extensions → Developer mode → Load unpacked
# → pick that directory.

# 2. Start the daemon as a Mode B relay (one global daemon, fixed socket).
browserwright-daemon serve

# 3. Click the extension icon → "Attach this tab".

# 4. Skill picks the running socket automatically:
browserwright <<'PY'
print(page_info())
PY

# 5. Persist the preference so future installs / agents keep using extension:
browserwright install   # → choose 3
```

`browserwright-daemon doctor --json` lists the extension backend with
`available=true` once the relay is alive. The Skill install wizard's option 3
auto-flips from "(daemon reports extension backend not yet available)" to
live based on that doctor signal — no Skill release required to surface a
freshly-shipped daemon backend.

## v0.5 — Cloud / remote-browser backend

`browserwright-daemon` v0.5 ships a `cloud` backend that fronts third-party
remote-Chrome services (Browser Use, Browserless, Hyperbrowser, or any
CDP-compatible cloud endpoint with the `generic` provider). The daemon's
built-in `AuthProvider` abstraction handles Bearer / Basic / mTLS
credential injection so Skill never has to see the secret itself.

### Backend chooser — pick one

| Backend | Picked via | Chrome process | Popup | Banner | Credentials live in |
|---|---|---|---|---|---|
| `rdp` (isolated profile) | wizard option **1** (default) — `browserwright-daemon launch-chrome` | dedicated background user-data-dir | ❌ | ❌ | n/a |
| `rdp` (fingerprint browser) | wizard option **2** — user runs AdsPower / MultiLogin / etc. | user's fingerprint browser | ❌ | ❌ | n/a |
| `extension` | wizard option **3** — load unpacked extension | user's daily Chrome via `chrome.debugger` | ❌ | ❌ | n/a |
| `cloud` | wizard option **4** | none (remote) | ❌ | ❌ | env var name / file path / URL-embedded — **never the secret itself** |

`cloud`'s `ux_cost` is `"auth-required"` — the daemon surfaces auth failures via ordinary CDP error paths, no popup involved.

### Setup walkthrough

```bash
# 1. Install browserwright-daemon v0.5+ and verify the cloud backend is registered.
browserwright-daemon doctor --json
# → look for {"name": "cloud", "available": true, "ux_cost": "auth-required",
#             "extras": {"provider": ..., "auth_kind": ..., ...}}

# 2. Run the wizard — pick option 4.
browserwright install
#   ...
#   Choose 1 / 2 / 3 / 4 [1]: 4
#   Provider (browser-use / browserless / hyperbrowser / generic) [browser-use]:
#   Auth kind (bearer / basic / mtls; oauth2 coming v0.6) [bearer]:
#   ...
#   wrote daemon.preferred_backend = 'cloud' to ~/.browserwright/global.md
#   wrote [backends.cloud] + [backends.cloud.auth.*] sections to ~/.config/browserwright-daemon/config.toml

# 3. Start the daemon in cloud-relay mode.
browserwright-daemon serve --provider browser-use

# 4. Verify the connection.
browserwright-daemon doctor --json
# → "cloud" backend now shows "available": true with a "ws_url"

# 5. Skill picks up the running socket — same workflow as every other backend.
browserwright <<'PY'
print(page_info())
PY
```

### Three auth-kind flows

The wizard collects *references* to credentials, never the credentials
themselves. The daemon's `AuthProvider` reads the live values from env,
disk, or the URL at `serve` startup.

#### Bearer (Browser Use, Hyperbrowser, generic)

```bash
browserwright install   # → 5 → browser-use → bearer
# wizard prompt: env var name → e.g. BROWSER_USE_API_KEY
# wizard prompt: endpoint → wss://api.browser-use.example/ws  (optional)

# Then export the token before serving:
export BROWSER_USE_API_KEY=<your-token>
browserwright-daemon serve --provider browser-use
```

#### Basic (Browserless, generic with HTTP basic auth)

```bash
browserwright install   # → 5 → browserless → basic
# wizard prompts: username_env name → e.g. BROWSERLESS_USER
#                 password_env name → e.g. BROWSERLESS_PASS
#                 endpoint (BARE URL, no creds) →
#                   wss://chrome.browserless.io/ws

# Then export both env vars before serving. The daemon's BasicAuth
# reads them at startup and emits `Authorization: Basic <b64>` headers
# upstream — credentials never enter the URL or Skill memory.
export BROWSERLESS_USER=<your-username>
export BROWSERLESS_PASS=<your-password>
browserwright-daemon serve --provider browserless
```

> The wizard refuses `wss://user:pass@host/...` style URLs even though
> the daemon supports `embed_in_url=true` mode — the URL form would put
> the secret in Skill memory, which violates the "Skill never holds
> credentials" contract. If you genuinely need URL-embedded basic auth,
> hand-edit `~/.config/browserwright-daemon/config.toml` to add
> `embed_in_url = true` after the wizard runs.

#### mTLS (enterprise / generic with client certs)

```bash
browserwright install   # → 5 → generic → mtls
# wizard prompts: cert path → /opt/certs/client.crt
#                 key  path → /opt/certs/client.key
#                 endpoint  → wss://api.example/cdp   (optional)

browserwright-daemon serve --provider generic
# daemon loads both PEMs at startup; secrets never enter Skill memory.
```

### `oauth2` is coming in v0.6

The wizard recognises `oauth2` by name and refuses with a version-specific
hint (`"oauth2 auth is coming in v0.6 — not yet supported by daemon. Pick
bearer / basic / mtls."`). This is deliberate — typing it on purpose
should teach the user when to expect it, not pretend it's an unknown
auth_kind.

### What gets persisted (and what doesn't)

```yaml
# ~/.browserwright/global.md (frontmatter)
daemon:
  preferred_backend:    cloud
  cloud_provider_hint:  browser-use
  cloud_auth_kind:      bearer
  cloud_endpoint:       wss://...           # if collected
  # bearer: env-var *name* (never the token itself):
  cloud_token_env:      BROWSER_USE_API_KEY
  # basic (instead of bearer): two env-var names:
  # cloud_username_env: BROWSERLESS_USER
  # cloud_password_env: BROWSERLESS_PASS
  # mtls (instead of bearer): cert + key paths:
  # cloud_cert_file:    /opt/certs/client.crt
  # cloud_key_file:     /opt/certs/client.key
```

```toml
# ~/.config/browserwright-daemon/config.toml
[backends.cloud]
endpoint      = "wss://..."
auth_kind     = "bearer"
provider_hint = "browser-use"           # display name, daemon doesn't act on it

[backends.cloud.auth.bearer]            # <- one of:
token_env     = "BROWSER_USE_API_KEY"   #    .bearer / .basic / .mtls
                                        # daemon dispatches AuthProvider
                                        # implementations off this subtable
```

**Never** persisted: bearer tokens, mTLS private-key contents, basic-auth
URL passwords (the URL itself is stored, but it *is* the credential by
design — RFC 3986 URI). If the daemon-side `AuthProvider` ever needs to
escalate to a real secret store (OS keychain, Vault, etc.), that's
daemon work — Skill keeps holding only references.

> **Daemon-side reference**: see [browserwright-daemon README, v0.5 cloud
> backend section](../browserwright-daemon/README.md#v05-cloud-backend) for the
> daemon's own protocol + auth contract.

## v0.5.1 — Review remediation release

After the v0.5.0 ship landed (cloud backend + Schema-correct wizard
emit), independent reviewer-1 produced REVIEW.md with 12 skill-side
findings. v0.5.1 closes all 12 (and 2 incidental bugs that surfaced
during the audit) with **229 tests** passing.

**Surface completeness (F-4)** — 13 documented-but-missing primitives
shipped: `type_text`, `press_key`, `fill_input`, `scroll`,
`dispatch_key`, `upload_file`, `wait_for_element`,
`wait_for_network_idle`, `drain_events`, `ensure_real_tab`,
`iframe_target`, `http_get`, plus three Layer-3 re-exports
(`list_site_skills`, `load_site_skill`, `run_task`). `EXPORTS` grew
from 23 → 36. Two primitives remain deferred to v0.6 with explicit
footnotes in `design.md` §A.2: `handle_dialog`, `try_recover_from_drift`.

**Production hardening (F-4b) — removed (2026-05)** — the popup-defense
assertions previously lived in `_hardening.py` and gated `repl start`,
`task`, and the inline heredoc against the `autoconnect` backend's
Chrome 144+ popup-accumulation hazard. With autoconnect deleted, the
gate no longer has a purpose; the module and its tests were dropped.

**Coverage & contract hygiene (F-7 / F-9 / F-12 / F-13 / F-16 / F-17)** —
scaffold template now emits `OUTPUT_SCHEMA` (commented placeholder or
inferred from `return {...}` / `return [...]`); 14 more `host_stem`
multi-label TLD cases; args-schema rejects non-string keys; TOML emit
rejects unsafe control characters; `warm_upstream` flagged for removal
in v0.6.

**Two incidental bug fixes** surfaced during expanded coverage and
were shipped alongside: `host_stem("github.com.")` now strips the FQDN
trailing dot, and `_validate_args_schema` now rejects non-string keys
with a clear `ValueError`.

Full per-finding table + cross-team status: see `../HANDOFF-v0.5.md`
"Review remediation summary" section.
