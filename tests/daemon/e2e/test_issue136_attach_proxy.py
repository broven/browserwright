"""#136: a remote `--attach` takes its proxy from the environment of the CLI
that opens the session — never from the daemon's — for discovery, the
session's CDP websocket, and the Playwright facade bridge alike.

The rig, all on this machine:

- a real Chrome on the test CDP port, reached through a TCP forwarder bound to
  a **non-loopback** address of this host, so it counts as remote (loopback
  targets are always direct);
- a recording HTTP proxy (absolute-form GET + CONNECT) and a recording SOCKS5
  proxy, each logging every target it tunnels to;
- the test daemon started with `all_proxy` / `http_proxy` pointing at a dead
  port. Before #136 that alone crashed every attach (`socksio` missing) or
  sent it nowhere.

Each scenario runs the real CLI: `session new --attach=<url>`, one `-e` that
drives the page through Playwright, `session end`. The artifact is
`issue136-proxy-routes.json` in the e2e artifacts dir: which proxy saw which
connections, per scenario.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from .conftest import TEST_CDP_PORT, published_endpoint, scrubbed_env

_BS_HOME = Path(__file__).resolve().parent / "_bs_home" / "cdp"
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_DEAD = "127.0.0.1:9"  # discard port: nothing listens


def _non_loopback_ip() -> str | None:
    """An address of this host that is not loopback, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


