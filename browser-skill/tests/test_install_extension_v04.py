"""v0.4 install-wizard wire: extension backend.

Skill mirrors the daemon's v0.4 ship without a Skill version bump:
``install.py`` probes ``browser-daemon doctor --json`` for an
``extension`` backend entry with ``available=true``. When present,
wizard option 3 surfaces as live and the persisted preference becomes
``daemon.preferred_backend = "extension"``.

All tests are pure mocks — no real daemon, no real Chrome, no popup risk
(per chrome-popup-test-policy memory).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest


# ---- _extension_backend_available() ----------------------------------


def _stub_doctor(monkeypatch, blob: dict) -> None:
    from browser_skill import daemon_client
    monkeypatch.setattr(daemon_client.DaemonClient, "doctor",
                        lambda self: blob)


def test_extension_available_true_when_doctor_reports_available(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "rdp", "available": True, "ux_cost": "none"},
            {"name": "extension", "available": True, "ux_cost": "none",
             "ws_url": "ws+unix:///tmp/relay.sock"},
        ],
    })
    from browser_skill import install
    assert install._extension_backend_available() is True


def test_extension_available_false_when_entry_unavailable(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "extension", "available": False,
             "needs_user_action": "load the unpacked extension first"},
        ],
    })
    from browser_skill import install
    assert install._extension_backend_available() is False


def test_extension_available_false_when_entry_missing(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "rdp", "available": True, "ux_cost": "none"},
        ],
    })
    from browser_skill import install
    assert install._extension_backend_available() is False


def test_extension_available_false_when_doctor_synthetic_error(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [],
        "error": "browser-daemon: not found on PATH",
        "skill_synthetic": True,
    })
    from browser_skill import install
    assert install._extension_backend_available() is False


# ---- chrome_extension_path() ----------------------------------------


def test_chrome_extension_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "ce"
    target.mkdir()
    monkeypatch.setenv("BS_CHROME_EXTENSION_PATH", str(target))
    # Even if subprocess would succeed, env wins — make subprocess raise
    # so we know the env path short-circuited.
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("subprocess should not be called")))
    from browser_skill import install
    # Re-import path reference to make sure we don't capture the stub
    # before patching.
    assert install.chrome_extension_path() == str(target)


def test_chrome_extension_path_daemon_subprocess_json(monkeypatch, tmp_path):
    monkeypatch.delenv("BS_CHROME_EXTENSION_PATH", raising=False)
    target = tmp_path / "daemon-ext"
    target.mkdir()

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({"path": str(target)})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc())
    from browser_skill import install
    assert install.chrome_extension_path() == str(target)


def test_chrome_extension_path_returns_none_when_daemon_missing(monkeypatch):
    monkeypatch.delenv("BS_CHROME_EXTENSION_PATH", raising=False)

    def _no_daemon(*a, **kw):
        raise FileNotFoundError("browser-daemon")

    monkeypatch.setattr(subprocess, "run", _no_daemon)
    # Also blank out the binary so the fallback walk fails too.
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    from browser_skill import install
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    assert install.chrome_extension_path() is None


# ---- wizard end-to-end (option 3) -----------------------------------


def _drive_wizard(monkeypatch, inputs, *, ext_live=True, ext_dir=None):
    """Helper: pre-load doctor + chrome_extension_path + input() then call
    install.run(). Returns ``(exit_code, stdout, stderr)``."""
    blob = {
        "schema_version": 1,
        "backends": [
            {"name": "extension", "available": bool(ext_live),
             "ux_cost": "none"},
        ],
    }
    _stub_doctor(monkeypatch, blob)
    from browser_skill import install
    monkeypatch.setattr(install, "chrome_extension_path", lambda: ext_dir)

    iter_inputs = iter(inputs)

    def _fake_input(prompt: str = "") -> str:
        try:
            return next(iter_inputs)
        except StopIteration:
            return ""

    monkeypatch.setattr("builtins.input", _fake_input)
    return install.run()


def test_wizard_choice_4_available_writes_preference(monkeypatch, tmp_bs_home,
                                                     capsys):
    # inputs: choice=3, persist=y.
    rc = _drive_wizard(monkeypatch, ["3", "y"], ext_live=True,
                       ext_dir="/opt/browser-daemon/chrome-extension")
    out = capsys.readouterr().out
    assert rc == 0
    assert "wrote daemon.preferred_backend = 'extension'" in out
    # Wizard surfaced the absolute extension path.
    assert "/opt/browser-daemon/chrome-extension" in out
    # And mentioned the Mode B requirement.
    assert "serve --backend extension" in out
    assert "Mode A" in out  # the cautionary aside about Mode A unavailability

    from browser_skill.memory.global_mem import global_memory
    blob = global_memory().read()
    daemon_fm = blob.get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("preferred_backend") == "extension"


def test_wizard_choice_4_unavailable_blocks(monkeypatch, tmp_bs_home, capsys):
    rc = _drive_wizard(monkeypatch, ["3"], ext_live=False, ext_dir=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Extension backend is not yet available" in out

    # No preference should have been persisted.
    from browser_skill.memory.global_mem import global_memory
    blob = global_memory().read()
    daemon_fm = blob.get("frontmatter", {}).get("daemon", {})
    assert "preferred_backend" not in daemon_fm


def test_wizard_choice_4_available_no_path_falls_back_to_hint(monkeypatch,
                                                              tmp_bs_home,
                                                              capsys):
    # Wizard should still complete; just print the generic hint instead of
    # an absolute path.
    rc = _drive_wizard(monkeypatch, ["3", "y"], ext_live=True, ext_dir=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "browser-daemon extension-path --json" in out


def test_wizard_extension_option_renders_as_coming_when_unavailable(monkeypatch,
                                                                    tmp_bs_home,
                                                                    capsys):
    # Pick option 1 instead so we don't hit the option-4 unavailable branch;
    # just assert the menu label was rewritten.
    rc = _drive_wizard(monkeypatch, ["1", "n"], ext_live=False, ext_dir=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon reports extension backend not yet available" in out


def test_wizard_extension_option_renders_live_when_available(monkeypatch,
                                                             tmp_bs_home,
                                                             capsys):
    rc = _drive_wizard(monkeypatch, ["1", "n"], ext_live=True, ext_dir=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon reports extension backend not yet available" not in out
