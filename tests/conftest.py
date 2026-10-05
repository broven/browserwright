"""Repo-wide wall between the test suite and the developer's *real* daemon.

Running `mise run test` used to kill the machine-global daemon and leave a
stray one behind. Two independent vectors, both of which had to be closed:

**A — a test spawns a real daemon on the production ports.**
`daemon_lifecycle.ensure` cold-starts `browserwright-daemon serve` on demand, and
with no `BD_EXTENSION_PORT` in the environment that binds **19989**, the port
the user's real Chrome extension dials. The new daemon then takes over the
control socket, the LaunchAgent-managed one exits with "already running", and
because launchd never revives a non-zero exit (see
`test_issue39_launchagent_keepalive`), the user's daemon is simply gone — while
a worktree daemon squats on the port until someone runs `mise run teardown`.

**B — a test reaches the real daemon's endpoint.** ADR-0011 collapsed every
client-facing transport onto one TCP port, so isolation is now **port-based**
rather than socket-path-based. Unpinned, `daemon_url()` resolves to
`http://127.0.0.1:19990` — the developer's live daemon — and an innocuous
`diagnose()` in a unit test would talk to it. We publish an endpoint state file
inside the isolated runtime dir naming a *dead* port, so every in-process
resolution lands there instead. It is deliberately the state file and not
`$BW_DAEMON_URL`: the env var is an **explicit** source, which would switch the
whole suite into ADR-0011's "never auto-start, connect failure is an error"
regime and change the behavior under test. The state file ranks below every
configured source and is not explicit, so tests see the default regime pointed
somewhere harmless.

Both were previously defended against *per test*: eight files monkeypatched
the cold-start entry point one by one, and `e2e/helpers.py` builds its own port
"isolation wall" and documents this exact hazard. Per-test opt-in means the
protection is only as good as the next author's memory, and two tests in
`test_coverage_cli_runtime.py` had already forgotten it. This makes the wall the
default for the whole suite instead.

`tests/daemon/e2e/` is exempt: it launches real daemons on purpose, through
subprocesses whose environment it controls itself (`TEST_EXT_PORT`, its own
runtime dirs), and in-process monkeypatching would not reach them anyway.

**C — a test writes into the global install's files.** Code under test that
*logs* or *persists* resolves its paths from the environment, and the
developer's shell resolves them to the same places the LaunchAgent daemon
does: `$TMPDIR/browserwright-daemon.log` (launchd hands the daemon the same
per-user `TMPDIR` as a login shell), `~/.browserwright/` (`$BS_HOME`: sessions
ledger, memory, site skills) and `~/.cache/browserwright-daemon/`
(`$XDG_CACHE_HOME`: persistent Chrome profiles, launchd logs). Nothing is
signalled, but `cli stop` / `restart` unit tests appended fake `LIFECYCLE`
lines (pids 4242, 2468, 111) to the attribution trail ADR-0012 rule 5 exists
for, and the issue-86 rebind tests rewrote the live sessions ledger. All three
variables are redirected below, and an audit hook refuses — and fails the test
for — any write that still lands on one of those paths, so a new hardcoded
default is caught by name rather than by someone reading the daemon log.

**Boundary.** This wall is in-process. A test that shells out to `browserwright`
/ `browserwright-daemon` gets a child whose own `daemon_lifecycle.ensure` we
cannot patch — such a child could still bind port 19989. It inherits the
redirected `XDG_RUNTIME_DIR` (that part *is* environmental), so it reads the
same dead-port endpoint state file and cannot reach the real daemon. No test in
the fast gate spawns one today; if you add one, pin `BD_EXTENSION_PORT` and
`--facade-port` in its child environment the way `e2e/helpers.py` does.

**Also out of scope:** a *different worktree* running its own suite concurrently.
Nothing in this process can stop that, and it looks identical from the outside —
if the daemon dies during a run, check whether the port holder belongs to
another checkout before suspecting this one.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile

import pytest


def _global_state_paths() -> tuple[str, ...]:
    """What the machine-global install owns on disk, resolved the way the
    developer's shell (and so the LaunchAgent daemon) resolves it.

    Evaluated once at import, before any fixture redirects the environment —
    afterwards `TMPDIR` & co. point into the test's own dirs and would no
    longer name the real thing. Entries ending in `/` are directory prefixes;
    the rest are exact files or filename prefixes.
    """
    home = os.path.expanduser("~")
    tmp = os.environ.get("TMPDIR") or "/tmp"
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    bs_home = os.path.expanduser(os.environ.get("BS_HOME", "~/.browserwright"))
    entries = {
        # Daemon log: `_ipc.log_path()` = {TMPDIR | /tmp}/browserwright-daemon.log
        os.path.join(os.path.realpath(tmp), "browserwright-daemon.log"),
        # Default runtime dir of a daemon with no XDG_RUNTIME_DIR (launchd's):
        # pid, endpoint state, already-running state, executor records.
        "/private/tmp/browserwright-daemon.",
        "/private/tmp/bw-exec-",
        os.path.realpath(bs_home) + "/",
        os.path.realpath(os.path.join(cache, "browserwright-daemon")) + "/",
        # `launchagent.LOG_DIR` is a literal `~/.cache/...`, not XDG-derived.
        os.path.realpath(os.path.join(home, ".cache", "browserwright-daemon")) + "/",
        os.path.realpath(os.path.join(
            home, "Library", "LaunchAgents", "com.browserwright-daemon.plist")),
    }
    return tuple(sorted(entries))


_GLOBAL_STATE = _global_state_paths()
#: Set by the autouse wall while a fast-gate test body runs; the hook is inert
#: otherwise (collection, e2e tests, other plugins).
_guard = {"armed": False, "busy": False, "hits": []}

#: Extension relay and the one TCP endpoint of the global daemon (AGENTS.md).
_PRODUCTION_PORTS = frozenset({19989, 19990})
_WRITE_FLAGS =os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_MUTATING_EVENTS = frozenset({
    "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.symlink", "os.link",
    "os.truncate", "os.utime", "os.chmod", "shutil.rmtree",
})


def _global_hit(path) -> str | None:
    try:
        p = os.fsdecode(path)
    except TypeError:
        return None  # an fd, or something else that is not a path
    if not p:
        return None
    rp = os.path.realpath(p)
    for entry in _GLOBAL_STATE:
        if rp.startswith(entry) or rp == entry.rstrip("/"):
            return rp
    return None


def _global_state_audit(event: str, args: tuple) -> None:
    """Refuse a write to global install state while a fast-gate test runs.

    Raises `PermissionError` so the write never happens (the code under test
    usually swallows `OSError`, which is why the leak went unnoticed), and
    records it so the wall's teardown fails the test by name.
    """
    if not _guard["armed"] or _guard["busy"]:
        return
    if event == "socket.connect":
        # Vector B backstop: the state-file pin does not cover a test that
        # sets an explicit `BW_DAEMON_URL` naming the production port.
        addr = args[1] if len(args) > 1 else None
        if (isinstance(addr, tuple) and len(addr) >= 2
                and addr[1] in _PRODUCTION_PORTS):
            _guard["hits"].append(f"socket.connect {addr[0]}:{addr[1]}")
            raise PermissionError(
                f"test isolation: connect to production port {addr[1]}")
        return
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
        candidates = (path,) if writes else ()
    elif event in _MUTATING_EVENTS:
        candidates = tuple(a for a in args[:2] if isinstance(a, (str, bytes, os.PathLike)))
    else:
        return
    _guard["busy"] = True
    try:
        for c in candidates:
            if (hit := _global_hit(c)):
                _guard["hits"].append(f"{event} {hit}")
                raise PermissionError(
                    f"test isolation: {event} on global install state {hit}")
    finally:
        _guard["busy"] = False


sys.addaudithook(_global_state_audit)


def _dead_port() -> int:
    """A port nothing is listening on: bound to learn the number, then closed.

    A closed port is exactly what the wall wants — a resolution that lands here
    fails fast and locally instead of reaching the developer's daemon.
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _is_e2e(item_path, rootpath) -> bool:
    try:
        parts = item_path.relative_to(rootpath).parts
    except ValueError:
        return False
    return len(parts) >= 3 and parts[:3] == ("tests", "daemon", "e2e")


