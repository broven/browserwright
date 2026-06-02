# Update mise global install to released artifacts

## Goal

Update the global install mise task so it no longer installs the Chrome
extension from the local checkout. The Python package should continue to come
from PyPI, and the extension should come from the GitHub Release asset generated
by the release workflow for the installed version.

## Requirements

- `mise run upgrade-global` installs or refreshes `browserwright` from PyPI.
- The task derives the installed CLI version and downloads
  `browserwright-extension-<version>.zip` from the matching `v<version>` GitHub
  Release.
- The unpacked extension is synced into the stable local load path.
- The task keeps reload guidance based on whether the extension target changed.
- README guidance should describe the PyPI + GitHub Release artifact flow.

## Non-Goals

- Do not change the release workflow in this task.
- Do not remove the local development `dev-link` task.
- Do not implement `setuptools-scm` in this task.
