#!/usr/bin/env bash
# Run the real-Chrome E2E suite against THIS worktree's code — both
# browser-daemon AND browser-skill — from any git worktree, with no setup.
#
# Why this exists: the harness drives browser-skill via `shutil.which` and
# spawns the daemon via `sys.executable`, so BOTH packages must resolve to the
# current worktree. But the installed scripts / project .venvs point at the
# MAIN checkout, and the two packages are separate uv projects (no workspace),
# so plain `uv run pytest tests/e2e/` would test worktree-daemon + stale-skill.
# We layer the sibling worktree's browser-skill into the daemon's uv env with
# `--with ../browser-skill`. All paths are relative, so this Just Works in any
# worktree without edits.
#
# Usage:
#   tests/e2e/run.sh                              # whole e2e suite
#   tests/e2e/run.sh -v                           # pass pytest flags through
#   tests/e2e/run.sh tests/e2e/test_l2_recovery.py -v   # target a file
#
# Prereq: Chrome for Testing (see tests/e2e/README.md).
set -euo pipefail
cd "$(dirname "$0")/../.."   # -> the browser-daemon package dir

# Free a stale test daemon left by a previous interrupted run (port 29989) —
# otherwise the session fixture fails with "port already in use".
if leftover=$(lsof -ti :29989 2>/dev/null); then
  echo "run.sh: killing stale test daemon on :29989 ($leftover)" >&2
  echo "$leftover" | xargs kill 2>/dev/null || true
  sleep 1
fi

# Default the target to the e2e dir when the caller passed only flags (or
# nothing); pass an explicit path through untouched.
has_path=0
for a in "$@"; do case "$a" in -*) ;; *) has_path=1 ;; esac; done
if [ "$has_path" -eq 0 ]; then
  set -- tests/e2e/ "$@"
fi

exec uv run --with ../browser-skill --extra test python -m pytest "$@"
