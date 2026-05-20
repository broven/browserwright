"""CLI integration — actually run `browser-daemon` as a subprocess and check
stdout/stderr/exit code byte-for-byte. This is the Skill-facing contract.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run(args, env_overrides=None):
    """Spawn `python -m browser_daemon.cli <args>` with a controlled env.
    Returns (returncode, stdout, stderr) — all str."""
    env = os.environ.copy()
    # Strip proxy + BD/BU envs so the subprocess sees a clean slate.
    for var in [
        "BD_BACKEND", "BD_CDP_WS", "BD_CDP_URL", "BD_CONFIG",
        "BD_NAME", "BD_TIMEOUT", "BU_CDP_WS", "BU_CDP_URL",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ]:
        env.pop(var, None)
    if env_overrides:
        env.update(env_overrides)
    p = subprocess.run(
        [sys.executable, "-m", "browser_daemon.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return p.returncode, p.stdout, p.stderr


def test_version_subcommand():
    code, out, err = _run(["version"])
    assert code == 0
    assert out.startswith("browser-daemon ")
    assert err == ""


def test_list_backends_json_shape():
    code, out, err = _run(["list-backends", "--json"])
    assert code == 0
    data = json.loads(out)
    from browser_daemon.doctor import SCHEMA_VERSION

    assert data["schema_version"] == SCHEMA_VERSION
    names = {b["name"] for b in data["backends"]}
    assert names == {"env", "rdp", "extension", "cloud"}


def test_doctor_json_schema_v1():
    code, out, err = _run(["doctor", "--json"])
    assert code == 0, f"stderr: {err}"
    data = json.loads(out)
    from browser_daemon.doctor import SCHEMA_VERSION

    assert data["schema_version"] == SCHEMA_VERSION
    assert "backends" in data and isinstance(data["backends"], list)
    # Every entry has the full key set even when not available
    for entry in data["backends"]:
        for key in ("name", "available", "ws_url", "detail",
                    "ux_warning", "needs_user_action", "ux_cost"):
            assert key in entry, f"missing {key} in {entry!r}"


def test_url_with_bd_cdp_ws_outputs_bare_url():
    """spec §5.1: ONE line on stdout, no decoration."""
    raw = "wss://my.cloud.example/cdp?token=secret"
    code, out, err = _run(["url"], env_overrides={"BD_CDP_WS": raw})
    assert code == 0
    assert out == raw + "\n"
    assert err == ""


def test_url_no_backend_available_exit_code_2():
    """No env var, no Chrome on 9222 — empty HOME via env override."""
    code, out, err = _run(
        ["url", "--backend", "env"],
        env_overrides={"BD_CDP_WS": "", "BD_CDP_URL": ""},
    )
    # Even with BD_CDP_WS="" set explicitly, env backend treats empty-string
    # as "not set" — the convention from browser-harness. Exit 2 = unavailable.
    assert code == 2
    assert out == ""
    assert "error:" in err


def test_unknown_backend_exit_code_1():
    code, out, err = _run(["url", "--backend", "totally-fake"])
    # argparse rejects unknown choice → exit 2 (its own convention). Our
    # `--backend` is a `choices=` argument so it shortcircuits at parse time.
    assert code == 2
    assert "totally-fake" in (out + err)


# ---- LaunchAgent install (v0.5.5) ----------------------------------------


def test_install_plist_content_includes_serve_args():
    """The plist must spawn `browser-daemon serve --backend X --name Y` with
    KeepAlive and RunAtLoad. Direct unit test of the generator — we don't
    actually `launchctl load` in tests (side effects + needs a real macOS)."""
    from browser_daemon import cli as cli_mod
    content = cli_mod._build_plist(
        label="com.browser-daemon.test1",
        name="test1",
        backend="extension",
        extension_port=29999,
    )
    assert "com.browser-daemon.test1" in content
    assert "<string>serve</string>" in content
    assert "<string>--backend</string>" in content
    assert "<string>extension</string>" in content
    assert "<string>--name</string>" in content
    assert "<string>test1</string>" in content
    assert "<string>--extension-port</string>" in content
    assert "<string>29999</string>" in content
    assert "<key>RunAtLoad</key><true/>" in content
    assert "<key>KeepAlive</key>" in content
    assert "<key>Crashed</key><true/>" in content
    # Successful exits don't restart — Crashed=true is the only respawn trigger.
    assert "<key>SuccessfulExit</key><false/>" in content


def test_install_plist_omits_extension_port_when_unspecified():
    """No --extension-port flag in the plist when caller didn't pass one;
    daemon falls back to its default (19989)."""
    from browser_daemon import cli as cli_mod
    content = cli_mod._build_plist(
        label="com.browser-daemon.t2", name="t2",
        backend="extension", extension_port=None,
    )
    assert "<string>--extension-port</string>" not in content


def test_install_rejects_invalid_name():
    """H-2 regression: --name must be alphanumeric/(dot)/dash/underscore so
    it can't escape ~/Library/LaunchAgents/ or land malformed XML in the
    plist. The check fires before any filesystem write — the upstream
    config-level `check_name` is the first line of defense (and rejects
    everything outside [A-Za-z0-9_-]{1,64}); the install-level
    `_validate_daemon_name` is the belt-and-suspenders second guard. We
    accept either error message — the security guarantee is "this path
    NEVER touches the filesystem"."""
    # Path-traversal attempt — must error out, never write a plist.
    code, _, err = _run(["install", "--name", "../../tmp/evil"])
    assert code == 1
    assert "1-64" in err or "[A-Za-z0-9" in err
    # XML-special chars (would corrupt the plist if interpolated raw).
    code, _, err = _run(["install", "--name", "weird<chars>"])
    assert code == 1
    # Slash in the middle is invalid too.
    code, _, err = _run(["install", "--name", "weird/name"])
    assert code == 1


