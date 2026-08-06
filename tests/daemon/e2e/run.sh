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

# Compute THIS worktree's e2e port block (issue #44 A). The ports are derived
# from the worktree root path — not fixed literals — so sibling worktrees can
# run e2e concurrently on one machine. Single source of truth:
# tests/daemon/e2e/_e2e_ports.py; the pytest fixtures import the same module,
# so the runner and the fixtures can never disagree about which ports this
# worktree owns.
eval "$(uv run python "$ROOT/tests/daemon/e2e/_e2e_ports.py")"

# Free stale test daemons / orphan Chrome left by a previous interrupted run —
# otherwise the session fixture fails with "port already in use".
#
# WORKTREE-SCOPED, deliberately. The port block is derived from THIS worktree's
# root, so an unconditional `lsof -ti :PORT | xargs kill` would still reach
# straight into a *sibling worktree's live run* if the hashes collided (or if
# the sibling is on an older checkout still using the fixed 29989 block) and
# kill its daemon mid-suite — both results silently void. That has actually
# happened here. `mise run teardown` already documents the correct posture
# ("reclaims **this worktree's** leaked e2e daemons … deliberately never
# touches … a sibling worktree"); this is the same rule, applied to every port
# in the block.
#
# Ownership test, same marker `mise run teardown` uses plus a cwd fallback:
#   - argv contains "$ROOT/.venv"  → spawned from this worktree's interpreter
#     (the global daemon and every sibling worktree resolve to a different
#     absolute path, so this is exact and collision-free); or
#   - the process cwd is inside $ROOT (covers a daemon started some other way).
# Anything else is somebody else's — we REFUSE and exit instead of killing.
# Killing the wrong daemon is far more damaging than not killing at all: it
# corrupts a run nobody is watching, whereas refusing fails loudly right here.
E2E_PORTS="$TEST_EXT_PORT $TEST_RDP_PORT $TEST_FACADE_L1_PORT \
$TEST_FACADE_L1_EXT_PORT $TEST_FACADE_EXT_PORT $TEST_FACADE_RDP_PORT \
$TEST_FACADE_AUTOFACADE_PORT"
mine=""; theirs=""
for port in $E2E_PORTS; do
  if leftover=$(lsof -ti :$port 2>/dev/null); then
    for pid in $leftover; do
      cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
      cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true)
      case "$cmd" in
        *"$ROOT/.venv"*) mine="$mine $pid"; continue ;;
      esac
      case "$cwd" in
        "$ROOT"|"$ROOT"/*) mine="$mine $pid" ;;
        *) theirs="$theirs $pid (port $port, cwd=${cwd:-?})" ;;
      esac
    done
  fi
done
if [ -n "$theirs" ]; then
  echo "run.sh: REFUSING to start — this worktree's e2e ports are held by" >&2
  echo "  processes NOT from this worktree ($ROOT):$theirs" >&2
  echo "  The ports are derived from this worktree's path (issue #44), so a" >&2
  echo "  foreign holder is most likely a sibling worktree's e2e run in" >&2
  echo "  progress (or a leftover from an older checkout's fixed 29989 block)." >&2
  echo "  Killing it would void both runs. Wait for it, or (if you are sure" >&2
  echo "  it is dead weight) kill it by hand." >&2
  exit 1
fi
if [ -n "$mine" ]; then
  echo "run.sh: killing this worktree's stale test daemon(s) on ports $E2E_PORTS ($mine)" >&2
  # shellcheck disable=SC2086
  kill $mine 2>/dev/null || true
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