@pytest.fixture(scope="session")
def isolated_runtime_dir() -> str:
    """A short-path runtime dir shared by the whole session.

    Deliberately under `/tmp` and not `tmp_path`: AF_UNIX `sun_path` has a hard
    104-byte budget, and pytest's tmp dirs live under
    `/private/var/folders/...`, which blows it. `_ipc.runtime_dir()` hardcodes
    `/tmp` for the same reason.
    """
    path = tempfile.mkdtemp(prefix="bw-test-rt-", dir="/tmp")
    # ADR-0011 vector B: pin the endpoint the whole suite resolves to. Written
    # here, in the runtime dir, because that is where `daemon_url` looks for it
    # and because a child process inherits the redirected dir for free.
    (pathlib.Path(path) / "browserwright-daemon.endpoint").write_text(
        json.dumps({"url": f"http://127.0.0.1:{_dead_port()}"}))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def isolated_state_dirs(tmp_path_factory) -> dict[str, str]:
    """Private stand-ins for the env vars global-state paths derive from
    (vector C). Long paths are fine here: nothing under them is a socket."""
    return {var: str(tmp_path_factory.mktemp(name)) for var, name in (
        ("TMPDIR", "bw-tmpdir"),
        ("BS_HOME", "bw-home"),
        ("XDG_CACHE_HOME", "bw-cache"),
    )}


