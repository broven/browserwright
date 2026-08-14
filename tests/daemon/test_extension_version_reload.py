from __future__ import annotations

import json

import pytest

from browserwright import version as version_mod
from browserwright.daemon import cli as cli_mod
from browserwright.daemon.server.relay import RelayServer, _ExtensionConn
from browserwright.version import VersionDrift, compare_versions


def test_compare_versions_classifies_semver_drift():
    assert compare_versions("1.2.3", "1.2.3").drift == VersionDrift.EQUAL
    assert compare_versions("1.2.3", "1.2.4").drift == VersionDrift.PATCH
    assert compare_versions("1.2.3", "1.3.0").drift == VersionDrift.MINOR
    assert compare_versions("1.2.3", "2.0.0").drift == VersionDrift.MAJOR
    unknown = compare_versions("dev", "1.2.3")
    assert unknown.drift == VersionDrift.UNKNOWN
    assert unknown.order is None


@pytest.mark.asyncio
async def test_relay_hello_ack_status_and_guarded_reload(monkeypatch):
    monkeypatch.setattr("browserwright.daemon.server.relay.__version__", "1.2.4")
    relay = RelayServer()
    sent: list[dict] = []

    class FakeConn:
        async def send(self, text: str):
            sent.append(json.loads(text))

    ext = _ExtensionConn(conn=FakeConn())
    relay._extensions["tmp"] = ext

    await relay._dispatch_from_extension(
        ext,
        "tmp",
        {
            "type": "hello",
            "installId": "install-1",
            "browser": "chrome",
            "version": "1.2.3",
            "browserwrightVersion": "1.2.3",
            "extensionProtocolVersion": version_mod.EXTENSION_PROTOCOL_VERSION,
        },
    )

    assert sent[0]["type"] == "helloAck"
    assert sent[0]["daemonVersion"] == "1.2.4"
    assert sent[0]["versionDrift"] == "patch"
    assert sent[1]["type"] == "reloadExtension"
    assert sent[1]["reason"] == "version_drift"
    assert sent[1]["expectedVersion"] == "1.2.4"

    status = relay.status_payload()
    assert status["daemon_version"] == "1.2.4"
    assert status["extensions"] == 1
    assert status["extension_details"][0]["version_drift"] == "patch"

    await relay._dispatch_from_extension(
        ext,
        "tmp",
        {
            "type": "hello",
            "installId": "install-1",
            "browser": "chrome",
            "version": "1.2.3",
            "browserwrightVersion": "1.2.3",
            "extensionProtocolVersion": version_mod.EXTENSION_PROTOCOL_VERSION,
        },
    )
    assert [msg["type"] for msg in sent].count("reloadExtension") == 1


@pytest.mark.asyncio
async def test_manual_reload_broadcasts_without_guard(monkeypatch):
    monkeypatch.setattr("browserwright.daemon.server.relay.__version__", "2.0.0")
    relay = RelayServer()
    sent: list[dict] = []

    # D: reload verification would otherwise wait the full verify timeout for
    # a replacement SW; this unit test covers the broadcast, not the wait.
    async def _no_replacement(*_a, **_kw):
        return None

    monkeypatch.setattr(RelayServer, "_wait_for_replacement", _no_replacement)

    class FakeConn:
        async def send(self, text: str):
            sent.append(json.loads(text))

    ext = _ExtensionConn(
        conn=FakeConn(),
        install_id="ready",
        browser="chrome",
        version="1.0.0",
        browserwright_version="1.0.0",
    )
    ext.hello_received.set()
    relay._extensions["ready"] = ext

    result = await relay.reload_extensions(reason="manual")

    assert result["sent"] == 1
    assert result["ok"] is False  # no replacement SW came back (D)
    assert result["dead"][0]["install_id"] == "ready"
    assert sent == [
        {
            "type": "reloadExtension",
            "reason": "manual",
            "expectedVersion": "2.0.0",
        }
    ]


def test_daemon_extension_reload_cli_dispatches_rpc(monkeypatch, capsys):
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(cfg, method, params, **kwargs):
        calls.append((method, params))
        return {"ok": True, "sent": 2, "reconnected": 2, "dead": [],
                "extensions": []}

    monkeypatch.setattr(cli_mod, "_rpc_via_ws", fake_rpc)

    assert cli_mod.main(["extension", "reload"]) == 0
    assert calls == [("BrowserwrightDaemon.extension.reload", {"reason": "manual"})]
    assert "reload complete: 2 of 2 extension(s) reconnected" in \
        capsys.readouterr().out


def test_daemon_extension_reload_cli_reports_dead_sw(monkeypatch, capsys):
    """D: when a reloaded SW does not come back, the CLI says so explicitly
    (exit 1 + manual recovery guidance) instead of claiming success."""
    async def fake_rpc(cfg, method, params, **kwargs):
        return {
            "ok": False, "sent": 1, "reconnected": 0,
            "dead": [{"install_id": "i1", "version": "1.0.0"}],
            "extensions": [],
        }

    monkeypatch.setattr(cli_mod, "_rpc_via_ws", fake_rpc)

    assert cli_mod.main(["extension", "reload"]) == 1
    err = capsys.readouterr().err
    assert "did not come back after reload" in err
    assert "chrome://extensions" in err
    assert "browserwright doctor" in err
