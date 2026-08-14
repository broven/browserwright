# Releasing & updating the local install

How a browserwright version is published, and how to get that version onto a
machine (the global `uv tool` install + the Chrome extension). Read this with
[AGENTS.md](AGENTS.md) and [ONBOARD.md](ONBOARD.md).

## TL;DR

```bash
# 1. cut a release  (CI publishes to PyPI + GitHub Release on tag push)
git tag -a vX.Y.Z -m "browserwright X.Y.Z — <summary>"
git push origin vX.Y.Z

# 2. update this machine to the just-released version
mise run upgrade-global
```

Nothing polls for new versions. Step 2 is **manual** — there is no background
auto-upgrader. The only automatic piece is the in-Chrome extension reload (see
[Extension auto-reload](#extension-auto-reload)).

---

## How a release is built

Releases are driven entirely by a **git tag** matching `v*`
(`.github/workflows/release.yml`):

- The repo's `pyproject.toml` `version`, `chrome-extension/manifest.json`
  `version` and `pi-extension/package.json` `version` are intentionally **NOT
  bumped per release** — all three stay at the placeholder **`0.0.0`** in git.
  CI overwrites them **from the tag** at build time. So the tag is the single
  source of truth for the version; do not edit any of them in a release commit.
- The placeholder is `0.0.0` so it can never be mistaken for a real release. It
  previously sat at `0.6.2` — a version that *had* shipped — which made every
  checkout look like it was eight releases behind (`browserwright version` said
  `0.6.2` while tags were at `v0.8.0`) and made a locally built wheel look
  installable when it was really an unstamped dev build.
- `tests/skill/test_release_versioning.py` (in the fast gate) replays the
  workflow's own stamping code against the current `pyproject.toml` /
  `manifest.json` / `pi-extension/package.json` and fails if CI could no longer
  stamp them — a trailing comment on the `version` line, a second top-level
  `version =`, a switch to `dynamic = ["version"]`, drift between the three
  placeholders, or the three jobs' tag regexes disagreeing. Without it those
  break the release **after** the tag is pushed, when the only fix is deleting
  and re-cutting the tag.
- Job `publish-pypi` builds the sdist+wheel and uploads to **PyPI** via OIDC
  **trusted publishing** (no stored token), gated on the GitHub `pypi`
  environment.
- Job `publish-extension` stamps the manifest, zips `chrome-extension/` into
  `browserwright-extension-<version>.zip`, and attaches it to a **GitHub
  Release** named `vX.Y.Z`.
- Job `publish-npm` stamps `pi-extension/package.json`, runs that package's unit
  tests, and publishes **`@browserwright/pi`** to npm via OIDC trusted
  publishing (no stored token), gated on the GitHub `npm` environment. See
  [ADR-0008](docs/adr/0008-pi-extension-is-a-subpackage.md) for why the pi
  extension lives in this repo.
- Job `publish-cws` repackages `chrome-extension/` with `manifest.json` at the
  zip root (the shape CWS requires) and publishes it to the **Chrome Web
  Store** via the official Upload API, gated on the GitHub `cws` environment.
  It runs **only on pure `X.Y.Z` tags** — pre-release tags (`vX.Y.Z-rc*`) skip
  it (see [Store & pre-releases](#store--pre-releases)).

The three jobs are independent: none waits on the others, and a failure in one
does not roll back the rest. That is why the tag-regex agreement is asserted in
the fast gate rather than discovered at publish time.

Pick the next version by bumping the latest tag (`git tag --sort=-v:refname |
head`). Patch for fixes, minor for features.

---

## First npm publish

npm's trusted publishing has a bootstrapping problem that PyPI's does not:
**the package must already exist before a trusted publisher can be configured
for it.** So the first release is manual, and every one after it is automatic.

### One-time, before the first release

1. Create the **`@browserwright` org** on npmjs.com (free for public packages).
2. **Publish `0.0.1` by hand, once**, so the package exists:
   ```bash
   cd pi-extension
   npm login
   npm version 0.0.1 --no-git-tag-version   # do NOT commit this
   npm publish --access public
   git checkout package.json                # restore the 0.0.0 sentinel
   ```
   Restoring the sentinel matters: `test_release_versioning.py` fails the fast
   gate if `package.json` carries anything but `0.0.0` in git.
3. On npmjs.com → Packages → `@browserwright/pi` → **Settings → Trusted
   publishing**, add a GitHub Actions publisher:

   | field | value |
   |---|---|
   | Organization / repository | `broven/browserwright` |
   | Workflow filename | `release.yml` |
   | Environment | `npm` |

4. Create the **`npm` environment** in the repo's GitHub settings (Settings →
   Environments → New environment). The job declares
   `environment: {name: npm}`, so publishing fails without it.

That is it — no token is stored anywhere.

### What makes it work

- `permissions: id-token: write` on the job. Without it npm gets no OIDC token.
- npm **>= 11.5.1**. An older npm fails with a bare authentication error and no
  hint about the cause, which is why the job pins it explicitly.
- Provenance attestations are generated automatically; no `--provenance` flag.

### Things that will break it

- **Renaming `release.yml`.** Trust is pinned to the exact
  org/repo/workflow-filename triple. Rename the file and every publish fails
  until the trusted publisher is updated to match.
- **Publishing from a fork or another repo** — rejected by design.
- **Self-hosted runners.** Only GitHub-hosted runners issue OIDC tokens npm
  accepts.
- **A second package under the same org** needs its own trusted-publisher entry;
  the setting is per package, not per org.

### Fallback: a stored token

If trusted publishing cannot be used, add an `NPM_TOKEN` repository secret and
give the publish step:

```yaml
env:
  NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
```

Same trade-off as the PyPI API-token fallback below: it works, but it is a
long-lived credential sitting in repo settings, and it is what trusted
publishing exists to remove.

---

## Fixing the PyPI auto-publish (`invalid-publisher`)

Symptom in the `publish-pypi` job:

```
Trusted publishing exchange failure:
  invalid-publisher: valid token, but no corresponding publisher
  (Publisher with matching claims was not found)
```

The build succeeded; only the upload failed. This is **not a code or workflow
bug** — PyPI has no *trusted publisher* registered whose claims match what the
workflow presents. The workflow presents exactly these four claims:

| Claim          | Value                      | Where it's set                                   |
| -------------- | -------------------------- | ------------------------------------------------ |
| Owner          | `broven`                   | the GitHub repo owner                            |
| Repository     | `browserwright`            | the GitHub repo name                             |
| Workflow file  | `release.yml`              | `.github/workflows/release.yml`                  |
| Environment    | `pypi`                     | `publish-pypi.environment.name` in that workflow |

### Fix (recommended — keep trusted publishing)

As a **PyPI owner/maintainer of the `browserwright` project**:

1. Open <https://pypi.org/manage/project/browserwright/settings/publishing/>.
2. **Add a new GitHub trusted publisher** with the four values from the table
   above (Owner `broven`, Repository `browserwright`, Workflow `release.yml`,
   Environment `pypi`). All four must match **exactly** — a stale entry from a
   previous repo owner/name, or a blank/different Environment, produces this
   exact error.
3. Re-run the failed release (no new tag needed):
   `gh run rerun <run-id> --repo broven/browserwright` — or push the next tag.

> Most common root cause: the project was first published with an API token and
> trusted publishing was never configured, **or** the repo was renamed/moved
> owners and the old trusted-publisher entry no longer matches.

### Fallback (API token instead of OIDC)

If you'd rather not use trusted publishing:

1. Create a scoped PyPI API token for the `browserwright` project.
2. Add it as a repo secret, e.g. `PYPI_API_TOKEN`.
3. In `release.yml`, change the publish step to pass the token and drop the OIDC
   bits:
   ```yaml
   - name: Publish distributions to PyPI
     uses: pypa/gh-action-pypi-publish@release/v1
     with:
       password: ${{ secrets.PYPI_API_TOKEN }}
   # and remove `permissions: id-token: write` + `environment: pypi` from the job
   ```

### While PyPI is broken

The **GitHub Release (extension zip) still publishes fine** — only the PyPI
upload fails. To get the fixed code onto a machine without PyPI, use the
[local-wheel fallback](#fallback-when-pypi-doesnt-have-the-version-yet) below.

---

## Updating the local global install

The global install is two independent pieces:

1. **Python CLI + daemon** — a `uv tool` install (`~/.local/bin/browserwright`,
   `browserwright-daemon`). The GH#18 fix lived entirely here.
2. **Chrome extension** — unpacked files on disk that your daily Chrome loads
   (default `~/Library/Mobile Documents/com~apple~CloudDocs/etc/chrome-extension/browserwright`,
   override with `BROWSERWRIGHT_CHROME_EXTENSION_TARGET`). Not shipped via PyPI;
   it comes from the GitHub Release zip.

### Normal path

```bash
mise run upgrade-global
```

This: `uv tool install browserwright --force --refresh` (CLI+daemon from PyPI) →
downloads the matching `browserwright-extension-<version>.zip` from the GitHub
Release and unpacks it into the extension dir → `browserwright-daemon restart
--force` → `browserwright-daemon extension reload` → `browserwright-daemon
version check --strict-daemon` → `pi update npm:@browserwright/pi` (pi
extension, only when it is installed in pi's user settings).

> **Why `--force` and `--strict-daemon` (issue #57).** A restart kills every
> session's live executor state, so `restart` refuses by default while anyone is
> driving a session; an upgrade is explicit human intent, so it passes `--force`
> and prints what it interrupted rather than killing silently. `--strict-daemon`
> makes the post-check require that the daemon answering `/__status__` is running
> the version just installed. Without it, the check compared the package to the
> extension manifest — which on a `uv tool` install (no manifest beside the venv)
> reduces to "is this string semver", and reported `versions ok` for a whole
> release while a daemon one version behind kept serving.

> **If the upgrade reports a restart failure**, read the message: it distinguishes
> "pid unchanged", "new process died on startup", and "new process is running the
> wrong version", and it quotes the daemon's own stderr. The usual cause is
> another `browserwright-daemon serve` holding port 19989 — commonly one leaked
> by a test run in another worktree (`mise run teardown` in that worktree).

> **Why `--refresh`:** without it, uv's index cache can resolve to the *previous*
> version for a few minutes after a release and silently install the old wheel.
> `--refresh` busts the cache. (Do **not** pin `uv tool install browserwright==X.Y.Z`
> — the pin lands in the receipt and caps every later `upgrade` at that version.)

### Fallback when PyPI doesn't have the version yet

When the PyPI publish failed (see above) but the tag/GitHub Release exist, build
the wheel locally — stamped with the tag version, since `pyproject` stays at the
placeholder — and install it directly:

```bash
VERSION=X.Y.Z

# Build a wheel stamped with the release version, then restore pyproject.
cp pyproject.toml /tmp/pyproject.bak
python3 - "$VERSION" <<'PY'
import re, sys, pathlib
p = pathlib.Path("pyproject.toml")
p.write_text(re.sub(r'(?m)^(version\s*=\s*)"[^"]+"', rf'\g<1>"{sys.argv[1]}"',
                    p.read_text(), count=1))
PY
uv build --wheel
mv /tmp/pyproject.bak pyproject.toml          # keep git's placeholder version

# Replace the global CLI+daemon with the local wheel.
browserwright-daemon stop || true
uv tool install --force "./dist/browserwright-${VERSION}-py3-none-any.whl"

# Update the extension files from the GitHub Release, then reload in Chrome.
EXT_TARGET="${BROWSERWRIGHT_CHROME_EXTENSION_TARGET:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/etc/chrome-extension/browserwright}"
gh release download "v${VERSION}" --repo broven/browserwright \
  --pattern "browserwright-extension-${VERSION}.zip" --dir /tmp/bwext
( cd /tmp/bwext && unzip -qo "browserwright-extension-${VERSION}.zip" )
rsync -a --delete --exclude .DS_Store /tmp/bwext/chrome-extension/ "$EXT_TARGET/"
rm -rf /tmp/bwext
```

Then verify: `browserwright version` shows `X.Y.Z`, and after the extension is
reloaded the connected version matches (see below).

---

## Extension auto-reload

`chrome.runtime.reload()` only re-reads the unpacked dir **from disk** — it never
downloads anything, so it depends on the disk files already being updated (by
`upgrade-global` or the fallback). The reload itself is automatic:

- When an extension connects (`hello`), the daemon compares the extension's
  reported version to its own (`relay._maybe_reload_for_version_drift`). If the
  extension is **older**, the daemon sends `reloadExtension`; the extension runs
  `chrome.runtime.reload()` and comes back on the disk version.
- So you usually **don't** need to click reload in `chrome://extensions` — *if*
  the extension is currently connected to the daemon, and *if* the in-memory
  extension already understands `reloadExtension` (added in **0.6.13**; upgrading
  from **≤0.6.12** needs one manual reload — the chicken-and-egg).
- If the extension isn't connected at all (e.g. it never reconnected after a
  daemon restart), the daemon can't reach it — reload it once manually at
  `chrome://extensions` to kick it.

Confirm the connected extension version (daemon running):

```bash
curl -s http://127.0.0.1:19989/__status__ \
  | python3 -c "import json,sys; print([i.get('browserwright_version') for i in json.load(sys.stdin).get('extension_details',[])])”
```

A non-empty list showing the new version means it's connected and current.

> **Store installs don't reload.** The drift-driven `reloadExtension` is
> skipped when the connected extension is a Chrome Web Store install
> (`install_source=store` in `/__status__`): `chrome.runtime.reload()` can't
> change a store build's version, and the store auto-updates on its own. A
> store extension that lags the daemon resolves when a matching version is
> published (see below); until then `doctor` shows a cosmetic mismatch warning
> and sessions still work.

---

## Chrome Web Store publishing (`publish-cws`)

The `publish-cws` job uploads the extension to the Chrome Web Store on every
pure `vX.Y.Z` tag, using the official Upload API:

1. **Repackage** `chrome-extension/` with `manifest.json` at the zip root (CWS
   rejects the GitHub Release zip, which nests everything under
   `chrome-extension/`).
2. **OAuth2 token exchange** (`client_id` / `client_secret` / `refresh_token`
   → `access_token`). CWS has no OIDC/trusted publishing, so this is the one
   stored-credential channel in this repo's CI.
3. **Upload** `PUT /upload/chromewebstore/v1.1/items/$ITEM_ID` (replaces the
   draft) and **publish** `POST .../publish` (default audience).

Failure policy: a genuine API error (auth, rejected version) **fails the
release** — a half-published release is worse than a loud one. The one
non-fatal case is Google deciding to **re-review** the update (permission or
material code change): the job prints a `::warning::` and the item goes live
when review clears (usually ≤ 3 days; store users then auto-update).

### Post-release store check (agent ritual)

The agent that cut the release verifies the store outcome explicitly after
every tag, because a blocked store publish is easy to miss inside an
otherwise-green run:

1. `gh run view <id>` — `Publish Chrome extension to Web Store` must be
   **success**;
2. success → grep its log for which branch ran: `skipping CWS publish`
   (extension unchanged, expected) vs `Chrome Web Store updated to ...
   (publish accepted)` (published);
3. failure → grep `--log-failed` for `ITEM_NOT_UPDATABLE`: the item is
   locked while a previous version is under review. Report to the user as
   "store blocked — the earlier version publishes when review clears
   (usually ≤ 3 days); this version needs a re-run/tag after that; do NOT
   unpublish to unlock". Any other failure is a real bug in the job.

The store can therefore lag releases by one review cycle; the daemon's
`install_source` / drift reporting keeps that visible locally.

### CWS secrets setup (one-time)

These four GitHub secrets (repo settings → Secrets → Actions) are the only
stored credentials in this repo's CI. Only tag pushers can trigger the job, so
whoever can push `main` effectively holds the store publish key — keep that
set small.

| Secret | Value |
|---|---|
| `CWS_ITEM_ID` | `okgnalaalckoaeledbjhpjiccmcdceeb` (the store item id) |
| `CWS_CLIENT_ID` | Google Cloud OAuth client id |
| `CWS_CLIENT_SECRET` | Google Cloud OAuth client secret |
| `CWS_REFRESH_TOKEN` | OAuth refresh token (see below) |

Generating the OAuth pair (official path, [Web Store API docs](https://developer.chrome.com/docs/webstore/using_webstore_api)):

1. [Google Cloud console](https://console.cloud.google.com) → create/select a
   project → enable the **Chrome Web Store API**.
2. **OAuth consent screen** → External → fill app info; add your email as a
   **test user**.
3. **Credentials** → Create credentials → **OAuth client ID** → app type
   **Desktop app** → note `client_id` / `client_secret`.
4. In a browser (logged into the CWS developer account — **2FA must be on**),
   authorize and grab the code:

   ```
   https://accounts.google.com/o/oauth2/auth?response_type=code&scope=https://www.googleapis.com/auth/chromewebstore&client_id=$CLIENT_ID&redirect_uri=urn:ietf:wg:oauth:2.0:oob
   ```

5. Exchange the code for a refresh token:

   ```bash
   curl "https://oauth2.googleapis.com/token" -d \
     "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&code=$CODE&grant_type=authorization_code&redirect_uri=urn:ietf:wg:oauth:2.0:oob"
   ```

   Store the `refresh_token` from the response as `CWS_REFRESH_TOKEN`. It
   authorizes store publishes until revoked.

### Store & pre-releases

The store item has exactly **one version slot** and CWS enforces two rules:
versions are numeric only (no `-rc1`), and every upload must be **strictly
greater** than the currently published version. Publishing an rc with the
final's number (e.g. `0.13.0` for both `v0.13.0-rc1` and `v0.13.0`) therefore
collides, and giving rcs a different number breaks the repo's version-parity
discipline (extension version == daemon version, checked by `doctor`). So:

- **`vX.Y.Z-rc*` tags skip CWS entirely.** They still auto-publish to PyPI,
  npm, and the GitHub Release (all of which accept `-rc1` suffixes). Pre-release
  testing happens via the unpacked build.
- **Pure `vX.Y.Z` tags publish to the store** (default audience), but only when
  the release actually changed `chrome-extension/` — the job diffs it against
  the previous release tag and skips (with a clear message) when the extension
  source is byte-identical, since uploading it would just churn a review round
  for a version-number change. Backend-only / skill-only releases therefore
  never touch the store; store users keep the last published extension and
  `doctor`'s version-mismatch note stays honest until the next extension
  change. Store users auto-update once the version lands; the daemon's
  `install_source` reporting keeps the mismatch warning honest in the meantime.
- **Trusted-testers distribution stays manual.** When a real beta audience
  exists, push a build to `publishTarget=trustedTesters` (or publish a separate
  private BETA item per the [official beta flow](https://developer.chrome.com/docs/extensions/develop/migrate/publish-mv3));
  do not automate it into the tag pipeline — the same-version collision above
  makes it stateful.
