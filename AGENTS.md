> Single source of truth for all code agents working in this repo.
> `CLAUDE.md` / `.cursorrules` just redirect here.

## Run it (clone → install → test)

This repo is harnessed with [mise](https://mise.jdx.dev). After cloning:

```bash
mise trust && mise install   # pin & install python + uv
mise run install             # uv sync --extra ux (deps + dev group)
mise run test                # fast gate: daemon + skill + skill evals
```

See **[ONBOARD.md](ONBOARD.md)** for the full local dev loop, **[ONBOARDING.md](ONBOARDING.md)**
for architecture orientation, **[TESTING.md](TESTING.md)** for the test-suite map, and
**[RELEASING.md](RELEASING.md)** for cutting a release, fixing the PyPI publish, and updating the global install.
`mise tasks` lists every verb (install / dev / test[:daemon|:skill|:evals|:e2e] / lint / build / dev-link / upgrade-global / …).

## Project Architecture Reference

Before changing session routing, backend semantics, tab creation, Playwright
facade behavior, or teardown semantics, read `docs/session-workspaces.md`.

## Extension Backend E2E Testing

When testing extension-backend behavior, prefer the existing real-Chrome E2E
harness instead of asking the user to install or reload the extension in their
daily Chrome. `tests/daemon/e2e/conftest.py` and
`tests/daemon/e2e/_real_browser.py` start an isolated daemon, launch Chrome for
Testing with a patched unpacked `chrome-extension/`, and wait for the extension
to connect. Use the `e2e_daemon`, `e2e_chrome`, `patched_ext_dir`, and
`ext_ready` fixtures for full extension-backend verification. If Chrome for
Testing is missing, install it with:

```bash
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
```