def _verdict(**overrides):
    """A `DaemonVerdict` for a current daemon on the resolved endpoint — what
    the stubbed `daemon_lifecycle.ensure` returns. `overrides` builds any
    other verdict (e.g. ``state="down"``)."""
    from browserwright import daemon_lifecycle
    from browserwright.daemon_url import daemon_endpoint
    from browserwright.version import package_version

    fields = {"state": daemon_lifecycle.UP, "endpoint": daemon_endpoint(),
              "installed": package_version(), "detail": "stubbed",
              "pid": 4242, "version": package_version(),
              "probes": ("ours", "ours")}
    fields.update(overrides)
    return daemon_lifecycle.DaemonVerdict(**fields)


@pytest.fixture
def make_verdict():
    """Factory for `daemon_lifecycle.DaemonVerdict` stand-ins (see `_verdict`)."""
    return _verdict


@pytest.fixture(autouse=True)
def never_touch_the_global_daemon(request, monkeypatch, isolated_runtime_dir,
                                  isolated_state_dirs):
    """Autouse: no test may reach the real control socket or spawn a real daemon.

    Vector B is closed by pointing `XDG_RUNTIME_DIR` somewhere private, whose
    endpoint state file names a dead port — so every endpoint resolution in the
    suite lands nowhere instead of on the developer's daemon.

    Vector A is closed in two layers: the cold-start *entry points* become
    no-ops, so a test that merely wanders into them keeps working without having
    to know they exist; and the low-level detached spawn **raises**, so a future
    code path that reaches a real `Popen` fails loudly and by name instead of
    leaking a daemon nobody notices until an upgrade breaks.
    """
    if _is_e2e(request.path, request.config.rootpath):
        yield
        return

    monkeypatch.setenv("XDG_RUNTIME_DIR", isolated_runtime_dir)
    # Vector C: the daemon log, the sessions ledger and the Chrome-profile
    # cache resolve from these. A test that wants its own value still
    # overrides them with its own `monkeypatch.setenv`.
    for var, path in isolated_state_dirs.items():
        monkeypatch.setenv(var, path)

    # `--daemon-url` / `--config` are recorded process-wide (they must be
    # readable far from argument parsing), so a test that sets one would
    # otherwise redirect every later test's endpoint resolution.
    from browserwright import daemon_url as _daemon_url

    monkeypatch.setattr(_daemon_url, "_cli_override", None)
    monkeypatch.setattr(_daemon_url, "_cli_config_path", None)

    from browserwright import daemon_lifecycle

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            f"{request.node.nodeid} tried to spawn a real browserwright-daemon "
            f"({args!r}). That binds the production ports and evicts the "
            "developer's global daemon. Stub the call, or use the e2e harness "
            "under tests/daemon/e2e/ which isolates ports properly."
        )

    # Layer 1: the one start/replace entry point does nothing and reports a
    # daemon that is up, so callers take their ordinary path.
    monkeypatch.setattr(daemon_lifecycle, "ensure",
                        lambda reason, **_kw: _verdict())
    # Layer 2: anything that still reaches a real spawn is a bug, not a leak.
    monkeypatch.setattr(daemon_lifecycle, "_spawn_detached", _forbidden)

    # Vector B, second door (ADR-0013 rule 3): the endpoint *diagnosis* probes
    # not only the resolved endpoint but the alternatives a local client
    # could have meant — including the built-in default 127.0.0.1:19990,
    # which the state-file pin above cannot redirect. Default every probe to
    # "refused"; a test that wants a specific observation overrides
    # `daemon_lifecycle.probe` itself.
    from browserwright.daemon._ipc import EndpointProbe as _EP

    monkeypatch.setattr(
        daemon_lifecycle, "probe",
        lambda host, port, timeout=1.5: _EP(kind="refused", host=host,
                                            port=port, detail="stubbed"))

    # Vector C backstop: armed only for the test's own lifetime.
    _guard["hits"].clear()
    _guard["armed"] = True
    try:
        yield
    finally:
        _guard["armed"] = False
    if _guard["hits"]:
        hits = sorted(set(_guard["hits"]))
        _guard["hits"].clear()
        pytest.fail(
            f"{request.node.nodeid} reached the machine-global install: "
            f"{hits}. Files: derive the path from TMPDIR / BS_HOME / "
            "XDG_CACHE_HOME / XDG_RUNTIME_DIR, which tests/conftest.py "
            "redirects, or stub the write. Ports 19989/19990: pin a dead "
            "port or stub the probe (see the state-file pin above).",
            pytrace=False)
