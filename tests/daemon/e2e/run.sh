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
ROOT="$PWD"

# Free a stale test daemon left by a previous interrupted run (port 29989) —
# otherwise the session fixture fails with "port already in use".
#
# WORKTREE-SCOPED, deliberately. :29989 is a fixed port shared by every
# worktree's e2e run, so an unconditional `lsof -ti :29989 | xargs kill` reaches
# straight into a *sibling worktree's live run* and kills its daemon mid-suite —
# both results silently void. That has actually happened here. `mise run
# teardown` already documents the correct posture ("reclaims **this worktree's**
# leaked e2e daemons … deliberately never touches … a sibling worktree"); this
# is the same rule, applied to the same resource.
#
# Ownership test, same marker `mise run teardown` uses plus a cwd fallback:
#   - argv contains "$ROOT/.venv"  → spawned from this worktree's interpreter
#     (the global daemon and every sibling worktree resolve to a different
#     absolute path, so this is exact and collision-free); or
#   - the process cwd is inside $ROOT (covers a daemon started some other way).
# Anything else is somebody else's — we REFUSE and exit instead of killing.
# Killing the wrong daemon is far more damaging than not killing at all: it
# corrupts a run nobody is watching, whereas refusing fails loudly right here.
if leftover=$(lsof -ti :29989 2>/dev/null); then
  mine=""; theirs=""
  for pid in $leftover; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true)
    case "$cmd" in
      *"$ROOT/.venv"*) mine="$mine $pid"; continue ;;
    esac
    case "$cwd" in
      "$ROOT"|"$ROOT"/*) mine="$mine $pid" ;;
      *) theirs="$theirs $pid (cwd=${cwd:-?})" ;;
    esac
  done
  if [ -n "$theirs" ]; then
    echo "run.sh: REFUSING to start — port 29989 is held by a process that is" >&2
    echo "  NOT from this worktree ($ROOT):$theirs" >&2
    echo "  That is most likely a sibling worktree's e2e run in progress." >&2
    echo "  Killing it would void both runs. Wait for it, or (if you are sure" >&2
    echo "  it is dead weight) kill it by hand." >&2
    exit 1
  fi
  if [ -n "$mine" ]; then
    echo "run.sh: killing this worktree's stale test daemon on :29989 ($mine)" >&2
    # shellcheck disable=SC2086
    kill $mine 2>/dev/null || true
    sleep 1
  fi
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
