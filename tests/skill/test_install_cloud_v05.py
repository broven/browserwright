"""v0.5 install-wizard wire: cloud backend (first-wave skeleton).

Mirrors the v0.4 extension pattern (``test_install_extension_v04.py``):
``install.py`` probes ``browserwright-daemon doctor --json`` for a ``cloud``
backend entry with ``available=true``. When present, wizard option 5
surfaces as live and the user is walked through provider + auth_kind
prompts that collect *references* to credentials only — env-var names,
file paths, URLs with embedded creds. **The secret itself never lands
in Skill memory** (the daemon's auth provider abstraction owns the
credential lifecycle).

Pure-mock tests (no real daemon, no real cloud API, no popup risk per
chrome-popup-test-policy) make up the bulk of this file. The last block —
the **live verify** tests — invoke a real ``browserwright-daemon doctor --json``
when the binary is on PATH (or in the sibling dev-layout); they skip
gracefully when it isn't, so CI without daemon installed stays green.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest


# ---- _cloud_backend_available() -------------------------------------


def _stub_doctor(monkeypatch, blob: dict) -> None:
    from browserwright import daemon_client
    monkeypatch.setattr(daemon_client.DaemonClient, "doctor",
                        lambda self: blob)


def test_cloud_available_true_when_doctor_lists_it(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "cloud", "available": True, "ux_cost": "none",
             "ws_url": "wss://api.example.com/ws"},
        ],
    })
    from browserwright import install
    assert install._cloud_backend_available() is True


def test_cloud_available_false_when_entry_unavailable(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "cloud", "available": False,
             "needs_user_action": "configure auth provider first"},
        ],
    })
    from browserwright import install
    assert install._cloud_backend_available() is False


def test_cloud_available_false_when_entry_missing(monkeypatch):
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "rdp", "available": True, "ux_cost": "none"},
            {"name": "extension", "available": True, "ux_cost": "none"},
        ],
    })
    from browserwright import install
    assert install._cloud_backend_available() is False


# ---- wizard menu rendering ------------------------------------------


def _drive_wizard(monkeypatch, inputs, *, ext_live=False, cloud_live=False,
                  cloud_extras=None, tmp_path=None):
    """Pre-load doctor + mock ``input()``, call wizard, return its exit code.
    Caller checks ``capsys`` separately."""
    backends = []
    if ext_live:
        backends.append({"name": "extension", "available": True, "ux_cost": "none"})
    if cloud_live:
        entry = {"name": "cloud", "available": True, "ux_cost": "auth-required"}
        if cloud_extras is not None:
            entry["extras"] = cloud_extras
        backends.append(entry)
    _stub_doctor(monkeypatch, {"schema_version": 1, "backends": backends})

    # Crucial: never let the wizard write to the real ~/.config/browserwright-daemon
    # during tests. Each test redirects to a per-test tmp file.
    if tmp_path is not None:
        monkeypatch.setenv("BS_DAEMON_CONFIG_PATH",
                           str(tmp_path / "fake-daemon-config.toml"))

    iter_inputs = iter(inputs)

    def _fake_input(prompt: str = "") -> str:
        try:
            return next(iter_inputs)
        except StopIteration:
            return ""

    monkeypatch.setattr("builtins.input", _fake_input)
    from browserwright import install
    return install.run()


def test_menu_shows_cloud_as_coming_when_unavailable(monkeypatch, tmp_bs_home,
                                                     capsys):
    rc = _drive_wizard(monkeypatch, ["1", "n"], cloud_live=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon reports cloud backend not yet available" in out


def test_menu_shows_cloud_as_live_when_available(monkeypatch, tmp_bs_home,
                                                 capsys):
    rc = _drive_wizard(monkeypatch, ["1", "n"], cloud_live=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon reports cloud backend not yet available" not in out
    assert "Cloud/Remote browser" in out


def test_choice_5_blocked_when_cloud_unavailable(monkeypatch, tmp_bs_home,
                                                  capsys):
    rc = _drive_wizard(monkeypatch, ["4"], cloud_live=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Cloud backend is not yet available" in out

    # And memory must be untouched.
    from browserwright.memory.global_mem import global_memory
    daemon_fm = global_memory().read().get("frontmatter", {}).get("daemon", {})
    assert "preferred_backend" not in daemon_fm


# ---- option 5 — bearer auth flow ------------------------------------


def test_wizard_choice_5_bearer_flow_writes_references(monkeypatch,
                                                        tmp_bs_home,
                                                        tmp_path,
                                                        capsys):
    # inputs: choice=4, provider, auth_kind, envvar, endpoint, persist=y
    inputs = [
        "4",                    # menu choice
        "browser-use",          # provider
        "bearer",               # auth_kind
        "BROWSER_USE_API_KEY",  # bearer env-var name
        "",                     # optional endpoint (skip)
        "y",                    # persist preference
    ]
    rc = _drive_wizard(monkeypatch, inputs, cloud_live=True, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "wrote daemon.preferred_backend = 'cloud'" in out
    # Next-steps section must coach the user through the env-var step.
    assert "BROWSER_USE_API_KEY" in out
    assert "browser-use" in out

    from browserwright.memory.global_mem import global_memory
    daemon_fm = global_memory().read().get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("preferred_backend") == "cloud"
    assert daemon_fm.get("cloud_provider_hint") == "browser-use"
    assert daemon_fm.get("cloud_auth_kind") == "bearer"
    assert daemon_fm.get("cloud_token_env") == "BROWSER_USE_API_KEY"
    # And the secret itself must NOT be in memory.
    serialized = repr(daemon_fm)
    assert "your-token" not in serialized
    # No fields from other auth kinds leaked.
    assert "cloud_cert_file" not in daemon_fm
    assert "cloud_key_file" not in daemon_fm
    assert "cloud_username_env" not in daemon_fm
    assert "cloud_password_env" not in daemon_fm

    # Daemon config.toml block was written with daemon-0.5.0 schema:
    # top section keeps endpoint/auth_kind/provider_hint, kind-specific
    # credentials in a [backends.cloud.auth.<kind>] subtable.
    cfg = (tmp_path / "fake-daemon-config.toml").read_text(encoding="utf-8")
    assert "[backends.cloud]" in cfg
    assert "[backends.cloud.auth.bearer]" in cfg
    assert 'provider_hint = "browser-use"' in cfg
    assert 'auth_kind = "bearer"' in cfg
    assert 'token_env = "BROWSER_USE_API_KEY"' in cfg
    # Sanity: no stale `provider = "..."` (old guess) anywhere.
    assert 'provider =' not in cfg


# ---- option 5 — basic auth flow (header mode, env-var refs) ---------


def test_wizard_choice_5_basic_flow_writes_env_var_refs(monkeypatch,
                                                        tmp_bs_home,
                                                        tmp_path, capsys):
    """v0.5 daemon header-mode BasicAuth reads ``username_env`` +
    ``password_env`` from ``[backends.cloud.auth.basic]``. The wizard
    collects the env-var **names**, never the secret itself."""
    inputs = [
        "4",
        "browserless",
        "basic",
        "BROWSERLESS_USER",                          # username_env name
        "BROWSERLESS_PASS",                          # password_env name
        "wss://chrome.browserless.io/ws",            # bare URL (no creds)
        "y",
    ]
    rc = _drive_wizard(monkeypatch, inputs, cloud_live=True, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    # Next-steps coach `export` for both env vars.
    assert "BROWSERLESS_USER" in out
    assert "BROWSERLESS_PASS" in out

    from browserwright.memory.global_mem import global_memory
    daemon_fm = global_memory().read().get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("cloud_provider_hint") == "browserless"
    assert daemon_fm.get("cloud_auth_kind") == "basic"
    assert daemon_fm.get("cloud_username_env") == "BROWSERLESS_USER"
    assert daemon_fm.get("cloud_password_env") == "BROWSERLESS_PASS"
    assert daemon_fm.get("cloud_endpoint") == "wss://chrome.browserless.io/ws"
    # No bearer / mtls fields leaked.
    assert "cloud_token_env" not in daemon_fm
    assert "cloud_cert_file" not in daemon_fm
    # No secrets stored.
    serialized = repr(daemon_fm)
    assert "secret" not in serialized.lower() or "secret" in "BROWSERLESS_USER".lower()

    cfg = (tmp_path / "fake-daemon-config.toml").read_text(encoding="utf-8")
    assert "[backends.cloud]" in cfg
    assert "[backends.cloud.auth.basic]" in cfg
    assert 'username_env = "BROWSERLESS_USER"' in cfg
    assert 'password_env = "BROWSERLESS_PASS"' in cfg
    assert 'endpoint = "wss://chrome.browserless.io/ws"' in cfg


def test_wizard_choice_5_basic_rejects_url_with_embedded_creds(monkeypatch,
                                                                tmp_bs_home,
                                                                capsys):
    """Embedded ``user:pass@`` in the endpoint URL is daemon-side
    ``embed_in_url=true`` territory and would put the secret in
    Skill memory — wizard refuses and points at env-var names."""
    inputs = [
        "4",
        "browserless",
        "basic",
        "BROWSERLESS_USER",
        "BROWSERLESS_PASS",
        "wss://alice:secret@chrome.browserless.io/ws",  # creds in URL
    ]
    rc = _drive_wizard(monkeypatch, inputs, cloud_live=True)
    err = capsys.readouterr().err
    assert rc == 1
    assert "user:pass@" in err or "env-var" in err
    # No memory written.
    from browserwright.memory.global_mem import global_memory
    daemon_fm = global_memory().read().get("frontmatter", {}).get("daemon", {})
    assert "preferred_backend" not in daemon_fm


# ---- option 5 — mtls auth flow --------------------------------------


def test_wizard_choice_5_mtls_flow_writes_cert_and_key_paths(monkeypatch,
                                                              tmp_bs_home,
                                                              tmp_path,
                                                              capsys):
    """v0.5 daemon ``MtlsAuth`` reads ``cert_file`` + ``key_file`` from
    ``[backends.cloud.auth.mtls]``. Note the field names — wizard used
    to write ``cert_path``/``key_path``; that was a guess from the
    forward-prep era and is fixed in this release."""
    inputs = [
        "4",
        "hyperbrowser",
        "mtls",
        "/opt/certs/client.crt",
        "/opt/certs/client.key",
        "wss://api.hyperbrowser.example/cdp",
        "y",
    ]
    rc = _drive_wizard(monkeypatch, inputs, cloud_live=True, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "/opt/certs/client.crt" in out
    assert "/opt/certs/client.key" in out

    from browserwright.memory.global_mem import global_memory
    daemon_fm = global_memory().read().get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("cloud_provider_hint") == "hyperbrowser"
    assert daemon_fm.get("cloud_auth_kind") == "mtls"
    assert daemon_fm.get("cloud_cert_file") == "/opt/certs/client.crt"
    assert daemon_fm.get("cloud_key_file") == "/opt/certs/client.key"
    assert daemon_fm.get("cloud_endpoint") == \
        "wss://api.hyperbrowser.example/cdp"


# ---- option 5 — validation errors -----------------------------------


def test_wizard_choice_5_rejects_unknown_provider(monkeypatch, tmp_bs_home,
                                                  capsys):
    rc = _drive_wizard(monkeypatch, ["4", "bogus-provider"], cloud_live=True)
    err = capsys.readouterr().err
    assert rc == 1
    assert "unknown provider" in err
    assert "browser-use" in err  # error message must list valid choices


def test_wizard_choice_5_rejects_unknown_auth_kind(monkeypatch, tmp_bs_home,
                                                   capsys):
    rc = _drive_wizard(monkeypatch, ["4", "browser-use", "totp"],
                       cloud_live=True)
    err = capsys.readouterr().err
    assert rc == 1
    assert "unknown auth_kind" in err
    assert "bearer" in err


# ---- detection-contract regression guard ----------------------------


def test_doctor_probe_is_the_only_detection_channel(monkeypatch):
    """Spec H3 + HANDOFF detection contract: both ``_extension_backend_available``
    and ``_cloud_backend_available`` must derive their answer from
    ``DaemonClient().doctor()`` alone. Any future helper that opens a ws
    or curls a backend API is a contract break.

    We enforce this by replacing every other plausible network-touching
    primitive with a tripwire and asserting the helper still works."""
    import socket
    from browserwright import install

    def _trip(*a, **kw):
        raise AssertionError(
            "Detection helper opened a network connection — must consume "
            "DaemonClient().doctor() output only (spec H3, HANDOFF)."
        )

    # Replace socket.socket so any ws / TCP attempt blows up loudly.
    monkeypatch.setattr(socket, "socket", _trip)

    # Mock doctor as a clean async-free Python dict.
    _stub_doctor(monkeypatch, {
        "schema_version": 1,
        "backends": [
            {"name": "cloud", "available": True, "ux_cost": "none"},
        ],
    })

    # Both helpers must succeed without ever hitting socket.socket.
    assert install._cloud_backend_available() is True
    assert install._extension_backend_available() is False


# ---- live verify: real `browserwright-daemon doctor --json` ---------------
# Item 1 of v0.5 second wave (team-lead brief). Skipped when the daemon
# binary isn't reachable — CI machines / dev boxes without the sibling
# checkout shouldn't fail; the rest of the suite stays mocked.


def _find_daemon_bin() -> Optional[str]:
    """Return a usable ``browserwright-daemon`` binary path, or ``None``.

    Order:
      1. ``shutil.which`` — daemon installed system-wide.
      2. Sibling dev-layout: ``../browserwright-daemon/.venv/bin/browserwright-daemon``.
    """
    found = shutil.which("browserwright-daemon")
    if found:
        return found
    here = Path(__file__).resolve()
    sibling = (here.parent.parent.parent / "browserwright-daemon"
               / ".venv" / "bin" / "browserwright-daemon")
    return str(sibling) if sibling.exists() else None


@pytest.fixture
def daemon_bin():
    bin_path = _find_daemon_bin()
    if bin_path is None:
        pytest.skip("browserwright-daemon binary not on PATH; live verify skipped")
    return bin_path


def _run_daemon_doctor(daemon_bin: str, *,
                       env_overrides: Optional[dict] = None) -> dict:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [daemon_bin, "doctor", "--json"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert proc.returncode == 0, \
        f"daemon doctor exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


def test_live_daemon_doctor_lists_cloud_entry(daemon_bin):
    """Pure baseline: real daemon emits a ``cloud`` backend entry in its
    doctor JSON regardless of configuration. ``available`` will be False
    because no config / env is set, but the entry's presence proves the
    backend is registered."""
    blob = _run_daemon_doctor(daemon_bin)
    backends = blob.get("backends") or []
    cloud_entries = [b for b in backends if b.get("name") == "cloud"]
    assert len(cloud_entries) == 1, \
        f"expected exactly one cloud entry, got: {cloud_entries}"
    cloud = cloud_entries[0]
    # Contract sanity: required keys.
    for k in ("name", "available", "detail", "ws_url",
              "ux_cost", "ux_warning", "needs_user_action"):
        assert k in cloud, f"cloud entry missing key {k!r}: {cloud}"
    assert cloud["available"] is False
    assert "endpoint" in cloud["detail"].lower() or \
        "configure" in cloud["detail"].lower(), \
        f"unhelpful unavailable detail: {cloud['detail']!r}"


def test_live_daemon_parses_wizard_emitted_config(daemon_bin, tmp_path,
                                                   monkeypatch):
    """End-to-end contract certification: wizard's TOML writer emits a
    config the real daemon can fully load (parses TOML + instantiates
    ``AuthProvider`` + reports cloud available).

    Team-lead's standard: "real invoke daemon ... 能解析自己的 config 没
    error (不必真连云端，能 init AuthProvider 即可)." `doctor` exercises
    the same config-load path that `serve` would. Daemon's
    ``BearerTokenAuth`` resolves the bearer token from the env var named
    in TOML at init time, so we set a fake non-empty value here — the
    daemon never tries to upstream-connect with it during doctor."""
    target = tmp_path / "wizard-emitted-config.toml"
    monkeypatch.setenv("BS_DAEMON_CONFIG_PATH", str(target))
    from browserwright import install
    install._write_daemon_cloud_config(
        "browser-use", "bearer",
        {"cloud_provider_hint": "browser-use",
         "cloud_auth_kind": "bearer",
         "cloud_token_env": "BS_TEST_FAKE_TOKEN_ENV",
         "cloud_endpoint": "wss://api.browser-use.example/ws"},
    )
    assert target.exists()

    blob = _run_daemon_doctor(daemon_bin, env_overrides={
        "BD_CONFIG": str(target),
        "BS_TEST_FAKE_TOKEN_ENV": "not-a-real-token-just-non-empty",
    })
    cloud = next(b for b in blob["backends"] if b["name"] == "cloud")
    # Daemon parsed the config + instantiated AuthProvider successfully.
    assert cloud["available"] is True, (
        "daemon should report cloud available with a wizard-emitted "
        f"config + token env set; got: {cloud}"
    )
    # provider_hint surfaced in detail / extras (daemon-impl-2 contract).
    detail = cloud.get("detail") or ""
    extras = cloud.get("extras") or {}
    surfaced = (
        "browser-use" in detail
        or extras.get("provider") == "browser-use"
        or extras.get("provider_hint") == "browser-use"
    )
    assert surfaced, (
        "daemon should surface provider_hint='browser-use' in doctor "
        f"output; got detail={detail!r} extras={extras!r}"
    )


def test_live_daemon_doctor_surfaces_informative_unavailability(daemon_bin):
    """Negative contract certification: when the config references a
    token env that isn't set, daemon's doctor reports
    ``available=false`` with a **specific** error pointing at the
    missing env var — not just a generic "configure cloud" hint.

    This is the loop closer for the doctor-as-contract principle: agents
    that key on doctor output (Skill's ``_cloud_backend_available()``)
    can show the user exactly what's broken, not just that *something*
    is. Skill-side helper agrees: when fed the real-daemon JSON it
    returns False — matching the daemon's verdict."""
    blob = _run_daemon_doctor(daemon_bin, env_overrides={
        "BD_CLOUD_ENDPOINT": "wss://api.browser-use.example/ws",
        "BD_CLOUD_AUTH_KIND": "bearer",
        "BD_CLOUD_PROVIDER_HINT": "browser-use",
    })
    cloud = next(b for b in blob["backends"] if b["name"] == "cloud")
    # Auth incompletely configured → unavailable.
    assert cloud["available"] is False
    # Detail is *specific* enough to act on, not a generic placeholder.
    detail = (cloud.get("detail") or "").lower()
    assert "auth" in detail or "token" in detail, (
        f"daemon should surface specific auth failure; got: {detail!r}"
    )
    # extras still carries the partial config so an agent UI can show
    # what *was* set vs what's missing.
    extras = cloud.get("extras") or {}
    assert extras.get("endpoint") == "wss://api.browser-use.example/ws"
    assert extras.get("auth_kind") == "bearer"
    assert extras.get("configured") is False

    # Skill-side helper agrees when fed the real-daemon JSON.
    from browserwright import daemon_client, install

    def _patched_doctor(self):
        return blob

    daemon_client.DaemonClient.doctor = _patched_doctor
    try:
        assert install._cloud_backend_available() is False
        entry = install._cloud_backend_entry()
        assert entry is not None
        assert entry.get("name") == "cloud"
        assert entry.get("available") is False
    finally:
        from browserwright import daemon_client as _dc
        import importlib
        importlib.reload(_dc)


# ---- option 5 — oauth2 deferred to v0.6 -----------------------------


def test_wizard_choice_5_oauth2_rejected_as_coming_v06(monkeypatch,
                                                       tmp_bs_home, capsys):
    """``oauth2`` is a recognised auth_kind name but daemon-side support
    is v0.6 work. The wizard must reject with a version-specific hint
    instead of a generic "unknown auth_kind" so the user knows when to
    expect it."""
    inputs = ["4", "browser-use", "oauth2"]
    rc = _drive_wizard(monkeypatch, inputs, cloud_live=True)
    err = capsys.readouterr().err
    assert rc == 1
    assert "oauth2" in err
    assert "v0.6" in err
    # Critical: don't pretend it's an unknown auth_kind — that would
    # mislead users who typed it deliberately.
    assert "unknown auth_kind" not in err


# ---- option 5 — extras-prefill from doctor --------------------------


def test_wizard_prefills_from_doctor_extras(monkeypatch, tmp_bs_home,
                                            tmp_path, capsys):
    """When daemon's doctor reports an existing cloud config in
    ``extras``, the wizard uses those values as prompt defaults. The
    user can re-run install and just press Enter through all fields if
    nothing needs to change."""
    extras = {
        "provider": "hyperbrowser",
        "endpoint": "wss://existing.hyperbrowser.example/cdp",
        "auth_kind": "bearer",
        "token_env": "EXISTING_HYPER_KEY",
        "configured": True,
    }
    # User presses Enter through every prompt to accept the defaults
    # (provider, auth_kind, envvar, endpoint), then confirms persist.
    inputs = ["4", "", "", "", "", "y"]
    rc = _drive_wizard(monkeypatch, inputs, cloud_live=True,
                       cloud_extras=extras, tmp_path=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0

    from browserwright.memory.global_mem import global_memory
    daemon_fm = global_memory().read().get("frontmatter", {}).get("daemon", {})
    assert daemon_fm.get("cloud_provider_hint") == "hyperbrowser"
    assert daemon_fm.get("cloud_auth_kind") == "bearer"
    assert daemon_fm.get("cloud_token_env") == "EXISTING_HYPER_KEY"
    assert daemon_fm.get("cloud_endpoint") == \
        "wss://existing.hyperbrowser.example/cdp"


# ---- config.toml writer in isolation --------------------------------


def test_write_daemon_cloud_config_creates_subtable_layout(monkeypatch, tmp_path):
    target = tmp_path / "config.toml"
    monkeypatch.setenv("BS_DAEMON_CONFIG_PATH", str(target))
    from browserwright import install
    path = install._write_daemon_cloud_config(
        "browser-use", "bearer",
        {"cloud_provider_hint": "browser-use",
         "cloud_auth_kind": "bearer",
         "cloud_token_env": "BROWSER_USE_API_KEY",
         "cloud_endpoint": "wss://api.browser-use.example/ws"},
    )
    assert path == target
    txt = target.read_text(encoding="utf-8")
    # Top section + auth subtable both present.
    assert "[backends.cloud]" in txt
    assert "[backends.cloud.auth.bearer]" in txt
    # Top section: endpoint + auth_kind + provider_hint (NOT provider).
    assert 'provider_hint = "browser-use"' in txt
    assert 'provider =' not in txt   # the old wrong field name must be gone
    assert 'auth_kind = "bearer"' in txt
    assert 'endpoint = "wss://api.browser-use.example/ws"' in txt
    # Auth subtable: kind-specific credential reference.
    assert 'token_env = "BROWSER_USE_API_KEY"' in txt


def test_write_daemon_cloud_config_basic_emits_username_password_envs(
        monkeypatch, tmp_path):
    """Basic auth subtable must carry username_env + password_env per
    daemon ``BasicAuth`` (header mode). URL stays credential-free."""
    target = tmp_path / "config.toml"
    monkeypatch.setenv("BS_DAEMON_CONFIG_PATH", str(target))
    from browserwright import install
    install._write_daemon_cloud_config(
        "browserless", "basic",
        {"cloud_provider_hint": "browserless",
         "cloud_auth_kind": "basic",
         "cloud_username_env": "BROWSERLESS_USER",
         "cloud_password_env": "BROWSERLESS_PASS",
         "cloud_endpoint": "wss://chrome.browserless.io/ws"},
    )
    txt = target.read_text(encoding="utf-8")
    assert "[backends.cloud.auth.basic]" in txt
    assert 'username_env = "BROWSERLESS_USER"' in txt
    assert 'password_env = "BROWSERLESS_PASS"' in txt
    # The bare endpoint is in the top section, not in the auth subtable.
    assert 'endpoint = "wss://chrome.browserless.io/ws"' in txt
    # No bearer / mtls fields in the file.
    assert "token_env" not in txt
    assert "cert_file" not in txt


def test_write_daemon_cloud_config_mtls_uses_file_field_names(monkeypatch,
                                                               tmp_path):
    """mTLS subtable uses ``cert_file`` + ``key_file`` (matches daemon
    ``MtlsAuth`` dataclass fields), not the old ``cert_path``/``key_path``."""
    target = tmp_path / "config.toml"
    monkeypatch.setenv("BS_DAEMON_CONFIG_PATH", str(target))
    from browserwright import install
    install._write_daemon_cloud_config(
        "hyperbrowser", "mtls",
        {"cloud_provider_hint": "hyperbrowser",
         "cloud_auth_kind": "mtls",
         "cloud_cert_file": "/opt/certs/client.crt",
         "cloud_key_file": "/opt/certs/client.key",
         "cloud_endpoint": "wss://api.hyperbrowser.example/cdp"},
    )
    txt = target.read_text(encoding="utf-8")
    assert "[backends.cloud.auth.mtls]" in txt
    assert 'cert_file = "/opt/certs/client.crt"' in txt
    assert 'key_file = "/opt/certs/client.key"' in txt
    # Old field names must not appear anywhere.
    assert "cert_path" not in txt
    assert "key_path" not in txt


def test_write_daemon_cloud_config_strips_stale_auth_subtables(monkeypatch,
                                                                tmp_path):
    """Switching auth_kind (e.g. bearer → mtls) must wipe the previous
    auth subtable so daemon doesn't see two conflicting auth configs."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[backends.cloud]\n'
        'auth_kind = "bearer"\n'
        'provider_hint = "old-provider"\n\n'
        '[backends.cloud.auth.bearer]\n'
        'token_env = "OLD_TOKEN_ENV"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BS_DAEMON_CONFIG_PATH", str(target))
    from browserwright import install
    install._write_daemon_cloud_config(
        "hyperbrowser", "mtls",
        {"cloud_provider_hint": "hyperbrowser",
         "cloud_auth_kind": "mtls",
         "cloud_cert_file": "/a.crt", "cloud_key_file": "/a.key"},
    )
    txt = target.read_text(encoding="utf-8")
    # Old bearer subtable replaced by mtls.
    assert "[backends.cloud.auth.mtls]" in txt
    assert "[backends.cloud.auth.bearer]" not in txt
    assert "OLD_TOKEN_ENV" not in txt


def test_write_daemon_cloud_config_preserves_other_sections(monkeypatch,
                                                            tmp_path):
    """The wizard must not clobber sections it doesn't own. Only
    ``[backends.cloud]`` and ``[backends.cloud.auth.*]`` are the
    wizard's; everything else (e.g. ``[server]``, ``[logging]``,
    ``[backends.rdp]``) stays untouched."""
    target = tmp_path / "config.toml"
    target.write_text(
        '[server]\nport = 9333\n\n'
        '[logging]\nlevel = "debug"\n\n'
        '[backends.rdp]\nport = 9222\n\n'
        '[backends.cloud]\n# old cloud block — replaced wholesale\n'
        'provider_hint = "old-provider"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BS_DAEMON_CONFIG_PATH", str(target))
    from browserwright import install
    install._write_daemon_cloud_config(
        "browserless", "basic",
        {"cloud_provider_hint": "browserless",
         "cloud_auth_kind": "basic",
         "cloud_username_env": "BL_USER",
         "cloud_password_env": "BL_PASS",
         "cloud_endpoint": "wss://chrome.browserless.io/ws"},
    )
    txt = target.read_text(encoding="utf-8")
    # Old non-cloud sections preserved verbatim.
    assert "[server]" in txt and "port = 9333" in txt
    assert "[logging]" in txt and 'level = "debug"' in txt
    assert "[backends.rdp]" in txt and "port = 9222" in txt
    # New cloud block replaced the old one wholesale.
    assert 'provider_hint = "browserless"' in txt
    assert "old-provider" not in txt
    # Only one [backends.cloud] section. (Note: the substring
    # "[backends.cloud]" appears as a prefix of "[backends.cloud.auth.basic]"
    # too, so we count lines whose stripped form matches exactly.)
    cloud_top_lines = [ln for ln in txt.splitlines()
                       if ln.strip() == "[backends.cloud]"]
    assert len(cloud_top_lines) == 1
