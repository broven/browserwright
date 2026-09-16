#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "release chore must run inside a git repository" >&2
  exit 1
}
cd "$repo_root"

force="${1:-false}"
if [[ "$force" != "true" && "$force" != "false" ]]; then
  echo "usage: $0 [true|false]" >&2
  exit 2
fi

tags_at_head="$(git tag --points-at HEAD --sort=-v:refname)"
release_tag=""
while IFS= read -r tag; do
  if [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    release_tag="$tag"
    break
  fi
done <<< "$tags_at_head"

if [[ -z "$release_tag" ]]; then
  scripts/bump-version.sh patch
  tags_at_head="$(git tag --points-at HEAD --sort=-v:refname)"
  while IFS= read -r tag; do
    if [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
      release_tag="$tag"
      break
    fi
  done <<< "$tags_at_head"
else
  echo "Resuming release $release_tag already tagged at HEAD."
fi

if [[ -z "$release_tag" ]]; then
  echo "could not find the newly-created release tag at HEAD" >&2
  exit 1
fi

version="${release_tag#v}"
echo "Pushing $release_tag to origin to trigger the release workflow..."
git push origin "$release_tag"

timeout_seconds="${BROWSERWRIGHT_PYPI_WAIT_TIMEOUT:-1800}"
poll_interval="${BROWSERWRIGHT_PYPI_POLL_INTERVAL:-15}"
pypi_url="${BROWSERWRIGHT_PYPI_URL:-https://pypi.org/pypi/browserwright/json}"

python3 - "$version" "$timeout_seconds" "$poll_interval" "$pypi_url" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

expected, timeout, interval, url = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
deadline = time.monotonic() + timeout

print(f"Waiting for browserwright {expected} on PyPI (timeout {int(timeout)}s)...")
while True:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "browserwright-release-chore"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if payload.get("releases", {}).get(expected):
            print(f"PyPI now has browserwright {expected}.")
            break
        current = payload.get("info", {}).get("version", "unknown")
        print(f"PyPI does not have {expected} yet (currently {current}); retrying...")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"Could not check PyPI ({exc}); retrying...")

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit(f"Timed out waiting for browserwright {expected} on PyPI")
    time.sleep(min(interval, remaining))
PY

production_tmpdir="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || true)"
if [[ -z "$production_tmpdir" ]]; then
  production_tmpdir="/tmp"
fi

global_cmd() {
  env -u BW_DAEMON_URL -u BD_CONFIG \
    -u XDG_RUNTIME_DIR -u TMPDIR -u BS_HOME \
    -u BD_EXTENSION_PORT -u BD_FACADE_PORT -u BD_CDP_PORT \
    -u BD_FACADE_HOST -u BROWSERWRIGHT_DEV -u BROWSERWRIGHT_DEV_ROOT \
    -u BROWSERWRIGHT_DEV_EXT_PORT -u BROWSERWRIGHT_DEV_FACADE_PORT \
    -u BROWSERWRIGHT_DEV_CDP_PORT \
    TMPDIR="$production_tmpdir" \
    "$@"
}

activity_cli="$repo_root/.venv/bin/browserwright-daemon"
if [[ ! -x "$activity_cli" ]]; then
  activity_cli="$(command -v browserwright-daemon || true)"
fi
if [[ -z "$activity_cli" ]]; then
  echo "release chore: cannot check global daemon activity; run mise run install first." >&2
  exit 3
fi

activity_timeout="${BROWSERWRIGHT_ACTIVITY_WAIT_TIMEOUT:-1800}"
activity_interval="${BROWSERWRIGHT_ACTIVITY_POLL_INTERVAL:-15}"
if [[ "$force" == "true" ]]; then
  echo "Force requested; skipping the global daemon idle wait."
else
  activity_deadline=$(( $(date +%s) + activity_timeout ))
  echo "Waiting for the global daemon to become idle (timeout ${activity_timeout}s)..."
  while true; do
    activity_report=""
    activity_rc=0
    activity_report="$(global_cmd "$activity_cli" activity 2>&1)" || activity_rc=$?
    if [[ $activity_rc -eq 0 ]]; then
      echo "Global daemon is idle."
      break
    fi
    if [[ $activity_rc -ne 4 ]]; then
      echo "release chore: could not check global daemon activity:" >&2
      echo "$activity_report" | sed 's/^/    /' >&2
      exit "$activity_rc"
    fi
    echo "Global daemon is busy; waiting ${activity_interval}s..."
    echo "$activity_report" | sed 's/^/    /'
    if [[ "$(date +%s)" -ge "$activity_deadline" ]]; then
      echo "Timed out waiting for the global daemon to become idle." >&2
      exit 4
    fi
    sleep "$activity_interval"
  done
fi

upgrade_args=()
if [[ "$force" == "true" ]]; then
  upgrade_args+=(--force)
fi

echo "PyPI is ready; updating the global installation..."
mise run upgrade-global "${upgrade_args[@]}"