class _Rig:
    """Forwarder + two recording proxies on one background event loop."""

    def __init__(self, lan_ip: str):
        self.lan_ip = lan_ip
        self.loop = asyncio.new_event_loop()
        self.seen: dict[str, list[str]] = {"http": [], "socks": []}
        self._servers: list[asyncio.base_events.Server] = []
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.fwd_port = self._start(self._forward, lan_ip)
        self.http_port = self._start(self._http_proxy, "127.0.0.1")
        self.socks_port = self._start(self._socks_proxy, "127.0.0.1")

    def _start(self, handler, host: str) -> int:
        async def _serve():
            srv = await asyncio.start_server(handler, host, 0)
            self._servers.append(srv)
            return srv.sockets[0].getsockname()[1]
        return asyncio.run_coroutine_threadsafe(_serve(), self.loop).result(5)

    def close(self) -> None:
        for srv in self._servers:
            self.loop.call_soon_threadsafe(srv.close)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)

    @staticmethod
    async def _pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _splice(self, r1, w1, r2, w2):
        await asyncio.gather(self._pipe(r1, w2), self._pipe(r2, w1))

    async def _forward(self, reader, writer):
        """LAN address -> the Chrome on loopback."""
        ur, uw = await asyncio.open_connection("127.0.0.1", TEST_CDP_PORT)
        await self._splice(reader, writer, ur, uw)

    async def _http_proxy(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            self.seen["http"].append(f"CONNECT {target}")
            ur, uw = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:  # absolute-form: GET http://host:port/path HTTP/1.1
            hostport = target.split("://", 1)[1].split("/", 1)[0]
            host, port = hostport.rsplit(":", 1)
            self.seen["http"].append(f"{method} {target}")
            ur, uw = await asyncio.open_connection(host, int(port))
            path = "/" + target.split("://", 1)[1].split("/", 1)[1]
            rest = head.split(b"\r\n", 1)[1]
            uw.write(f"{method} {path} HTTP/1.1\r\n".encode() + rest)
        await self._splice(reader, writer, ur, uw)

    async def _socks_proxy(self, reader, writer):
        ver, nmethods = await reader.readexactly(2)
        await reader.readexactly(nmethods)
        writer.write(b"\x05\x00")  # no auth
        _, cmd, _, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        self.seen["socks"].append(f"{host}:{port}")
        ur, uw = await asyncio.open_connection(host, port)
        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        await self._splice(reader, writer, ur, uw)

    def take(self) -> dict[str, list[str]]:
        out = {k: list(v) for k, v in self.seen.items()}
        for v in self.seen.values():
            v.clear()
        return out


@pytest.fixture
def daemon_env_has_dead_proxy(monkeypatch):
    """Inherited by the daemon `e2e_cdp_daemon` spawns (it copies os.environ).
    Must be requested before that fixture."""
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("all_proxy", f"socks5://{_DEAD}")
    monkeypatch.setenv("http_proxy", f"http://{_DEAD}")
    monkeypatch.setenv("https_proxy", f"http://{_DEAD}")


@pytest.fixture
def rig():
    ip = _non_loopback_ip()
    if ip is None:
        pytest.skip("this host has no non-loopback IPv4 address to act as remote")
    r = _Rig(ip)
    try:
        yield r
    finally:
        r.close()


def _cli(args: list[str], runtime_dir: str, proxy_env: dict[str, str],
         stdin: str | None = None, timeout: float = 120.0):
    env = {k: v for k, v in scrubbed_env().items() if k not in _PROXY_VARS}
    env.update(proxy_env)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BW_DAEMON_URL"] = published_endpoint(runtime_dir) or ""
    env["BS_HOME"] = str(_BS_HOME)
    assert env["BW_DAEMON_URL"], "test cdp daemon did not publish its endpoint"
    binary = Path(sys.executable).with_name("browserwright")
    return subprocess.run([str(binary), *args], input=stdin, capture_output=True,
                          text=True, env=env, timeout=timeout)


_SCRIPT = 'page.goto("data:text/html,<title>via-136</title>")\nprint(page.title())\n'


def _drive(rig: _Rig, runtime_dir: str, name: str, proxy_env: dict[str, str]):
    """session new --attach=<remote> → one -e through Playwright → session end.

    Returns (new, exec, list_json) CompletedProcesses."""
    attach = f"http://{rig.lan_ip}:{rig.fwd_port}"
    new = _cli(["session", "new", "--backend=cdp", f"--attach={attach}",
                f"--name={name}"], runtime_dir, proxy_env)
    assert new.returncode == 0, new.stderr
    sid = new.stdout.strip().splitlines()[-1]
    try:
        listed = _cli(["session", "list", "--json"], runtime_dir, proxy_env)
        # A different env on the call must not matter: the proxy was pinned
        # when the session was opened.
        ran = _cli(["-s", sid, "--code-stdin"], runtime_dir, {}, stdin=_SCRIPT)
    finally:
        _cli(["session", "end", sid], runtime_dir, proxy_env)
    return new, ran, listed


def _remote(rig: _Rig) -> str:
    return f"{rig.lan_ip}:{rig.fwd_port}"


def test_attach_proxy_follows_the_callers_env(
        daemon_env_has_dead_proxy, e2e_cdp_daemon, rig, e2e_artifacts_dir):
    report: dict[str, object] = {"remote": _remote(rig)}

    # 1. HTTP proxy with credentials: discovery is an absolute-form GET, both
    #    websockets (session upstream + facade bridge) are CONNECT tunnels.
    proxy = f"http://user:s3cret@127.0.0.1:{rig.http_port}"
    new, ran, listed = _drive(rig, e2e_cdp_daemon, "p136-http", {"http_proxy": proxy})
    seen = rig.take()
    report["http_proxy"] = seen
    assert ran.returncode == 0, ran.stderr
    assert "via-136" in ran.stdout
    assert any(s.startswith("GET http://" + _remote(rig)) for s in seen["http"]), seen
    assert seen["http"].count(f"CONNECT {_remote(rig)}") >= 2, seen  # upstream + bridge
    assert seen["socks"] == []
    assert "s3cret" not in listed.stdout, "proxy credentials leaked into session list"

    # 2. SOCKS5 via all_proxy — the #136 shape. Every connection goes through it.
    new, ran, _ = _drive(rig, e2e_cdp_daemon, "p136-socks",
                         {"all_proxy": f"socks5://127.0.0.1:{rig.socks_port}"})
    seen = rig.take()
    report["socks5_all_proxy"] = seen
    assert ran.returncode == 0, ran.stderr
    assert seen["socks"].count(_remote(rig)) >= 3, seen  # discovery + upstream + bridge
    assert seen["http"] == []

    # 3. Same SOCKS env, but NO_PROXY exempts the host by CIDR: all direct.
    cidr = ".".join(rig.lan_ip.split(".")[:3]) + ".0/24"
    new, ran, _ = _drive(rig, e2e_cdp_daemon, "p136-noproxy",
                         {"all_proxy": f"socks5://127.0.0.1:{rig.socks_port}",
                          "NO_PROXY": cidr})
    seen = rig.take()
    report["no_proxy_cidr"] = {"NO_PROXY": cidr, **seen}
    assert ran.returncode == 0, ran.stderr
    assert seen == {"http": [], "socks": []}, seen

    # 4. No proxy in the caller's env: direct, though the daemon's env has a
    #    dead SOCKS proxy (which used to crash discovery outright).
    new, ran, _ = _drive(rig, e2e_cdp_daemon, "p136-direct", {})
    seen = rig.take()
    report["caller_has_no_proxy"] = seen
    assert ran.returncode == 0, ran.stderr
    assert seen == {"http": [], "socks": []}, seen

    # 5. A dead proxy in the caller's env fails loudly and says how to bypass.
    new, ran, _ = _drive(rig, e2e_cdp_daemon, "p136-dead",
                         {"http_proxy": f"http://{_DEAD}"})
    report["dead_proxy_error"] = ran.stderr[-1500:]
    assert ran.returncode != 0
    assert "via proxy http://127.0.0.1:9" in ran.stderr, ran.stderr
    assert f"NO_PROXY={rig.lan_ip}" in ran.stderr, ran.stderr

    (e2e_artifacts_dir / "issue136-proxy-routes.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
