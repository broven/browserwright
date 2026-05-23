#!/usr/bin/env bash
# Run the real-Chrome E2E suite against THIS worktree's code, from any git
# worktree, with no setup.
#
# browserwright is now a single package (the agent-facing layer + the bundled
# daemon under browserwright.daemon), so a plain `uv run` against the repo-root
# project resolves the current worktree's code for both halves — no more
# `--with ../sibling` layering across separate uv projects.
#
# Usage:
#   tests/daemon/e2e/run.sh                                   # whole e2e suite
#   tests/daemon/e2e/run.sh -v                                # pass pytest flags through
#   tests/daemon/e2e/run.sh tests/daemon/e2e/test_l2_recovery.py -v   # target a file
#
# Prereq: Chrome for Testing (see tests/daemon/e2e/README.md).
set -euo pipefail
cd "$(dirname "$0")/../../.."   # -> the repo root (browserwright project root)

# Free a stale test daemon left by a previous interrupted run (port 29989) —
# otherwise the session fixture fails with "port already in use".
if leftover=$(lsof -ti :29989 2>/dev/null); then
  echo "run.sh: killing stale test daemon on :29989 ($leftover)" >&2
  echo "$leftover" | xargs kill 2>/dev/null || true
  sleep 1
fi

# Default the target to the e2e dir when the caller passed only flags (or
# nothing); pass an explicit path through untouched. Pointing at tests/daemon/e2e
# also opts in to the real_chrome tests (see e2e conftest).
has_path=0
for a in "$@"; do case "$a" in -*) ;; *) has_path=1 ;; esac; done
if [ "$has_path" -eq 0 ]; then
  set -- tests/daemon/e2e/ "$@"
fi

# Test deps live in the `dev` dependency-group (PEP 735), which uv installs by
# default — no `--extra test` (that extra doesn't exist).
exec uv run python -m pytest "$@"
