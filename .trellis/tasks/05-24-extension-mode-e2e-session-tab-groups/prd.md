# Extension Mode E2E Session Tab Groups

## Goal

Add real-Chrome extension-mode E2E coverage for Browserwright session tab group behavior.

## Requirements

- Creating/using an extension-backed session with a supplied session name creates a real Chrome tab group titled with that name.
- Three extension-backed sessions can run at the same time through the same extension/daemon/Chrome process.
- Each session is isolated:
  - each session lands in its own Chrome tab group,
  - each session can operate only its own page content,
  - `list_tabs()` / `Target.getTargets` only returns pages from that session's tab group.

## Verification

- Add an E2E test under `tests/daemon/e2e/`.
- Run the new test path against the real Chrome extension harness.
