"""Version metadata shared by the CLI, daemon, extension, and skill shell."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path


PACKAGE_NAME = "browserwright"
# Bumped to "2" by ADR-0009: the group binding moved from per-tab ownership
# markers to the group title, so `queryGroup` no longer carries
# `ownedSessionId` and `attachActive` / `createTab` / `queryGroup` no longer
# take `groupId` / `sessionId`. A version-1 extension cannot answer the new
# shape, and the drift check reloads it.
EXTENSION_PROTOCOL_VERSION = "2"
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


def strict_daemon_issues(daemon_version: str | None,
                         installed: str) -> list[VersionIssue]:
    """Issues for "the *live* daemon must be running exactly ``installed``".

    Issue #57. This is deliberately NOT :attr:`VersionComparison.compatible`.
    That predicate accepts MINOR drift on purpose — it answers "can this
    extension and this daemon talk to each other", and `backends/extension.py`
    depends on its breadth to keep working across a minor bump. Asked after an
    upgrade, though, it can only ever catch a major bump: 0.9.0 against a still
    running 0.8.2 is `drift=minor` -> compatible -> "versions ok", which is how
    a completely failed upgrade reported complete success.

    After an upgrade the only acceptable answer is equality, so this lives apart
    from `check_versions()` and is opt-in per call site — a dev checkout is
    routinely a different version from the machine-global daemon, and failing
    there would be noise.
    """
    if daemon_version is None:
        return [VersionIssue(
            "daemon-not-running",
            "no daemon answered /__status__, so its version cannot be confirmed",
        )]
    if str(daemon_version) != installed:
        return [VersionIssue(
            "daemon-version-mismatch",
            f"the running daemon reports {daemon_version}, not {installed} — "
            "the binary on disk was upgraded but the live process was not "
            "replaced (`browserwright-daemon restart`)",
        )]
    return []


def apply_strict_daemon(info: dict, installed: str) -> dict:
    """Fold :func:`strict_daemon_issues` into a `version_info()` dict in place.

    Keeps `ok` and `issues` consistent so every existing consumer — the human
    branch, the `--json` branch and the exit code — picks the failure up without
    each one re-deriving it.
    """
    extra = strict_daemon_issues(info.get("daemon_version"), installed)
    if extra:
        info["ok"] = False
        info["issues"] = list(info.get("issues") or []) + [
            issue.__dict__ for issue in extra]
    return info


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
