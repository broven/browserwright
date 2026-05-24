"""Version metadata shared by the CLI, daemon, extension, and skill shell."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path


PACKAGE_NAME = "browserwright"
EXTENSION_PROTOCOL_VERSION = "1"
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def _repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
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
    """Return version consistency issues across the repo-distributed pieces."""
    issues: list[VersionIssue] = []
    pkg = package_version()
    ext = extension_version()
    if not is_semver(pkg):
        issues.append(VersionIssue("package-not-semver", f"package version is not semver: {pkg}"))
    if ext is None:
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
