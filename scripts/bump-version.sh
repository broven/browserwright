#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ( "$1" != "major" && "$1" != "minor" && "$1" != "patch" ) ]]; then
  echo "usage: $0 major|minor|patch" >&2
  exit 2
fi

bump="$1"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "version bump must run inside a git repository" >&2
  exit 1
}
cd "$repo_root"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "version bump requires a clean working tree; commit or stash changes first" >&2
  exit 1
fi

tags="$(git tag --sort=-v:refname --list 'v*')"
latest_tag=""
while IFS= read -r tag; do
  if [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    latest_tag="$tag"
    break
  fi
done <<< "$tags"

if [[ -z "$latest_tag" ]]; then
  echo "could not find a stable vX.Y.Z release tag" >&2
  exit 1
fi

if ! git merge-base --is-ancestor "$latest_tag" HEAD; then
  echo "HEAD is not based on the latest release tag $latest_tag" >&2
  exit 1
fi

version="${latest_tag#v}"
IFS=. read -r major minor patch <<< "$version"
case "$bump" in
  major)
    major=$((major + 1))
    minor=0
    patch=0
    ;;
  patch)
    patch=$((patch + 1))
    ;;
  minor)
    minor=$((minor + 1))
    patch=0
    ;;
esac

next_version="$major.$minor.$patch"
next_tag="v$next_version"
if git show-ref --tags --verify --quiet "refs/tags/$next_tag"; then
  echo "tag $next_tag already exists" >&2
  exit 1
fi

git tag -a "$next_tag" -m "browserwright $next_version"
printf 'Created %s at %s. Push it with: git push origin %s\n' \
  "$next_tag" "$(git rev-parse --short HEAD)" "$next_tag"
