"""launch-chrome subcommand — H9 install-wizard helper.

Spec §5.5: locate Chrome → allocate user-data-dir → spawn detached → poll
`DevToolsActivePort` → output ws URL → write pid file → exit. The Chrome
process stays alive (detached, in its own process group), so Skill can later
`kill $(cat pidfile)` to shut it down.

Important constraints (spec §5.5):
- We don't auto-attach. We just launch and print the URL.
- We don't take custom --no-sandbox / --lang flags. (Open question §10 → punt.)
- After exit, DevToolsActivePort is Chrome's responsibility to clean up.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from .config import Config, check_name
from .errors import ChromeBinaryNotFound, UserError, Unavailable
from .platforms import cache_dir, discover_chrome_binary, profile_paths, runtime_dir


DEFAULT_PORT = 0  # let the OS pick when --port not given (spec §5.5 step 3)
DEFAULT_TIMEOUT = 30.0


async def launch_chrome(
    cfg: Config,
    *,
    profile: str = "isolated",
    persistent: bool = True,
    chrome_binary: str | None = None,
    port: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_default_profile: bool = False,
    extra_args: list[str] | None = None,
) -> dict:
    """Launch Chrome detached with --remote-debugging-port + isolated profile.

    Returns the same shape as `url --json`:
        {schema_version: 1, ws_url, backend: "rdp", extras: {isolated_profile, profile_path, pid}}

    `allow_default_profile=True` (or env `BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE=1`)
    is the expert escape hatch for the §11 guard — see `_check_not_default_profile`.

    `extra_args` (optional list) is appended to the Chrome argv verbatim, after
    the framework's own flags. Used by the E2E harness to inject
    `--load-extension=...`. Caller is responsible for shell-escaping.
    """
    check_name(profile)

    # 1) Chrome binary.
    binary = discover_chrome_binary(chrome_binary or cfg.chrome_binary)
    if binary is None:
        raise ChromeBinaryNotFound(
            "could not locate a Chrome binary. Set BD_CHROME_BINARY or pass "
            "--chrome-binary."
        )

    # 2) user-data-dir.
    user_data_dir = _allocate_data_dir(profile, persistent=persistent)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    # 2.1) **Default-profile guard** (v0.5 — Task #11).
    # If `user_data_dir` is the OS-default Chrome profile, refuse. Launching
    # Chrome with `--remote-debugging-port` against the user's daily profile
    # permanently taints it: Chrome writes `DevToolsActivePort`, starts
    # LISTEN on the requested port, and every subsequent ws upgrade triggers
    # Chrome's "Allow remote debugging?" popup. This is the **root cause**
    # of the 2026-05-18 popup storm — see chrome-popup-accumulation-bug
    # memory for forensics. The escape hatch exists for the rare expert use
    # case (someone deliberately wants their daily Chrome on CDP).
    _check_not_default_profile(
        user_data_dir,
        allow=(allow_default_profile or _truthy_env(
            "BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE")),
    )

    # 3) Port.
    use_port = DEFAULT_PORT if port is None else port

    # 4) Spawn detached.
    args = [
        str(binary),
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={use_port}",
        # `--no-first-run` + `--no-default-browser-check` keep an isolated
        # profile from showing welcome dialogs on first launch. They don't
        # affect remote-debugging behavior — just UI.
        "--no-first-run",
        "--no-default-browser-check",
        # Chrome 121+ rejects the ws upgrade with HTTP 403 unless the caller's
        # Origin is on the allow-list (origin-based CSRF defense). Skill /
        # cdp-use opens the ws from a Python process with no Origin header —
        # which Chrome 121+ treats as *not allowed* by default. We pass `*`
        # because the user-data-dir is already an isolation boundary (no
        # session cookies / no auto-login) — same posture as DevTools itself.
        "--remote-allow-origins=*",
        # Disable OS keychain integration. On macOS, Chrome otherwise prompts
        # for the login keychain password on every fresh-profile start ("…wants
        # to use confidential information stored in Chromium Safe Storage…"),
        # which blocks automation. The isolated user-data-dir has nothing
        # encrypted to begin with, so the basic/mock store is functionally
        # equivalent. Same defaults Playwright / Puppeteer / browser-use ship.
        "--password-store=basic",
        "--use-mock-keychain",
    ]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_spawn_kwargs(),
    )

    # 5) Wait for Chrome to be reachable.
    #
    # Primary signal: DevToolsActivePort file (gives us both the chosen port
    # and the ws path in one read). Secondary signal: /json/version on the
    # known port — only available when --port N was explicit.
    #
    # The secondary path covers a Chrome 148 macOS quirk where, with the
    # user's primary Chrome already running, the spawned child Chrome answers
    # `/json/version` on the requested port but never writes
    # DevToolsActivePort (Skill team field report May 2026). When --port 0
    # we have no fallback because we don't know what port Chrome picked.
    actual_port, ws_path = await _wait_for_chrome_ready(
        proc, user_data_dir, requested_port=port, timeout=timeout,
    )

    # 6) Build ws URL.
    ws_url = f"ws://127.0.0.1:{actual_port}{ws_path}"

    # 7) Write pid file. Best-effort; missing dir / permission denied just
    # surfaces as a warning in extras.
    pid = proc.pid
    pidfile_err: str | None = None
    pidfile = runtime_dir() / f"browserwright-daemon-chrome-{profile}.pid"
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(f"{pid}\n")
    except OSError as e:
        pidfile_err = f"could not write pid file {pidfile}: {e}"

    return {
        "schema_version": 1,
        "ws_url": ws_url,
        "backend": "rdp",
        "extras": {
            "isolated_profile": True,
            "profile_path": str(user_data_dir),
            "pid": pid,
            "pid_file": str(pidfile) if pidfile_err is None else None,
            "pid_file_error": pidfile_err,
        },
    }


# ---- helpers ---------------------------------------------------------------


async def _wait_for_chrome_ready(
    proc: subprocess.Popen,
    user_data_dir: Path,
    *,
    requested_port: int | None,
    timeout: float,
) -> tuple[str, str]:
    """Poll DevToolsActivePort first; fall back to /json/version when an
    explicit port was requested. Returns (port_str, ws_path).

    Why two signals?
    - DevToolsActivePort is the canonical Chrome signal — it carries the
      ws path so we can build the URL without an extra HTTP roundtrip.
    - But Chrome 148 on macOS, when invoked while the user's primary Chrome
      is already running, sometimes never writes the file (the new instance
      gets bootstrapped through a different code path). It DOES answer
      `/json/version` on the requested port — so when --port was explicit,
      we can resolve via the HTTP discovery shape that rdp already uses.

    We poll both in the same loop instead of waiting full `timeout` on one
    then the other. Whichever wins first answers; the other never runs.
    """
    active_file = user_data_dir / "DevToolsActivePort"
    fallback_url = (
        f"http://127.0.0.1:{requested_port}/json/version"
        if requested_port is not None and requested_port > 0
        else None
    )
    deadline = time.monotonic() + timeout
    last_http_err: str | None = None
    # Chrome 148 macOS quirk (Skill team field report May 2026):
    # the launcher binary fork-exec's the real Chrome process and then
    # exits with code 126. The grandchild Chrome continues running, writes
    # DevToolsActivePort, and answers /json/version normally. Our `proc`
    # handle is the short-lived parent. So we must NOT fail the loop the
    # instant `proc.poll() is not None`; we have to keep checking the
    # DevToolsActivePort + HTTP signals until the actual `timeout`. We
    # still record the proc exit so the timeout error message can be
    # precise.
    child_exited_at: float | None = None
    child_exit_code: int | None = None

    while time.monotonic() < deadline:
        # Note when the child died (could be benign fork-exec hand-off OR a
        # real failure like SingletonLock). Keep polling either way.
        if proc.poll() is not None and child_exited_at is None:
            child_exited_at = time.monotonic()
            child_exit_code = proc.returncode

        # Primary: DevToolsActivePort.
        try:
            lines = active_file.read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
                return lines[0].strip(), lines[1].strip()
        except (FileNotFoundError, OSError):
            pass

        # Secondary: /json/version on the known port. We only try this when
        # --port was explicit — with --port 0 we don't know what port Chrome
        # picked, so DevToolsActivePort is our only option.
        if fallback_url is not None:
            try:
                async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                    resp = await client.get(fallback_url)
                if resp.status_code == 200:
                    body = resp.json()
                    ws_url_full = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
                    if isinstance(ws_url_full, str) and ws_url_full:
                        # ws_url_full is `ws://127.0.0.1:N/devtools/browser/UUID`
                        # — split into port + path for the caller's URL builder.
                        from urllib.parse import urlparse
                        parsed = urlparse(ws_url_full)
                        port_str = str(parsed.port or requested_port)
                        ws_path = parsed.path
                        return port_str, ws_path
            except (httpx.HTTPError, OSError) as e:
                last_http_err = f"{type(e).__name__}: {e}"

        await asyncio.sleep(0.1)

    # Timed out. Distinguish two failure modes for the error message:
    #   (a) child exited AND nothing answered → likely SingletonLock or
    #       Chrome flag rejection. Mention the exit code prominently.
    #   (b) child still alive but no DevToolsActivePort / no /json/version →
    #       Chrome is running but not serving CDP — bad flags, port already
    #       bound by something else, sandbox failure, etc.
    # Only terminate when we know the proc is still ours to kill (case b).
    if child_exited_at is None:
        with _silent():
            proc.terminate()

    reasons: list[str] = []
    if child_exit_code is not None:
        reasons.append(
            f"launcher process exited with code {child_exit_code} after "
            f"~{child_exited_at and (child_exited_at - (deadline - timeout)):.1f}s "
            f"(grandchild Chrome may have survived; check `ps aux | grep -i chrome`). "
            f"If another Chrome instance owns {user_data_dir}, remove "
            f"`SingletonLock` from that dir or pass a different `--profile`."
        )
    reasons.append(f"DevToolsActivePort never appeared in {user_data_dir}")
    if fallback_url is not None:
        suffix = f" (last HTTP error: {last_http_err})" if last_http_err else ""
        reasons.append(f"and {fallback_url} did not become reachable{suffix}")
    raise Unavailable(
        f"launch-chrome: Chrome not ready after {timeout}s — "
        + "; ".join(reasons)
    )


def _truthy_env(name: str) -> bool:
    """Common truthy parser for env-var flags. Recognizes 1/true/yes/on/y
    (case-insensitive); empty string and unset are False. Matches the
    informal convention most CLIs use — REVIEW.md F-9 #11 found we
    previously only accepted `"1"`/`"true"`/`"True"`, silently rejecting
    `"yes"` / `"on"` / `"TRUE"`."""
    return os.environ.get(name, "").strip().lower() in {
        "1", "true", "yes", "on", "y",
    }


def _check_not_default_profile(user_data_dir: Path, *, allow: bool) -> None:
    """Refuse if `user_data_dir` is the OS-default Chrome / Edge / Brave / Arc
    profile root. The platforms table is the source of truth — we resolve both
    sides to absolute paths and compare with `os.path.samefile()` if both
    exist, then fall back to string-equality on the resolved Path.

    Raises `UserError` when the dir matches a default-profile location and
    `allow=False`. No-op otherwise.
    """
    try:
        target = user_data_dir.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        target = user_data_dir
    target_str = str(target)
    for default in profile_paths():
        try:
            d = default.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            d = default
        if str(d) == target_str:
            if allow:
                return
            raise UserError(
                f"refusing to launch-chrome against the user's default profile "
                f"({target_str}). Chrome will be permanently tainted with "
                f"--remote-debugging-port (every ws upgrade triggers an "
                f"'Allow remote debugging?' popup; the LISTEN socket persists "
                f"across the Chrome process's lifetime). Use a different "
                f"`--profile <isolated_name>` or `--tmp` instead. If you "
                f"truly know what you're doing, set "
                f"`BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE=1` — but note this "
                f"may permanently expose your daily Chrome to CDP popup "
                f"hazard (see chrome-popup-accumulation-bug memory).")


def _allocate_data_dir(profile: str, *, persistent: bool) -> Path:
    if persistent:
        return cache_dir() / "profiles" / profile
    # --tmp: a fresh per-launch dir, NOT auto-cleaned (spec §5.5 step 2). User
    # cleans up by hand to avoid the race between Chrome shutdown writeback and
    # our rm -rf.
    return Path(tempfile.mkdtemp(prefix=f"browserwright-daemon-{profile}-"))


def _spawn_kwargs() -> dict:
    """Detach the spawn from this terminal — mirrors browser-harness
    `_ipc.py:68-76` `spawn_kwargs()`.
    """
    return {"start_new_session": True}


class _silent:
    """Context manager that swallows OSErrors during cleanup."""
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return exc_type is not None and issubclass(exc_type, OSError)
