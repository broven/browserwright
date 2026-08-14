"""Guards for the tag-is-the-source-of-truth release contract.

`pyproject.toml`, `chrome-extension/manifest.json` and
`pi-extension/package.json` are never bumped per release:
`.github/workflows/release.yml` rewrites all three from the pushed git tag at
build time (see RELEASING.md). That keeps the tag and every shipped artifact in
lockstep with no human bump step — but only as long as the stamping steps can
still find what they rewrite.

The failure mode this file exists to prevent is expensive and late: an innocuous
edit to `pyproject.toml` (a trailing comment on the version line, a second
top-level `version =`, a switch to `dynamic = ["version"]`) breaks the stamp,
and nothing notices until CI aborts *after* the tag has already been pushed —
at which point the tag has to be deleted and re-cut. These tests replay the
workflow's own stamping code against the repo's current files, so the breakage
surfaces in the fast gate instead.
"""
from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MANIFEST = REPO_ROOT / "chrome-extension" / "manifest.json"
PI_PACKAGE = REPO_ROOT / "pi-extension" / "package.json"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# The in-repo value both files carry between releases. Deliberately *not* a
# plausible release number: a stale real-looking version (this sat at "0.6.2"
# while tags had reached v0.8.0) reads as a version-drift bug to everyone who
# meets it, and a locally built wheel inherits it and looks installable.
PLACEHOLDER_VERSION = "0.0.0"


def _stamping_pattern() -> re.Pattern[str]:
    """Return the regex `release.yml` uses to rewrite `pyproject.toml`.

    Extracted from the workflow rather than duplicated here on purpose: the
    point of the test is to prove that *the regex CI will actually run* still
    matches this repo, so a change to the workflow has to keep passing without
    anyone remembering to update a copy.
    """
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"pattern = re\.compile\((r'[^\n]*?')\)", text)
    if match is None:
        pytest.fail(
            "Could not find the `pattern = re.compile(r'...')` version-stamping "
            "line in .github/workflows/release.yml. If the stamping step was "
            "rewritten, update this helper so the guard keeps tracking it — do "
            "not delete the guard."
        )
    return re.compile(ast.literal_eval(match.group(1)))


def test_pyproject_and_manifest_carry_the_placeholder():
    pyproject_version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    manifest_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    pi_version = json.loads(PI_PACKAGE.read_text(encoding="utf-8"))["version"]

    assert pyproject_version == PLACEHOLDER_VERSION, (
        f"pyproject version is {pyproject_version!r}, expected the placeholder "
        f"{PLACEHOLDER_VERSION!r}. Releases are cut by pushing a `vX.Y.Z` tag; "
        "CI stamps the version from that tag. Do not bump this by hand."
    )
    assert manifest_version == PLACEHOLDER_VERSION, (
        f"chrome-extension/manifest.json version is {manifest_version!r}, "
        f"expected the placeholder {PLACEHOLDER_VERSION!r}."
    )
    assert pi_version == PLACEHOLDER_VERSION, (
        f"pi-extension/package.json version is {pi_version!r}, expected the "
        f"placeholder {PLACEHOLDER_VERSION!r}."
    )
    # version.check_versions() reports `extension-version-mismatch` when the
    # first two disagree, so drift between the placeholders breaks
    # `browserwright version check` in every checkout. The npm package is not
    # part of that runtime check, but it ships from the same tag, so a drifted
    # placeholder there publishes an @browserwright/pi that claims a version the
    # CLI it shells out to never had.
    assert pyproject_version == manifest_version == pi_version


def test_release_workflow_can_stamp_pyproject():
    """Replay the workflow's pyproject rewrite and check the result parses."""
    pattern = _stamping_pattern()
    text = PYPROJECT.read_text(encoding="utf-8")

    assert len(pattern.findall(text)) == 1, (
        "The release workflow rewrites the *first* line matching "
        f"{pattern.pattern!r} and aborts unless it matches exactly once. "
        "pyproject.toml now has a different number of matches — a second "
        "top-level `version = \"...\"`, a trailing comment on the project "
        "version line, or a switch to `dynamic = [\"version\"]` will all break "
        "the release after the tag is pushed."
    )

    stamped, count = pattern.subn(lambda m: f'{m.group(1)}"9.9.9"', text, count=1)
    assert count == 1
    assert tomllib.loads(stamped)["project"]["version"] == "9.9.9", (
        "The stamping regex matched a line that is not project.version."
    )


