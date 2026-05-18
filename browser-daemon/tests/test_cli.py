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
    assert names == {"env", "rdp", "autoconnect", "extension", "cloud"}


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
    """No env var, no Chrome on 9222, no autoconnect profile (we use an empty
    HOME via env override to force that)."""
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


# Patch B warning prefix — we assert on this exact substring so the test
# stays robust against the rate-limit error message (which Patch A formats
# differently and which uses overlapping vocabulary).
_PATCH_B_PREFIX = "WARNING: autoconnect path triggers"


def _isolated_runtime_dir(tmp_dir_root: str = "/tmp") -> str:
    """Each CLI test gets a fresh XDG_RUNTIME_DIR so the autoconnect
    timestamp file (Patch A) doesn't leak between tests."""
    import tempfile
    return tempfile.mkdtemp(prefix="bd-cli-", dir=tmp_dir_root)


def test_url_explicit_autoconnect_emits_stderr_warning():
    """P0 defense Patch B: `url --backend autoconnect` warns about popup
    accumulation on stderr. The warning fires BEFORE the resolve attempt,
    so it's always visible regardless of whether Chrome is reachable.
    """
    rt = _isolated_runtime_dir()
    code, out, err = _run(
        ["url", "--backend", "autoconnect"],
        env_overrides={"XDG_RUNTIME_DIR": rt,
                       # Force-bypass rate-limit so this test focuses on
                       # the Patch B warning, not Patch A's behavior.
                       "BD_FORCE_AUTOCONNECT_RECONNECT": "1"},
    )
    assert _PATCH_B_PREFIX in err, \
        f"missing Patch B warning prefix in stderr: {err!r}"


def test_url_explicit_autoconnect_quiet_suppresses_warning():
    """--quiet must hide the Patch B warning. Rate-limit errors (Patch A)
    can still appear — they're orthogonal."""
    rt = _isolated_runtime_dir()
    code, out, err = _run(
        ["url", "--backend", "autoconnect", "--quiet"],
        env_overrides={"XDG_RUNTIME_DIR": rt,
                       "BD_FORCE_AUTOCONNECT_RECONNECT": "1"},
    )
    assert _PATCH_B_PREFIX not in err, \
        f"--quiet should suppress Patch B warning: {err!r}"


def test_url_auto_chain_no_warning_when_not_explicit():
    """Patch B warning fires ONLY on `--backend autoconnect`. Without
    --backend, the resolver auto-chain might fall through to autoconnect —
    but the user didn't explicitly opt in, so the explicit warning would
    be noise (env / rdp / autoconnect cascade is the default flow).
    """
    rt = _isolated_runtime_dir()
    code, out, err = _run(
        ["url"],
        env_overrides={"XDG_RUNTIME_DIR": rt,
                       "BD_FORCE_AUTOCONNECT_RECONNECT": "1"},
    )
    assert _PATCH_B_PREFIX not in err, \
        f"auto-chain should not emit explicit-autoconnect warning: {err!r}"


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
