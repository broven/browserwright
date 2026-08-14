"""Interactive install wizard.

Goal: walk a fresh user through the Chrome-source options the daemon
supports and print the exact next-step commands for the chosen backend.

Choices (order matters — the default is option 1):

  1. 隔离 profile (cdp + browserwright-daemon launch-chrome) — **Recommended for
     scraping / dev work**. Zero popups, zero banner, doesn't touch the
     user's daily Chrome.
  2. 指纹浏览器 (cdp + custom port) — AdsPower / MultiLogin / GoLogin /
     比特浏览器, etc. User supplies the port number.
  3. Browser extension relay — drive the user's daily Chrome without any
     popups or banners. Requires loading the unpacked extension from
     ``browserwright-daemon/chrome-extension/``. Surfaced as live when the
     daemon's ``doctor`` reports the extension backend available.
  4. External CDP endpoint — a browser the user started themselves (cloud,
     anti-detect, fingerprint) that exposes a browser-level CDP ws or http
     URL. Recorded per session via ``--attach=<url>``, so one daemon can hold
     several at once.

Detection is minimal — we ask the user.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


# (key, backend, label, description). The order here is the order shown.
_OPTIONS: list[Tuple[str, str, str, str]] = [
    ("1", "cdp",
     "隔离 profile (cdp + browserwright-daemon launch-chrome)  [Recommended]",
     "Skill 起一个独立 user-data-dir 的后台 Chrome；零打扰、零 popup。"),
    ("2", "cdp",
     "指纹浏览器 (cdp + 自定义端口)",
     "AdsPower / MultiLogin / GoLogin / 比特浏览器 等；你已开好对应 profile。"),
    ("3", "extension",
     "Browser extension relay (drives your daily Chrome, no popup)",
     "通过加载到 Chrome 的扩展中继 CDP，零 popup、零横幅；连接日常 Chrome 的唯一路径。"),
    ("4", "cdp",
     "External CDP endpoint (anti-detect / fingerprint / cloud browser)",
     "你自己起好的浏览器，暴露一个 browser-level CDP ws 或 http 地址。\n"
     "     attach 语义：session end 不会关这个浏览器。多 profile 直接开多个会话，\n"
     "     一个 daemon 就够——每个会话各带各的 endpoint。"),
]


def _prompt(msg: str, *, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        return default or ""
    return ans or (default or "")



def chrome_extension_path() -> Optional[str]:
    """Return the absolute path to the daemon's ``chrome-extension/`` directory,
    or ``None`` if we can't determine it.

    Resolution order:
      1. ``$BS_CHROME_EXTENSION_PATH`` env (test/dev override).
      2. ``browserwright-daemon extension-path --json`` subprocess (the v0.4 daemon
         exposes the resource path; cheap one-shot, no ws side effects).
      3. Best-effort walk from ``which browserwright-daemon`` — many dev checkouts
         keep ``chrome-extension/`` as a sibling of the daemon's ``src/``
         tree, which puts it two levels up from the installed bin.
      4. ``None`` → wizard prints generic guidance.
    """
    env_path = os.environ.get("BS_CHROME_EXTENSION_PATH")
    if env_path:
        return env_path

    # (2) Ask the daemon itself.
    try:
        proc = subprocess.run(
            ["browserwright-daemon", "extension-path", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
                p = data.get("path") if isinstance(data, dict) else None
                if p and os.path.isdir(p):
                    return p
            except json.JSONDecodeError:
                # Some daemon builds may emit a bare path on stdout.
                p = proc.stdout.strip().splitlines()[0]
                if os.path.isdir(p):
                    return p
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # (3) Best-effort walk from the installed binary.
    bin_path = shutil.which("browserwright-daemon")
    if bin_path:
        candidate = Path(bin_path).resolve().parent.parent / "chrome-extension"
        if candidate.is_dir():
            return str(candidate)
    return None


def _wizard_doctor_backends() -> list[dict]:
    """One ``health.daemon_doctor()`` call shared by every wizard
    option-availability detector.

    spec H3 / §D.2.3: ``doctor`` is contract-bound to **zero ws side
    effects**, so calling it at wizard entry is safe.

    **Detection contract for every future option (v0.5+)**: any new
    ``_<backend>_backend_available()`` helper MUST derive its answer
    from this dict only. Do not open a CDP ws and do not subprocess a
    backend-specific ``--probe`` command.
    If the signal you need isn't in doctor's JSON, extend the daemon's
    doctor schema first.
    """
    try:
        from .health import daemon_doctor
        info = daemon_doctor()
    except Exception:  # noqa: BLE001
        return []
    if info.get("skill_synthetic"):
        return []
    return list(info.get("backends", []) or [])


def _backend_available_from(backends: list[dict], name: str) -> bool:
    return any(b.get("name") == name and b.get("available")
               for b in backends)


def _extension_backend_available() -> bool:
    """Best-effort: ask the daemon's doctor if an ``extension`` backend
    is registered + available. Any failure → treat as unavailable (the
    label will read "coming v0.4")."""
    return _backend_available_from(_wizard_doctor_backends(), "extension")


def run() -> int:
    print("browserwright install wizard")
    print("=" * 32)
    print()
    # Single shared doctor probe (spec H3 zero-side-effect contract). The
    # wizard pays at most one subprocess regardless of backend count.
    backends = _wizard_doctor_backends()
    ext_live = _backend_available_from(backends, "extension")

    print("Pick how Skill should connect to Chrome:")
    for key, _backend, label, desc in _OPTIONS:
        if key == "3" and not ext_live:
            shown_label = f"{label}  (daemon reports extension backend not yet available)"
        else:
            shown_label = label
        print(f"  {key}. {shown_label}")
        for ln in desc.splitlines():
            print(f"     {ln}")
    print()

    choice = _prompt("Choose 1 / 2 / 3 / 4", default="1")
    match = next((o for o in _OPTIONS if o[0] == choice), None)
    if match is None:
        print(f"unknown choice: {choice!r}", file=sys.stderr)
        return 1
    _key, _backend, label, _desc = match

    if choice == "3" and not ext_live:
        print()
        print("Extension backend is not yet available in your installed daemon.")
        print("Re-run this wizard after upgrading to a daemon build that ships")
        print("the extension backend, or pick option 1 / 2 / 4 for now.")
        return 1

    if choice == "2":
        port = _prompt("Fingerprint browser CDP port (e.g. 9223)", default="9222")
        try:
            int(port)
        except ValueError:
            print(f"port must be an integer, got {port!r}", file=sys.stderr)
            return 1

    if choice == "4":
        # The endpoint is per-session ledger state now (#38), so there is
        # nothing for the wizard to write into config.toml or an env var — it
        # just shows the command that records it.
        cdp = _prompt(
            "External CDP ws or http URL (blank to fill in later)",
            default="",
        ).strip()
        example = cdp or "ws://127.0.0.1:8080/api/profiles/<id>/cdp"
        print()
        print("The endpoint belongs to the session, not the daemon. Bind one:")
        print(f"    browserwright session new --backend=cdp --attach={example} "
              "--name=<label>")
        print("Repeat for as many profiles as you need — one daemon serves them")
        print("all, each session on its own browser.")
        print()
        print("The URL is stored in the ledger (0600) and redacted wherever it "
              "is printed, so a token embedded in it is safe to pass here.")

    print()
    print(f"Selected: {label}")
    print()
    print("Next steps:")
    if choice == "1":
        print("  - Run `browserwright-daemon launch-chrome` to start the isolated profile.")
        print("  - Then create a session: `browserwright session new --backend=cdp --create --name=TASK`.")
        print("    (`--name` labels the isolated browser session; choose a short task label.)")
    elif choice == "2":
        print("  - Make sure your fingerprint browser is open on the chosen port.")
        print("  - Then attach a session: `browserwright session new --backend=cdp --attach=PORT --name=TASK`.")
        print("    (`--name` labels the attached browser session; choose a short task label.)")
    elif choice == "3":
        ext_dir = chrome_extension_path()
        print("  1. Install the Chrome extension (store build recommended):")
        print("       - Chrome Web Store → install browserwright:")
        print("         https://chromewebstore.google.com/detail/")
        print("         browserwright-daemon-rela/okgnalaalckoaeledbjhpjiccmcdceeb")
        print("       - Developers: chrome://extensions → toggle 'Developer mode'")
        if ext_dir:
            print("         → 'Load unpacked' → pick:")
            print(f"           {ext_dir}")
        else:
            print("         → 'Load unpacked' → pick the daemon's")
            print("         `chrome-extension/` directory.")
            print("         (Hint: `browserwright-daemon extension-path --json` prints it.)")
        print("  2. Start the single global daemon:")
        print("       browserwright-daemon serve")
        print("       (or install it once as a LaunchAgent: `browserwright-daemon install`.)")
        print("       The daemon hosts the extension relay and still routes session")
        print("       backends per session.")
        print("  3. In Chrome, click the extension icon → 'Attach this tab'.")
        print("     Verify with: `browserwright-daemon doctor --json` →")
        print("     look for `extension` backend `available=true` + `ws_url` set.")
        print("  4. Then create a session: `browserwright session new --backend=extension --name=TASK`.")
        print("     (`--name` is the Chrome tab group title; choose a short task label.)")
    return 0
