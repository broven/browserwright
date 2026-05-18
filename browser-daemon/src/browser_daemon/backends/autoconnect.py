"""autoconnect backend — user enabled chrome://inspect/#remote-debugging.

Spec §2.4 + §8.3: this path triggers an "Allow remote debugging?" popup on
*every* new WS handshake (Chrome 144+ has zero memory). Doctor and `url`
intentionally do NOT open a ws — they only read `DevToolsActivePort` from the
filesystem, which is side-effect-free. The popup happens later, when the Skill
or some other CDP client actually opens the websocket.

Multi-profile tie-break: mtime newest. Spec §10 open question is resolved as
"v0.1 adds an HTTP probe to mark the live profile in doctor detail"; we do that
probe in `probe()` not in the cold-path of `resolve()`.

## Popup-accumulation defense (added post-v0.3, P0 user report)

Chrome 144+ has a real bug: accumulating "Allow remote debugging?" popups past
some internal threshold freezes the entire Chrome process. Our framework can't
ask developers to "just be disciplined" — the autoconnect path is too easy to
hit repeatedly (every `browser-daemon url` short-conn = one popup).

The defense: rate-limit successful `resolve()` calls to one per 60s window,
persisted across process invocations via a small timestamp file in
`$XDG_RUNTIME_DIR`. Mode B (`browser-daemon serve`) is exempt — by design it
opens upstream ws exactly once and shares across clients. The
`BD_FORCE_AUTOCONNECT_RECONNECT=1` env var bypasses the limit for experts
(documented "may freeze your Chrome").
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from ..config import Config
from ..errors import Unavailable
from ..platforms import profile_paths, runtime_dir
from .base import Backend, DoctorResult, ResolveResult


# 60-second cooldown window. Conservative — Chrome's popup accumulation
# threshold isn't documented; this gives users a chance to dismiss + breathe
# between handshakes. Tunable via spec §10 open question if reports come in.
RATE_LIMIT_WINDOW_SECONDS = 60.0
_TIMESTAMP_FILENAME = "browser-daemon-autoconnect-last.ts"


def _timestamp_path() -> Path:
    """Where we persist the last-handshake timestamp. Lives under
    XDG_RUNTIME_DIR (or /tmp) so it survives `browser-daemon` process churn
    but doesn't leak across reboots."""
    return runtime_dir() / _TIMESTAMP_FILENAME


def _read_last_handshake() -> float | None:
    """Best-effort read. Missing / corrupt file → returns None (no rate-limit)."""
    try:
        raw = _timestamp_path().read_text().strip()
        return float(raw)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_last_handshake(ts: float) -> None:
    """Atomic write — `.tmp` then `os.replace` — so concurrent readers never
    see a half-written file. Best-effort; permission errors silently swallowed
    (the rate-limit just degrades to off, which is better than a fatal error)."""
    p = _timestamp_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(f"{ts:.6f}")
        os.replace(tmp, p)
    except OSError:
        pass


def _force_flag_set() -> bool:
    """Expert escape: `BD_FORCE_AUTOCONNECT_RECONNECT=1` bypasses the
    rate-limit. We read at call time (not Config-build time) so users can flip
    it for a single invocation without re-loading config."""
    return os.environ.get("BD_FORCE_AUTOCONNECT_RECONNECT", "").lower() in ("1", "true", "yes")


