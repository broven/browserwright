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

<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->
