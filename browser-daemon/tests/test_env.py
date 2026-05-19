"""env backend — BD_CDP_WS / BD_CDP_URL / BU_* compat alias."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from browser_daemon.backends.env import EnvBackend
from browser_daemon.config import load
from browser_daemon.errors import Unavailable


def _make(env: dict[str, str]) -> EnvBackend:
    cfg = load(env=env)
    return EnvBackend(cfg)


@pytest.mark.asyncio
async def test_bd_cdp_ws_pure_passthrough_no_rewrite():
    """The whole point of env is: trust whatever the caller injected. The
    backend may NOT mutate the URL — query strings, basic auth, tokens all
    pass through byte-for-byte."""
    raw = "wss://example.com/cdp?api_key=secret-123&region=us-east-1"
    backend = _make({"BD_CDP_WS": raw})

    res = await backend.resolve(timeout=5)
    assert res.ws_url == raw
    assert res.backend == "env"


@pytest.mark.asyncio
async def test_bu_cdp_ws_compat_alias_with_deprecation_hint():
    """BU_* legacy names from browser-harness must still work, but doctor's
    `detail` must call out the migration."""
    raw = "ws://127.0.0.1:9999/devtools/browser/abc"
    backend = _make({"BU_CDP_WS": raw})

    res = await backend.resolve(timeout=5)
    assert res.ws_url == raw

    probe = await backend.probe()
    assert probe.available is True
    assert "BU_CDP_WS" in probe.detail
    assert "deprecated" in probe.detail.lower()


@pytest.mark.asyncio
async def test_neither_env_var_set_is_unavailable():
    backend = _make({})
    probe = await backend.probe()
    assert probe.available is False
    assert "BD_CDP_WS" in probe.detail
    assert probe.ws_url is None  # spec §5.2: never opens ws

    with pytest.raises(Unavailable):
        await backend.resolve(timeout=5)


@pytest.mark.asyncio
async def test_bd_cdp_url_resolves_via_json_version(monkeypatch):
    """When BD_CDP_URL is set, env walks /json/version like rdp does — but
    against an arbitrary host instead of 127.0.0.1:9222."""
    expected_ws = "wss://cloud.example.com/cdp/session-42"

    class FakeResp:
        status_code = 200

        def json(self):
            return {"webSocketDebuggerUrl": expected_ws}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            assert url == "https://cloud.example.com/json/version"
            return FakeResp()

    monkeypatch.setattr("browser_daemon.backends.env.httpx.AsyncClient", FakeClient)

    backend = _make({"BD_CDP_URL": "https://cloud.example.com"})
    res = await backend.resolve(timeout=5)
    assert res.ws_url == expected_ws


@pytest.mark.asyncio
async def test_bd_cdp_url_404_raises_unavailable(monkeypatch):
    class FakeResp:
        status_code = 404
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url): return FakeResp()

    monkeypatch.setattr("browser_daemon.backends.env.httpx.AsyncClient", FakeClient)

    backend = _make({"BD_CDP_URL": "https://nope.example.com"})
    with pytest.raises(Unavailable):
        await backend.resolve(timeout=5)


@pytest.mark.asyncio
async def test_bd_overrides_bu_when_both_set():
    """BD_* must win when both are present — defends against a half-finished
    migration leaving stale BU_* values in shell rc files."""
    backend = _make({
        "BD_CDP_WS": "ws://winner:9222/",
        "BU_CDP_WS": "ws://loser:9333/",
    })
    res = await backend.resolve(timeout=5)
    assert res.ws_url == "ws://winner:9222/"


# ---- v0.4.1 Bug 1: BD_RDP_PORT env var support ---------------------------


def test_bd_rdp_port_env_sets_rdp_port():
    """Before v0.4.1 the rdp port was config-file + `--port` only. The
    `BD_RDP_PORT` env var was added so the ai-e2e harness can lock to its
    isolated Chrome's port WITHOUT a config file. Without it, callers reached
    for `BD_BACKEND=rdp` (no port) → silently fell to the 9222 default →
    user's daily Chrome → Allow popup (P1 framework bug surfaced via
    agent-sdk-tester 2026-05-18 LIVE run)."""
    cfg = load(env={"BD_RDP_PORT": "9444"})
    assert cfg.backends.rdp.port == 9444


def test_bd_rdp_port_invalid_value_raises_user_error():
    from browser_daemon.errors import UserError
    with pytest.raises(UserError) as exc:
        load(env={"BD_RDP_PORT": "not-a-number"})
    assert "BD_RDP_PORT" in str(exc.value)


def test_cli_port_flag_wins_over_bd_rdp_port():
    """Precedence: CLI `--port` > BD_RDP_PORT > toml > default. The CLI flag
    is highest because users expect a flag they typed to override env."""
    cfg = load(env={"BD_RDP_PORT": "9444"}, cli_port=51234)
    assert cfg.backends.rdp.port == 51234


def test_bd_rdp_port_overrides_toml_default():
    """env wins over the 9222 default (no toml loaded here = default in play)."""
    cfg = load(env={"BD_RDP_PORT": "9444"})
    assert cfg.backends.rdp.port != 9222
    assert cfg.backends.rdp.port == 9444


# ---- v0.5.2 Bug 5: default_backend in config.toml ------------------------


def test_default_backend_toml_key_is_parsed(tmp_path, monkeypatch):
    """Task #14: README has advertised `default_backend = "..."` in
    config.toml since v0.1, but the parser silently ignored it. Users
    wrote it expecting the auto chain to lock onto one backend, and
    instead saw it fall back as if no preference were set."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('default_backend = "autoconnect"\n')
    cfg = load(env={"BD_CONFIG": str(cfg_file)})
    assert cfg.backend == "autoconnect"


