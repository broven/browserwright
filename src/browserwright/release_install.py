"""Install browserwright as immutable local releases.

The development checkout may be broken at any time. Global agent entry points
therefore point at a copied release directory, never at the checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .version import package_version


ROOT_ENV = "BROWSERWRIGHT_RELEASE_ROOT"
LOCAL_BIN_ENV = "BROWSERWRIGHT_LOCAL_BIN"
SKILL_TARGETS_ENV = "BROWSERWRIGHT_SKILL_TARGETS"
CHROME_EXTENSION_TARGET_ENV = "BROWSERWRIGHT_CHROME_EXTENSION_TARGET"


class ReleaseError(RuntimeError):
    pass


def data_root() -> Path:
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "browserwright"


def releases_dir() -> Path:
    return data_root() / "releases"


def release_dir(version: str) -> Path:
    return releases_dir() / version


def local_bin_dir() -> Path:
    override = os.environ.get(LOCAL_BIN_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "bin"


def skill_targets() -> list[Path]:
    override = os.environ.get(SKILL_TARGETS_ENV)
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    return [
        Path.home() / ".claude" / "skills" / "browserwright",
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        / "skills"
        / "browserwright",
        Path.home() / ".pi" / "agent" / "skills" / "browserwright",
    ]


def chrome_extension_target() -> Path | None:
    override = os.environ.get(CHROME_EXTENSION_TARGET_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Mobile Documents"
            / "com~apple~CloudDocs"
            / "etc"
            / "chrome-extension"
            / "browserwright"
        )
    return None


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise ReleaseError("cannot locate repo root from installed package")


def _file_hash(path: Path) -> bytes:
    h = hashlib.sha256()
    h.update(path.name.encode("utf-8"))
    h.update(b"\0")
    h.update(path.read_bytes())
    return h.digest()


def hash_paths(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for root in sorted(paths, key=lambda p: p.as_posix()):
        if not root.exists():
            continue
        if root.is_file():
            h.update(root.as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(_file_hash(root))
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if "__pycache__" in path.parts or path.name.endswith((".pyc", ".pyo")):
                continue
            rel = path.relative_to(root).as_posix()
            h.update(root.name.encode("utf-8"))
            h.update(b"/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(_file_hash(path))
    return h.hexdigest()


def component_hashes(root: Path | None = None) -> dict[str, str]:
    root = repo_root() if root is None else root
    return {
        "python": hash_paths([
            root / "pyproject.toml",
            root / "uv.lock",
            root / "src" / "browserwright",
        ]),
        "skill": hash_paths([root / "skill"]),
        "chrome_extension": hash_paths([root / "chrome-extension"]),
    }


def git_info(root: Path | None = None) -> dict[str, Any]:
    root = repo_root() if root is None else root

    def run(args: list[str]) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    status = run(["status", "--porcelain"])
    return {
        "commit": run(["rev-parse", "HEAD"]),
        "dirty": bool(status),
    }


def read_release_metadata(version: str) -> dict[str, Any] | None:
    path = release_dir(version) / "release.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def list_releases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = releases_dir()
    if not base.exists():
        return out
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        meta = read_release_metadata(entry.name) or {"version": entry.name}
        meta["path"] = str(entry)
        meta["active"] = entry.resolve() == active_release_dir()
        out.append(meta)
    return out


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    except FileNotFoundError as e:
        raise ReleaseError(f"command not found: {cmd[0]}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(f"command failed ({proc.returncode}): {' '.join(cmd)}{suffix}")


def _copytree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        raise ReleaseError(f"required directory missing: {src}")
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def sync_chrome_extension(version: str) -> dict[str, Any] | None:
    """Copy the active release extension into the stable Chrome load path."""
    target = chrome_extension_target()
    if target is None:
        return None
    src = release_dir(version) / "chrome-extension"
    if not src.is_dir():
        raise ReleaseError(f"release chrome-extension missing: {src}")
    if target.exists() and target.is_symlink():
        target.unlink()
    elif target.exists() and not target.is_dir():
        raise ReleaseError(f"chrome extension target is not a directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(
        src,
        tmp,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    if target.exists():
        shutil.rmtree(target)
    os.replace(tmp, target)
    return {"path": str(target), "source": str(src)}


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _build_wheel(root: Path, target: Path) -> Path:
    dist = target / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    _run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=root)
    wheels = sorted(dist.glob("browserwright-*.whl"))
    if not wheels:
        raise ReleaseError("uv build did not produce a browserwright wheel")
    return wheels[-1]


def _install_wheel(root: Path, target: Path) -> None:
    wheels = sorted((target / "dist").glob("browserwright-*.whl"))
    if not wheels:
        raise ReleaseError("release dist does not contain a browserwright wheel")
    venv = target / ".venv"
    _run(["uv", "venv", str(venv)], cwd=root)
    py = _venv_python(venv)
    _run(["uv", "pip", "install", "--python", str(py), str(wheels[-1])], cwd=root)


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(str(target), str(tmp))
    os.replace(tmp, link)


def active_release_dir() -> Path | None:
    link = local_bin_dir() / "browserwright"
    if not link.is_symlink():
        return None
    try:
        target = link.resolve()
    except OSError:
        return None
    for parent in target.parents:
        if parent.parent == releases_dir():
            return parent
    return None


def active_version() -> str | None:
    active = active_release_dir()
    return active.name if active else None


def activate(version: str) -> dict[str, Any]:
    target = release_dir(version)
    if not target.is_dir():
        raise ReleaseError(f"release not installed: {version}")
    bin_dir = local_bin_dir()
    _atomic_symlink(target / ".venv" / "bin" / "browserwright", bin_dir / "browserwright")
    _atomic_symlink(
        target / ".venv" / "bin" / "browserwright-daemon",
        bin_dir / "browserwright-daemon",
    )
    for skill in skill_targets():
        _atomic_symlink(target / "skill", skill)
    return {"version": version, "path": str(target)}


def install_local(*, force: bool = False, activate_release: bool = True) -> dict[str, Any]:
    root = repo_root()
    version = package_version()
    final = release_dir(version)
    previous_version = active_version()
    previous_meta = read_release_metadata(previous_version) if previous_version else None
    if final.exists():
        if not force:
            raise ReleaseError(f"release {version} already exists; use --force to replace it")
    releases_dir().mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=str(releases_dir())))
    backup: Path | None = None
    try:
        _copytree(root / "skill", tmp / "skill")
        _copytree(root / "chrome-extension", tmp / "chrome-extension")
        _build_wheel(root, tmp)
        hashes = component_hashes(root)
        meta = {
            "schema_version": 1,
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "repo": str(root),
                **git_info(root),
            },
            "components": hashes,
        }
        (tmp / "release.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if final.exists():
            backup = final.with_name(f".{final.name}.backup-{os.getpid()}")
            shutil.rmtree(backup, ignore_errors=True)
            os.replace(final, backup)
        os.replace(tmp, final)
        _install_wheel(root, final)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        if backup is not None:
            shutil.rmtree(final, ignore_errors=True)
            if backup.exists():
                os.replace(backup, final)
        elif not final.exists() or not (final / ".venv").exists():
            shutil.rmtree(final, ignore_errors=True)
        raise

    activated = activate(version) if activate_release else None
    extension_sync = sync_chrome_extension(version)
    changes = compare_components(previous_meta, meta)
    return {
        "ok": True,
        "version": version,
        "path": str(final),
        "previous_version": previous_version,
        "activated": activated is not None,
        "chrome_extension_sync": extension_sync,
        "changes": changes,
        "actions": required_actions(changes),
    }


def compare_components(
    previous_meta: dict[str, Any] | None,
    current_meta: dict[str, Any],
) -> dict[str, bool]:
    prev = (previous_meta or {}).get("components") or {}
    cur = current_meta.get("components") or {}
    return {name: prev.get(name) != digest for name, digest in cur.items()}


def required_actions(changes: dict[str, bool]) -> dict[str, bool]:
    return {
        "restart_daemon": bool(changes.get("python")),
        "reload_chrome_extension": bool(changes.get("chrome_extension")),
        "skill_relinked": bool(changes.get("skill")),
    }


def _daemon_status() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["browserwright-daemon", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"alive": False, "version": None, "error": "status unavailable"}
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        data = {}
    data.setdefault("alive", proc.returncode == 0)
    data.setdefault("version", None)
    return data


def status() -> dict[str, Any]:
    active = active_release_dir()
    version = active.name if active else None
    daemon = _daemon_status()
    skill = [
        {
            "path": str(path),
            "target": str(path.resolve()) if path.is_symlink() else None,
            "ok": path.is_symlink() and active is not None and path.resolve() == active / "skill",
        }
        for path in skill_targets()
    ]
    return {
        "schema_version": 1,
        "installed_version": version,
        "installed_path": str(active) if active else None,
        "chrome_extension_target": str(chrome_extension_target())
        if chrome_extension_target()
        else None,
        "daemon": {
            "alive": bool(daemon.get("alive")),
            "version": daemon.get("version"),
            "restart_required": bool(
                daemon.get("alive") and version and daemon.get("version") != version
            ),
        },
        "skill": skill,
        "releases": list_releases(),
    }