def test_uninstall_rejects_invalid_name():
    """Same name guard applies to uninstall (otherwise an attacker who can
    craft args could unlink arbitrary plist paths via the un-validated
    LaunchAgent dir join)."""
    code, _, err = _run(["uninstall", "--name", "../../tmp/evil"])
    assert code == 1


def test_build_plist_escapes_special_characters_in_binary_path(monkeypatch):
    """H-2 unit test: even after the name guard, every interpolated string
    runs through xml.sax.saxutils.escape. We exercise the binary-path path
    because real macOS paths can legitimately contain spaces and brackets
    (e.g. "/Applications/Some <App>/.../browser-daemon")."""
    import plistlib
    from browser_daemon import cli as cli_mod

    weird_path = "/path/with <special> & chars/browser-daemon"
    monkeypatch.setattr(cli_mod, "_resolve_browser_daemon_bin",
                        lambda: weird_path)
    content = cli_mod._build_plist(
        label="com.browser-daemon.escape-test",
        name="escape-test",
        backend="extension",
        extension_port=None,
    )
    # If the escape works, plistlib will round-trip it cleanly. If raw chars
    # leaked in, plistlib raises an XML parse error.
    parsed = plistlib.loads(content.encode())
    assert parsed["ProgramArguments"][0] == weird_path
    # Sanity: Label / name etc. round-trip too.
    assert parsed["Label"] == "com.browser-daemon.escape-test"


def test_install_subcommand_refuses_on_non_darwin(monkeypatch):
    """install/uninstall are macOS-only — fail loudly elsewhere instead of
    writing a useless plist nothing knows how to load."""
    if sys.platform == "darwin":
        # Can't easily flip sys.platform in a subprocess test; the matching
        # negative test runs on CI Linux. Skip locally.
        import pytest as _pt
        _pt.skip("darwin host — non-darwin guard is exercised in CI")
    code, _, err = _run(["install", "--name", "t3"])
    assert code != 0
    assert "macOS-only" in err


def test_list_subcommand_runs_clean():
    """`browser-daemon list` should at minimum not crash on a host with no
    LaunchAgents and no running daemon."""
    code, out, err = _run(["list"])
    assert code == 0
    # Either "no daemon instances found" or a table with NAME header — both
    # are acceptable shapes. We just want non-zero exit + no traceback.
    assert "Traceback" not in err


def test_url_json_carries_extras():
    raw = "ws://127.0.0.1:9222/devtools/browser/some-uuid"
    code, out, err = _run(["url", "--json"], env_overrides={"BD_CDP_WS": raw})
    assert code == 0
    data = json.loads(out)
    assert data["ws_url"] == raw
    # `url --json` ResolveResult shape is its OWN schema; the v0.5.3 bump
    # of doctor.SCHEMA_VERSION to 2 (F-1+F-2) does NOT apply here — the
    # url JSON shape didn't change. Keep this literal 1 until a real
    # ResolveResult contract change forces a separate bump.
    assert data["schema_version"] == 1
    assert data["backend"] == "env"
    assert isinstance(data["extras"], dict)


def test_end_session_subcommand_registered():
    """P5: `browser-daemon end-session --session ID` parses and is dispatched."""
    from browser_daemon import cli

    assert "end-session" in cli._DISPATCH
    parser = cli._build_parser()
    args = parser.parse_args(["end-session", "--session", "7", "--name", "d"])
    assert args.cmd == "end-session"
    assert args.session == "7"