def test_env_BD_BACKEND_overrides_toml_default_backend(tmp_path):
    """Precedence: BD_BACKEND env wins over toml default_backend."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('default_backend = "autoconnect"\n')
    cfg = load(env={"BD_CONFIG": str(cfg_file), "BD_BACKEND": "rdp"})
    assert cfg.backend == "rdp"


def test_cli_backend_overrides_both_env_and_toml(tmp_path):
    """Precedence: CLI --backend wins over both BD_BACKEND and toml."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('default_backend = "autoconnect"\n')
    cfg = load(
        env={"BD_CONFIG": str(cfg_file), "BD_BACKEND": "rdp"},
        cli_backend="env",
    )
    assert cfg.backend == "env"


def test_default_backend_unset_keeps_auto_chain():
    """When nothing sets backend, cfg.backend stays None — resolver falls
    back through env/rdp/autoconnect order. Sanity to make sure we didn't
    accidentally default the backend to something."""
    cfg = load(env={})
    assert cfg.backend is None


# ---- v0.5.3 F-4c: BD_PORT typo alias + deprecation warning ----------------


def test_bd_port_typo_aliases_to_bd_rdp_port(capsys):
    """REVIEW.md F-4c: the v0.4 incident root cause was `BD_PORT=9444`
    being silently ignored. We now treat BD_PORT as a deprecated alias —
    value flows through to rdp.port, deprecation warning on stderr."""
    cfg = load(env={"BD_PORT": "9444"})
    assert cfg.backends.rdp.port == 9444
    captured = capsys.readouterr()
    assert "BD_PORT" in captured.err
    assert "deprecated" in captured.err.lower()


def test_bd_rdp_port_wins_over_bd_port_alias(capsys):
    """Precedence: when BOTH are set, the canonical BD_RDP_PORT wins.
    No deprecation warning fires (user clearly knows about the canonical
    name; nothing to warn about)."""
    cfg = load(env={"BD_PORT": "9444", "BD_RDP_PORT": "51234"})
    assert cfg.backends.rdp.port == 51234
    captured = capsys.readouterr()
    assert "deprecated" not in captured.err.lower()


def test_bd_port_alias_value_must_be_integer():
    from browser_daemon.errors import UserError
    with pytest.raises(UserError) as exc:
        load(env={"BD_PORT": "not-a-port"})
    # Error mentions both names so the user sees the migration path.
    msg = str(exc.value)
    assert "BD_PORT" in msg and "BD_RDP_PORT" in msg


def test_bd_port_quiet_env_suppresses_deprecation_warning(capsys):
    """Test seam — keeps test output clean when intentionally setting BD_PORT."""
    cfg = load(env={"BD_PORT": "9444", "BD_PORT_QUIET": "1"})
    assert cfg.backends.rdp.port == 9444
    captured = capsys.readouterr()
    assert "deprecated" not in captured.err.lower()


# ---- v0.5.3 F-5: [backends.autoconnect].profile_paths + .relay_url -------


def test_toml_autoconnect_profile_paths_is_parsed(tmp_path):
    """README has advertised `[backends.autoconnect].profile_paths` since
    v0.1 but parser silently ignored it. v0.5.3 parses + prepends to the
    platform default list (so user-supplied paths take precedence)."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("""\
