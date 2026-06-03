"""Version metadata shared by the CLI, daemon, extension, and skill shell."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path


PACKAGE_NAME = "browserwright"
EXTENSION_PROTOCOL_VERSION = "1"
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def _is_browserwright_pyproject(path: Path) -> bool:
    """Return True when `path` is a pyproject.toml owned by the browserwright project.

    Walking up from `__file__` toward a `pyproject.toml` is ambiguous: a user
    who runs `pip install browserwright` inside another project's virtualenv
    would otherwise resolve that unrelated project's root as our "repo root"
    and start hunting for `chrome-extension/manifest.json` there. Anchoring on
    `project.name == "browserwright"` keeps `_repo_root()` honest.
    """
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return data.get("project", {}).get("name") == PACKAGE_NAME
    except Exception:
        return False


def _repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file() and _is_browserwright_pyproject(candidate):
            return parent
    return None


def _version_from_pyproject() -> str | None:
    root = _repo_root()
    if root is None:
        return None
    try:
        import tomllib

        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = data.get("project", {}).get("version")
        return str(version) if version else None
    except Exception:
        return None


def package_version() -> str:
    """Return the installed package version, falling back to local pyproject."""
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return _version_from_pyproject() or "0.0.0+unknown"


__version__ = package_version()


def is_semver(value: str) -> bool:
    return bool(_SEMVER_RE.match(value))


class VersionDrift(str, Enum):
    EQUAL = "equal"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VersionComparison:
    drift: VersionDrift
    left: str
    right: str
    order: int | None

    @property
    def compatible(self) -> bool:
        return self.drift in {
            VersionDrift.EQUAL,
            VersionDrift.PATCH,
            VersionDrift.MINOR,
        }


def _semver_core(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def compare_versions(left: str, right: str) -> VersionComparison:
    """Compare two semver strings at the app-version drift level.

    Pre-release/build metadata is accepted for validity but ignored for drift
    classification. The daemon uses extensionProtocolVersion, not app semver,
    as the hard protocol compatibility boundary.
    """
    left = str(left or "")
    right = str(right or "")
    a = _semver_core(left)
    b = _semver_core(right)
    if a is None or b is None:
        return VersionComparison(VersionDrift.UNKNOWN, left, right, None)
    order = (a > b) - (a < b)
    if a == b:
        drift = VersionDrift.EQUAL
    elif a[0] != b[0]:
        drift = VersionDrift.MAJOR
    elif a[1] != b[1]:
        drift = VersionDrift.MINOR
    else:
        drift = VersionDrift.PATCH
    return VersionComparison(drift, left, right, order)


def extension_manifest_path() -> Path | None:
    root = _repo_root()
    if root is not None:
        path = root / "chrome-extension" / "manifest.json"
        if path.is_file():
            return path
    # Installed local releases keep chrome-extension beside the release .venv.
    for parent in Path(__file__).resolve().parents:
        path = parent / "chrome-extension" / "manifest.json"
        if path.is_file():
            return path
    return None


def extension_version() -> str | None:
    path = extension_manifest_path()
    if path is None:
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class VersionIssue:
    code: str
    message: str


def check_versions() -> list[VersionIssue]:
    """Return version consistency issues across the repo-distributed pieces.

    `chrome-extension/` is intentionally NOT part of the PyPI packaging
    contract — the extension ships separately via the Chrome Web Store. So a
    missing manifest is only a problem when we are running from a browserwright
    repo checkout (or a legacy local release that copied the extension beside
    the venv). For a PyPI/`uv tool` install, the absence of the manifest is
    expected; only the version-mismatch case still applies.
    """
    issues: list[VersionIssue] = []
    pkg = package_version()
    ext = extension_version()
    if not is_semver(pkg):
        issues.append(VersionIssue("package-not-semver", f"package version is not semver: {pkg}"))
    if ext is None:
        # Only flag a missing manifest when we can identify a browserwright
        # repo checkout — that's the only context where the extension is
        # expected to live next to the package.
        if _repo_root() is not None:
            issues.append(VersionIssue("extension-missing", "chrome-extension/manifest.json was not found"))
    elif not is_semver(ext):
        issues.append(VersionIssue("extension-not-semver", f"extension version is not semver: {ext}"))
    elif ext != pkg:
        issues.append(
            VersionIssue(
                "extension-version-mismatch",
                f"extension version {ext} does not match package version {pkg}",
            )
        )
    return issues


def version_info() -> dict:
    issues = check_versions()
    return {
        "schema_version": 1,
        "package": PACKAGE_NAME,
        "version": package_version(),
        "extension_version": extension_version(),
        "extension_protocol_version": EXTENSION_PROTOCOL_VERSION,
        "ok": not issues,
        "issues": [issue.__dict__ for issue in issues],
    }
