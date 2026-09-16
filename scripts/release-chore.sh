#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "release chore must run inside a git repository" >&2
  exit 1
}
cd "$repo_root"

scripts/bump-version.sh patch

tags_at_head="$(git tag --points-at HEAD --sort=-v:refname)"
release_tag=""
while IFS= read -r tag; do
  if [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    release_tag="$tag"
    break
  fi
done <<< "$tags_at_head"

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

echo "PyPI is ready; updating the global installation..."
mise run upgrade-global