[backends.autoconnect]
profile_paths = ["~/custom/chrome", "/tmp/other-chrome"]
""")
    cfg = load(env={"BD_CONFIG": str(cfg_file)})
    assert cfg.backends.autoconnect.profile_paths == [
        "~/custom/chrome", "/tmp/other-chrome",
    ]


def test_toml_autoconnect_profile_paths_default_empty():
    cfg = load(env={})
    assert cfg.backends.autoconnect.profile_paths == []


def test_toml_extension_relay_url_is_parsed(tmp_path):
    """README has advertised `[backends.extension].relay_url` since v0.4
    but parser silently ignored it. v0.5.3 parses; the ExtensionBackend
    + listener honor the port to coexist with e.g. playwriter on 19988."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("""\
[backends.extension]
relay_url = "ws://127.0.0.1:29988"
""")
    cfg = load(env={"BD_CONFIG": str(cfg_file)})
    assert cfg.backends.extension.relay_url == "ws://127.0.0.1:29988"


def test_extension_backend_uses_custom_relay_port_from_config(tmp_path):
    """ExtensionBackend ctor reads the config and binds to the alt port
    instead of DEFAULT_RELAY_PORT."""
    from browser_daemon.backends.extension import ExtensionBackend
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("""\
[backends.extension]
relay_url = "ws://127.0.0.1:29988"
""")
    cfg = load(env={"BD_CONFIG": str(cfg_file)})
    backend = ExtensionBackend(cfg)
    assert backend._port == 29988  # noqa: SLF001 (test seam)


def test_autoconnect_scan_prepends_custom_profile_paths(tmp_path, monkeypatch):
    """End-to-end: a DevToolsActivePort under the user's custom profile_paths
    must be found by `_scan_profiles`."""
    from browser_daemon.backends import autoconnect as ac_mod
    from pathlib import Path

    custom = tmp_path / "custom-profile"
    custom.mkdir()
    port_file = custom / "DevToolsActivePort"
    port_file.write_text("12345\n/devtools/browser/custom-uuid\n")

    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(f"""\
[backends.autoconnect]
profile_paths = [{str(custom)!r}]
""")
    cfg = load(env={"BD_CONFIG": str(cfg_file)})

    # Stub platform defaults to NOT include the custom dir, proving the
    # prepend is what reaches the scan.
    monkeypatch.setattr(ac_mod, "profile_paths", lambda: [])
    backend = ac_mod.AutoconnectBackend(cfg)
    extras = backend._extra_paths()  # noqa: SLF001
    assert custom in extras

    scan = ac_mod._scan_profiles(extra=extras)
    assert any(s.port == "12345" for s in scan)


# ---- v0.5.3 Task #24: extension port precedence (CLI > env > toml port
# > toml relay_url > 19989 default). Unblocks F-4e (ai-e2e harness can't
# bind 19988 when playwriter occupies it; default 19989 sidesteps this). ---


def test_extension_port_default_is_19989():
    """No config / no env / no CLI → fall back to DEFAULT_RELAY_PORT."""
    from browser_daemon.server.relay import DEFAULT_RELAY_PORT
    cfg = load(env={})
    host, port = cfg.backends.extension.resolved_host_port()
    assert host == "127.0.0.1"
    assert port == DEFAULT_RELAY_PORT
    assert port == 19989


def test_extension_port_toml_relay_url_only():
    """toml `relay_url = ws://127.0.0.1:29988` → both host + port from URL."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write('[backends.extension]\nrelay_url = "ws://10.0.0.5:29988"\n')
        path = f.name
    cfg = load(env={"BD_CONFIG": path})
    host, port = cfg.backends.extension.resolved_host_port()
    assert host == "10.0.0.5"
    assert port == 29988


def test_extension_port_toml_port_overrides_relay_url_port():
    """toml port + toml relay_url both set: explicit `port` wins; `host`
    still comes from `relay_url` (lets user mix: 'use the URL's host but
    a different port')."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(
            '[backends.extension]\n'
            'relay_url = "ws://10.0.0.5:29988"\n'
            'port = 31415\n'
        )
        path = f.name
    cfg = load(env={"BD_CONFIG": path})
    host, port = cfg.backends.extension.resolved_host_port()
    assert host == "10.0.0.5"
    assert port == 31415


def test_extension_port_env_var_overrides_toml(tmp_path):
    """BD_EXTENSION_PORT env wins over toml `port`."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('[backends.extension]\nport = 20000\n')
    cfg = load(env={"BD_CONFIG": str(cfg_file), "BD_EXTENSION_PORT": "30000"})
    _, port = cfg.backends.extension.resolved_host_port()
    assert port == 30000