class AutoconnectBackend(Backend):
    name = "autoconnect"
    kind = "UPSTREAM_WS"
    # Mode B is the recommended *future* mode for autoconnect (§2.5: short-conn
    # repeat = repeated popups). v0.1 still works in Mode A — just with the UX
    # cost.
    recommended_mode: str = "B"
    ux_cost = "popup-per-ws+banner"

    POPUP_WARNING = (
        "every new WS handshake triggers Chrome's \"Allow remote debugging?\" popup"
    )
    POPUP_ONBOARD = (
        "first time: tick chrome://inspect/#remote-debugging checkbox then accept the Allow popup"
    )

    def __init__(self, cfg: Config):
        self._cfg = cfg

    def _extra_paths(self) -> list[Path]:
        """v0.5.3 F-5: pull custom profile_paths from
        `[backends.autoconnect].profile_paths` in config.toml. Empty list
        when unset — caller falls back to the platform default list."""
        return [
            Path(p).expanduser()
            for p in self._cfg.backends.autoconnect.profile_paths
        ]

    # ------------------------------------------------------------------ probe

    async def probe(self) -> DoctorResult:
        scan = _scan_profiles(extra=self._extra_paths())
        if not scan:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=(
                    "no DevToolsActivePort file in any known profile — "
                    "enable chrome://inspect/#remote-debugging in your running Chrome"
                ),
                ux_warning=self.POPUP_WARNING,
                needs_user_action=self.POPUP_ONBOARD,
                ux_cost=self.ux_cost,
            )
        # Tie-break: mtime newest. Optional active-probe to mark which is live.
        scan.sort(key=lambda r: r.mtime, reverse=True)
        newest = scan[0]
        # Active probe is side-effect-free (HTTP only). Skips the ws entirely.
        live = await _is_live(newest.port)
        detail = (
            f"DevToolsActivePort present at {newest.base} port {newest.port}, "
            f"ws not probed{' (HTTP discovery live)' if live else ''}"
        )
        if len(scan) > 1:
            others = ", ".join(f"{r.base.name}:{r.port}" for r in scan[1:])
            detail += f"; other candidates by mtime: {others}"
        return DoctorResult(
            name=self.name,
            available=True,
            ws_url=None,
            detail=detail,
            ux_warning=self.POPUP_WARNING,
            needs_user_action=self.POPUP_ONBOARD,
            ux_cost=self.ux_cost,
        )

    # ---------------------------------------------------------------- resolve

    async def resolve(self, timeout: float) -> ResolveResult:
        # P0 defense: rate-limit Mode A invocations. Mode B (`browser-daemon
        # serve`) sets the caller_context contextvar so it bypasses this
        # check — it holds one upstream ws across all client connections, so
        # the popup-accumulation hazard doesn't apply.
        self._check_rate_limit()

        scan = _scan_profiles(extra=self._extra_paths())
        if not scan:
            raise Unavailable(
                "autoconnect: no DevToolsActivePort file in any known profile. "
                "Enable chrome://inspect/#remote-debugging in your running Chrome.",
                attempts={self.name: "no DevToolsActivePort file found"},
            )
        scan.sort(key=lambda r: r.mtime, reverse=True)
        # Prefer a profile whose HTTP discovery is *live*; mtime alone isn't
        # enough — Chrome can leave stale DevToolsActivePort files behind when
        # a different --user-data-dir grabbed the same port. We poll each
        # candidate in mtime order with a short timeout per call.
        per = max(0.5, timeout / max(len(scan), 1))
        last_err: str | None = None
        for r in scan:
            ws = await _resolve_via_json_version(r.port, per)
            if ws is not None:
                _write_last_handshake(time.time())
                return ResolveResult(
                    ws_url=ws,
                    backend=self.name,
                    extras={"isolated_profile": False, "profile_path": str(r.base)},
                )
            # 404 fallback per §8.2 — same DevToolsActivePort line-2 ws path.
            if r.ws_path:
                ws = f"ws://127.0.0.1:{r.port}{r.ws_path}"
                _write_last_handshake(time.time())
                return ResolveResult(
                    ws_url=ws,
                    backend=self.name,
                    extras={"isolated_profile": False, "profile_path": str(r.base)},
                )
            last_err = f"{r.base} port {r.port}: no /json/version, no ws_path line"
        raise Unavailable(
            "autoconnect: all candidate profiles unreachable",
            attempts={self.name: last_err or "no live candidate"},
        )

    def _check_rate_limit(self) -> None:
        """Raise `Unavailable` if Mode A is calling within the cooldown window.

        Mode B (`browser-daemon serve`) sets `caller_context` so it bypasses.
        `BD_FORCE_AUTOCONNECT_RECONNECT=1` env bypass is for expert escape.
        """
        # Mode B opt-out. Local import dodges the circular hazard between
        # resolver → backends → resolver.
        from .. import resolver as _resolver_mod
        if _resolver_mod.caller_context.get() == "mode_b_serve":
            return
        if _force_flag_set():
            return
        last = _read_last_handshake()
        if last is None:
            return
        elapsed = time.time() - last
        if elapsed >= RATE_LIMIT_WINDOW_SECONDS:
            return
        remaining = RATE_LIMIT_WINDOW_SECONDS - elapsed
        raise Unavailable(
            "autoconnect rate-limited: each new ws handshake triggers Chrome's "
            "'Allow remote debugging' popup, and Chrome 144+ accumulates these "
            f"popups and may freeze. Refusing to trigger another within "
            f"{RATE_LIMIT_WINDOW_SECONDS:.0f}s of the last handshake "
            f"({elapsed:.1f}s ago, {remaining:.1f}s until next allowed). "
            "Use Mode B long-running daemon (`browser-daemon serve --backend "
            "autoconnect`) for repeated work, OR isolated Chrome instead "
            "(`browser-daemon launch-chrome --port <X> --profile <P>`). "
            "Override with `BD_FORCE_AUTOCONNECT_RECONNECT=1` "
            "(WARNING: may freeze your Chrome).",
            attempts={self.name: f"rate-limited, {remaining:.1f}s remaining"},
        )


# ---- helpers ---------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class _Scan:
    base: Path
    port: str
    ws_path: str
    mtime: float


def _scan_profiles(extra: list[Path] | None = None) -> list[_Scan]:
    """Scan all known Chrome profile dirs for DevToolsActivePort files.

    `extra` (v0.5.3 F-5): paths from `[backends.autoconnect].profile_paths`
    in config.toml. They PREPEND the platform default list so a user can
    add a non-default profile dir without losing default coverage.
    """
    out: list[_Scan] = []
    sources: list[Path] = []
    if extra:
        sources.extend(extra)
    sources.extend(profile_paths())
    seen: set[Path] = set()
    for base in sources:
        if base in seen:
            continue
        seen.add(base)
        f = base / "DevToolsActivePort"
        try:
            lines = f.read_text().splitlines()
            mtime = f.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        if not lines:
            continue
        port = lines[0].strip()
        ws_path = lines[1].strip() if len(lines) > 1 else ""
        if not port:
            continue
        out.append(_Scan(base=base, port=port, ws_path=ws_path, mtime=mtime))
    return out


async def _is_live(port: str) -> bool:
    """HTTP-only liveness probe (no ws). Used by doctor to annotate which
    candidate is actually serving DevTools, in the multi-profile case."""
    try:
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
    except (httpx.HTTPError, OSError):
        return False
    # 200 OR 404 both mean "DevTools is up" — 404 only means the new lockdown
    # disabled HTTP discovery, but the ws still works.
    return resp.status_code in (200, 404)


async def _resolve_via_json_version(port: str, timeout: float) -> str | None:
    """Returns ws URL on HTTP 200, None on any other code / network failure
    (signaling the caller to try the 404 fallback)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
    return ws if isinstance(ws, str) and ws else None
