from __future__ import annotations

import json
import os
from pathlib import Path


def _fake_repo(root: Path) -> Path:
    (root / "src" / "browserwright").mkdir(parents=True)
    (root / "src" / "browserwright" / "__init__.py").write_text("# package\n", encoding="utf-8")
    (root / "skill").mkdir()
    (root / "skill" / "SKILL.md").write_text("# shell\n", encoding="utf-8")
    (root / "chrome-extension").mkdir()
    (root / "chrome-extension" / "manifest.json").write_text(
        '{"version":"0.6.0"}\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname="browserwright"\nversion="0.6.0"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("# lock\n", encoding="utf-8")
    return root


def _isolate_release_env(monkeypatch, tmp_path):
    from browserwright import release_install

    release_root = tmp_path / "global" / "share" / "browserwright"
    local_bin = tmp_path / "global" / "bin"
    skill_a = tmp_path / "claude" / "skills" / "browserwright"
    skill_b = tmp_path / "codex" / "skills" / "browserwright"
    skill_c = tmp_path / "pi" / "skills" / "browserwright"
    chrome_target = tmp_path / "icloud" / "chrome-extension" / "browserwright"
    monkeypatch.setenv(release_install.ROOT_ENV, str(release_root))
    monkeypatch.setenv(release_install.LOCAL_BIN_ENV, str(local_bin))
    monkeypatch.setenv(release_install.CHROME_EXTENSION_TARGET_ENV, str(chrome_target))
    monkeypatch.setenv(
        release_install.SKILL_TARGETS_ENV,
        os.pathsep.join([str(skill_a), str(skill_b), str(skill_c)]),
    )
    return release_root, local_bin, skill_a, skill_b, skill_c


def test_default_skill_targets_include_claude_codex_and_pi(monkeypatch, tmp_path):
    from browserwright import release_install

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv(release_install.SKILL_TARGETS_ENV, raising=False)

    assert release_install.skill_targets() == [
        home / ".claude" / "skills" / "browserwright",
        home / ".agents" / "skills" / "browserwright",
        home / ".pi" / "agent" / "skills" / "browserwright",
    ]


def test_release_install_local_copies_artifacts_and_activates(monkeypatch, tmp_path):
    from browserwright import release_install

    repo = _fake_repo(tmp_path / "repo")
    release_root, local_bin, skill_a, skill_b, skill_c = _isolate_release_env(monkeypatch, tmp_path)
    monkeypatch.setattr(release_install, "repo_root", lambda: repo)
    monkeypatch.setattr(release_install, "package_version", lambda: "0.6.0")
    monkeypatch.setattr(
        release_install,
        "git_info",
        lambda root=None: {"commit": "abc123", "dirty": False},
    )

    def fake_run(cmd, *, cwd=None):
        if cmd[:2] == ["uv", "build"]:
            out_dir = Path(cmd[cmd.index("--out-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "browserwright-0.6.0-py3-none-any.whl").write_text("wheel")
        elif cmd[:2] == ["uv", "venv"]:
            bin_dir = Path(cmd[2]) / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("# python")
            (bin_dir / "browserwright").write_text("# cli")
            (bin_dir / "browserwright-daemon").write_text("# daemon")

    monkeypatch.setattr(release_install, "_run", fake_run)

    result = release_install.install_local(force=False)

    installed = release_root / "releases" / "0.6.0"
    assert result["ok"] is True
    assert installed.is_dir()
    assert (installed / "skill" / "SKILL.md").read_text(encoding="utf-8") == "# shell\n"
    assert (installed / "chrome-extension" / "manifest.json").is_file()
    assert json.loads((installed / "release.json").read_text(encoding="utf-8"))["version"] == "0.6.0"
    assert local_bin.joinpath("browserwright").resolve() == installed / ".venv" / "bin" / "browserwright"
    assert not skill_a.is_symlink()
    assert not skill_b.is_symlink()
    assert not skill_c.is_symlink()
    assert (skill_a / "SKILL.md").read_text(encoding="utf-8") == "# shell\n"
    assert (skill_b / "SKILL.md").read_text(encoding="utf-8") == "# shell\n"
    assert (skill_c / "SKILL.md").read_text(encoding="utf-8") == "# shell\n"
    assert result["chrome_extension_sync"]["path"].endswith("chrome-extension/browserwright")
    assert (tmp_path / "icloud" / "chrome-extension" / "browserwright" / "manifest.json").is_file()


def test_release_status_reports_daemon_restart_and_copied_skill(monkeypatch, tmp_path):
    from browserwright import release_install

    _release_root, local_bin, skill_a, _skill_b, _skill_c = _isolate_release_env(monkeypatch, tmp_path)
    installed = release_install.release_dir("0.6.0")
    (installed / ".venv" / "bin").mkdir(parents=True)
    (installed / ".venv" / "bin" / "browserwright").write_text("# cli")
    (installed / "skill").mkdir()
    (installed / "skill" / "SKILL.md").write_text("# shell\n", encoding="utf-8")
    (installed / "release.json").write_text('{"version":"0.6.0"}\n', encoding="utf-8")
    release_install._atomic_symlink(installed / ".venv" / "bin" / "browserwright", local_bin / "browserwright")
    release_install._copytree_replace(installed / "skill", skill_a)
    monkeypatch.setattr(release_install, "_daemon_status", lambda: {"alive": True, "version": "0.5.9"})

    status = release_install.status()

    assert status["installed_version"] == "0.6.0"
    assert status["daemon"]["restart_required"] is True
    assert status["skill"][0]["ok"] is True
    assert status["skill"][0]["target"] is None


def test_release_activate_rejects_missing_version(monkeypatch, tmp_path):
    from browserwright import release_install

    _isolate_release_env(monkeypatch, tmp_path)

    try:
        release_install.activate("9.9.9")
    except release_install.ReleaseError as e:
        assert "release not installed" in str(e)
    else:
        raise AssertionError("activate should reject missing release")


def test_version_finds_extension_manifest_from_release_parent(monkeypatch, tmp_path):
    from browserwright import version as version_mod

    release = tmp_path / "releases" / "0.6.0"
    package_dir = release / ".venv" / "lib" / "python3.11" / "site-packages" / "browserwright"
    package_dir.mkdir(parents=True)
    (release / "chrome-extension").mkdir(parents=True)
    manifest = release / "chrome-extension" / "manifest.json"
    manifest.write_text('{"version":"0.6.0"}\n', encoding="utf-8")
    fake_file = package_dir / "version.py"
    fake_file.write_text("# fake\n", encoding="utf-8")

    monkeypatch.setattr(version_mod, "_repo_root", lambda: None)
    monkeypatch.setattr(version_mod, "__file__", str(fake_file))

    assert version_mod.extension_manifest_path() == manifest
    assert version_mod.extension_version() == "0.6.0"