def test_extension_port_cli_flag_overrides_env_and_toml(tmp_path):
    """CLI `--extension-port N` is the top of the precedence stack."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('[backends.extension]\nport = 20000\n')
    cfg = load(
        env={"BD_CONFIG": str(cfg_file), "BD_EXTENSION_PORT": "30000"},
        cli_extension_port=40000,
    )
    _, port = cfg.backends.extension.resolved_host_port()
    assert port == 40000


def test_extension_port_env_invalid_raises_user_error():
    from browser_daemon.errors import UserError
    with pytest.raises(UserError) as exc:
        load(env={"BD_EXTENSION_PORT": "not-a-port"})
    assert "BD_EXTENSION_PORT" in str(exc.value)


# ---- v0.5.3 F-9: coverage gaps -------------------------------------------


def test_bug5_bd_rdp_port_env_wins_over_toml_port(tmp_path):
    """F-9 / bug #5: directly verify TOML-vs-env precedence for the port
    knob (previous suite only tested it for default_backend). Env wins."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("""\
[backends.rdp]
port = 12000
""")
    cfg = load(env={"BD_CONFIG": str(cfg_file), "BD_RDP_PORT": "13000"})
    assert cfg.backends.rdp.port == 13000


def test_bug5_cli_port_wins_over_env_wins_over_toml(tmp_path):
    """Full precedence stack for the rdp port: CLI > env > toml > 9222."""
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('[backends.rdp]\nport = 12000\n')
    cfg = load(
        env={"BD_CONFIG": str(cfg_file), "BD_RDP_PORT": "13000"},
        cli_port=14000,
    )
    assert cfg.backends.rdp.port == 14000


@pytest.mark.parametrize("env_value", ["1", "true", "True", "TRUE", "yes",
                                       "YES", "on", "ON", "y", "Y"])
def test_bug11_allow_default_profile_env_truthy_variants_unlock(env_value, monkeypatch):
    """F-9 / bug #11: previously only `1`/`true`/`True` unlocked the guard;
    `yes`/`on`/`TRUE` (case-insensitive) silently fell through as False.
    Now uses common truthy parser."""
    from browser_daemon.launch_chrome import _truthy_env
    monkeypatch.setenv("X_TRUTHY", env_value)
    assert _truthy_env("X_TRUTHY") is True


@pytest.mark.parametrize("env_value", ["", "0", "false", "no", "off", "n"])
def test_bug11_allow_default_profile_env_falsy_variants_stay_locked(env_value, monkeypatch):
    from browser_daemon.launch_chrome import _truthy_env
    monkeypatch.setenv("X_TRUTHY", env_value)
    assert _truthy_env("X_TRUTHY") is False


def test_bug11_allow_default_profile_env_unset_is_false(monkeypatch):
    from browser_daemon.launch_chrome import _truthy_env
    monkeypatch.delenv("X_TRUTHY", raising=False)
    assert _truthy_env("X_TRUTHY") is False


# ---- F-9 #14: invalid default_backend value -----------------------------


def test_bug14_default_backend_garbage_string_raises_at_resolve_time(tmp_path):
    """F-9 / bug #14: `default_backend = "garbage"` in config.toml gets
    stored as cfg.backend (we don't validate names at config-time to dodge
    a circular import on `backends.names`). Surface comes at resolve-time
    as UserError. Test ensures that path isn't silently dropping anyone."""
    import asyncio
    from browser_daemon.errors import UserError
    from browser_daemon.resolver import resolve

    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('default_backend = "nonexistent-backend"\n')
    cfg = load(env={"BD_CONFIG": str(cfg_file)})
    # Config-time: stored as-is.
    assert cfg.backend == "nonexistent-backend"
    # Resolve-time: raises with the canonical "unknown backend" message.
    with pytest.raises(UserError) as exc:
        asyncio.run(resolve(cfg))
    assert "unknown backend" in str(exc.value).lower()
    assert "nonexistent-backend" in str(exc.value)


def test_bug14_default_backend_integer_raises_at_config_time(tmp_path):
    """F-9 / bug #14 part 2: a non-string default_backend value (e.g., an
    integer typo) was silently ignored before. Now config-time raises with
    a clear type-mismatch message — no need to defer to resolve-time."""
    from browser_daemon.errors import UserError
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('default_backend = 42\n')
    with pytest.raises(UserError) as exc:
        load(env={"BD_CONFIG": str(cfg_file)})
    assert "default_backend" in str(exc.value)
    assert "string" in str(exc.value).lower()


def test_bug14_default_backend_bool_also_rejected(tmp_path):
    """toml's `true` / `false` literals are also rejected — same code path."""
    from browser_daemon.errors import UserError
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text('default_backend = true\n')
    with pytest.raises(UserError):
        load(env={"BD_CONFIG": str(cfg_file)})
