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

- The repo's `pyproject.toml` `version` and `chrome-extension/manifest.json`
  `version` are intentionally **NOT bumped per release** — they stay at the
  placeholder **`0.0.0`** in git. CI overwrites both **from the tag** at build
  time. So the tag is the single source of truth for the version; do not edit
  `pyproject` `version` in a release commit.
- The placeholder is `0.0.0` so it can never be mistaken for a real release. It
  previously sat at `0.6.2` — a version that *had* shipped — which made every
  checkout look like it was eight releases behind (`browserwright version` said
  `0.6.2` while tags were at `v0.8.0`) and made a locally built wheel look
  installable when it was really an unstamped dev build.
- `tests/skill/test_release_versioning.py` (in the fast gate) replays the
  workflow's own stamping code against the current `pyproject.toml` /
  `manifest.json` and fails if CI could no longer stamp them — a trailing
  comment on the `version` line, a second top-level `version =`, a switch to
  `dynamic = ["version"]`, drift between the two placeholders, or the two jobs'
  tag regexes disagreeing. Without it those break the release **after** the tag
  is pushed, when the only fix is deleting and re-cutting the tag.
- Job `publish-pypi` builds the sdist+wheel and uploads to **PyPI** via OIDC
  **trusted publishing** (no stored token), gated on the GitHub `pypi`
  environment.
- Job `publish-extension` stamps the manifest, zips `chrome-extension/` into
  `browserwright-extension-<version>.zip`, and attaches it to a **GitHub
  Release** named `vX.Y.Z`.

Pick the next version by bumping the latest tag (`git tag --sort=-v:refname |
head`). Patch for fixes, minor for features.

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
version check --strict-daemon`.

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
  | python3 -c "import json,sys; print([i.get('browserwright_version') for i in json.load(sys.stdin).get('extension_details',[])])"
```

A non-empty list showing the new version means it's connected and current.