def test_release_workflow_can_stamp_extension_manifest():
    """The manifest stamp is `json.loads` -> set `version` -> dump."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "version" in manifest
    manifest["version"] = "9.9.9"
    round_tripped = json.loads(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    assert round_tripped["version"] == "9.9.9"
    assert round_tripped["manifest_version"] == 3, (
        "Stamping must not disturb the rest of the manifest."
    )


def test_release_workflow_can_stamp_pi_package():
    """The npm stamp is `json.loads` -> set `version` -> dump, like the manifest."""
    package = json.loads(PI_PACKAGE.read_text(encoding="utf-8"))
    assert "version" in package
    package["version"] = "9.9.9"
    round_tripped = json.loads(json.dumps(package, indent=2, ensure_ascii=False) + "\n")
    assert round_tripped["version"] == "9.9.9"
    # pi discovers an installed package's entry points through this key. Losing
    # it in the stamp publishes a package that installs and then does nothing.
    assert round_tripped["pi"]["extensions"] == ["./index.ts"], (
        "Stamping must not disturb the pi manifest key."
    )


def test_pi_package_never_bundles_the_host_packages():
    """pi injects its own typebox and pi-* modules into the extension loader.

    Declaring them as real dependencies installs a second copy, and schema
    identity then fails across the module boundary — a failure that shows up as
    a tool whose parameters never validate, not as an import error.
    """
    package = json.loads(PI_PACKAGE.read_text(encoding="utf-8"))
    host_packages = {"typebox", "@earendil-works/pi-coding-agent"}
    for field in ("dependencies", "bundledDependencies"):
        declared = set(package.get(field) or [])
        assert not (declared & host_packages), (
            f"{field} must not contain {declared & host_packages}; they belong in "
            "peerDependencies with a \"*\" range."
        )
    assert host_packages <= set(package.get("peerDependencies", {}))


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v0.8.1", "0.8.1"),
        ("v1.0.0", "1.0.0"),
        ("0.8.1", "0.8.1"),
    ],
)
def test_release_workflow_accepts_normal_release_tags(tag, expected):
    pattern = _tag_pattern()
    assert pattern.fullmatch(tag) is not None, f"{tag!r} should be a valid release tag"
    assert (tag[1:] if tag.startswith("v") else tag) == expected


@pytest.mark.parametrize("tag", ["v0.8", "vnext", "release-0.8.1", "v01.2.3"])
def test_release_workflow_rejects_malformed_tags(tag):
    assert _tag_pattern().fullmatch(tag) is None, (
        f"{tag!r} must be rejected by the workflow's tag check — otherwise CI "
        "would stamp a nonsense version into the published package."
    )


def _tag_pattern() -> re.Pattern[str]:
    """Return the tag-validation regex from `release.yml`.

    All four jobs (PyPI, extension zip, npm, CWS) embed their own copy of this
    literal and refuse to build when the pushed tag does not match — that is
    what stops a `v*` tag like `vnext` from being stamped in as a garbage
    version. The copies must agree, or one job could publish while another
    rejects the same tag, so this asserts they do.
    """
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    sources = [
        "".join(re.findall(r'r"([^"]*)"', block))
        for block in re.findall(r"re\.fullmatch\(\s*((?:r\"[^\"]*\"\s*)+),\s*tag", text)
    ]
    if not sources:
        pytest.fail(
            "Could not find the tag-validation regex in "
            ".github/workflows/release.yml; update this helper if the workflow "
            "was restructured — do not delete the guard."
        )
    assert len(sources) == 4, (
        f"Expected four tag-validating release jobs (PyPI, extension zip, npm, "
        f"CWS), found {len(sources)}. A job that publishes without validating "
        "the tag can stamp a garbage version; a job that disappeared means an "
        "artifact silently stopped shipping."
    )
    assert len(set(sources)) == 1, (
        "The release jobs validate the tag with different regexes "
        f"({sources}); they must accept exactly the same tags or a release can "
        "publish a wheel with no matching extension zip or npm package."
    )
    return re.compile(sources[0])
