"""Interactive install wizard.

Goal: walk a fresh user through the Chrome-source options the daemon
supports and persist their pick into ``global.md`` so future Skill processes
auto-connect via the right backend.

Choices (order matters — the default is option 1):

  1. 隔离 profile (rdp + browserwright-daemon launch-chrome) — **Recommended for
     scraping / dev work**. Zero popups, zero banner, doesn't touch the
     user's daily Chrome.
  2. 指纹浏览器 (rdp + custom port) — AdsPower / MultiLogin / GoLogin /
     比特浏览器, etc. User supplies the port number.
  3. Browser extension relay — drive the user's daily Chrome without any
     popups or banners. Requires loading the unpacked extension from
     ``browserwright-daemon/chrome-extension/``. Surfaced as live when the
     daemon's ``doctor`` reports the extension backend available.
  4. Cloud / remote browser (Browser Use / Browserless / Hyperbrowser) —
     hosted Chrome via auth provider.

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

from .memory.global_mem import global_memory


# (key, backend, label, description). The order here is the order shown.
_OPTIONS: list[Tuple[str, str, str, str]] = [
    ("1", "rdp",
     "隔离 profile (rdp + browserwright-daemon launch-chrome)  [Recommended]",
     "Skill 起一个独立 user-data-dir 的后台 Chrome；零打扰、零 popup。"),
    ("2", "rdp",
     "指纹浏览器 (rdp + 自定义端口)",
     "AdsPower / MultiLogin / GoLogin / 比特浏览器 等；你已开好对应 profile。"),
    ("3", "extension",
     "Browser extension relay (drives your daily Chrome, no popup)",
     "通过加载到 Chrome 的扩展中继 CDP，零 popup、零横幅；连接日常 Chrome 的唯一路径。"),
    ("4", "cloud",
     "Cloud/Remote browser (Browser Use / Browserless / Hyperbrowser)",
     "远程 Chrome 服务，通过 daemon 内置 AuthProvider 抽象处理 \n"
     "     Bearer / Basic / mTLS 鉴权。零本地 Chrome 进程。"),
]


def _prompt(msg: str, *, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        return default or ""
    return ans or (default or "")


def _yesno(msg: str, *, default_yes: bool = True) -> bool:
    suffix = "Y/n" if default_yes else "y/N"
    ans = _prompt(f"{msg} ({suffix})").lower()
    if not ans:
        return default_yes
    return ans.startswith("y")


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
    """One ``DaemonClient().doctor()`` call shared by every wizard
    option-availability detector.

    spec H3 / §D.2.3: ``doctor`` is contract-bound to **zero ws side
    effects**, so calling it at wizard entry is safe.

    **Detection contract for every future option (v0.5+)**: any new
    ``_<backend>_backend_available()`` helper MUST derive its answer
    from this dict only. Do not open a CDP ws, do not subprocess a
    backend-specific ``--probe`` command, do not curl a cloud provider.
    If the signal you need isn't in doctor's JSON, extend the daemon's
    doctor schema first.
    """
    try:
        from .daemon_client import DaemonClient
        info = DaemonClient().doctor()
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


def _cloud_backend_entry(backends: Optional[list[dict]] = None) -> Optional[dict]:
    """Return the daemon's ``cloud`` backend doctor entry, or ``None``.

    Contract (v0.5, pre-agreed with daemon-impl-2 — see HANDOFF v0.5)::

        {
          "name":               "cloud",
          "available":          bool,            # config complete + auth provider loadable
          "ws_url":             str | None,
          "detail":             str,             # "<provider> (auth_kind=..., endpoint=...)"
                                                 # or "not configured; run ..."
          "ux_cost":            "auth-required", # new enum value, not "popup"
          "ux_warning":         str | None,
          "needs_user_action":  str | None,
          "extras": {
            "provider":   "browser-use"|"browserless"|"hyperbrowser"|"generic",
            "endpoint":   "wss://..." or "https://...",
            "auth_kind":  "bearer"|"basic"|"mtls",
            "configured": bool,
          }
        }

    If ``backends`` is provided, scan it; otherwise fetch via
    ``_wizard_doctor_backends()``. Returns ``None`` if doctor isn't
    reachable or the entry is missing.
    """
    if backends is None:
        backends = _wizard_doctor_backends()
    for b in backends:
        if b.get("name") == "cloud":
            return b
    return None


def _cloud_backend_available() -> bool:
    """v0.5 mirror of ``_extension_backend_available()``: ask doctor if the
    daemon's ``cloud`` backend (Browser Use / Browserless / Hyperbrowser /
    generic) is registered and currently available.

    The daemon-side cloud backend handles auth abstraction (Bearer / Basic
    / mTLS) and credential lifecycle; the Skill side only collects
    *references* to credentials (env-var names, cert file paths) — never
    the secrets themselves — and persists them to ``global.md``.
    """
    entry = _cloud_backend_entry()
    return bool(entry and entry.get("available"))


# v0.5 cloud config TOML table names. Matches the daemon 0.5.0 schema:
# top section keeps endpoint / auth_kind / provider_hint; per-kind credential
# *references* live in a ``[backends.cloud.auth.<kind>]`` subtable so the
# daemon's polymorphic ``AuthProvider`` builder can pick the right
# implementation. Kept as module-level constants so a future daemon schema
# rename is one-place + one-grep.
_CLOUD_TOML_TOP_SECTION = "[backends.cloud]"
_CLOUD_TOML_AUTH_SECTION_FMT = "[backends.cloud.auth.{kind}]"


def _cloud_owned_sections(auth_kinds: tuple[str, ...] = (
        "bearer", "basic", "mtls", "oauth2")) -> set[str]:
    """All TOML sections the wizard considers its own. Includes the
    top-level cloud table and every auth subtable (even unused ones —
    so when the user switches from bearer to mtls the old subtable
    doesn't linger)."""
    out = {_CLOUD_TOML_TOP_SECTION}
    for k in auth_kinds:
        out.add(_CLOUD_TOML_AUTH_SECTION_FMT.format(kind=k))
    return out


def _daemon_config_path() -> Path:
    """Return ``~/.config/browserwright-daemon/config.toml`` (XDG-aware).

    ``$BS_DAEMON_CONFIG_PATH`` env override exists for tests + the rare
    case where a user wants a non-XDG location.
    """
    override = os.environ.get("BS_DAEMON_CONFIG_PATH")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "browserwright-daemon" / "config.toml"


def _toml_escape(s: str) -> str:
    """TOML basic-string escape (REVIEW.md F-13).

    Handles backslash, double-quote, and the full ASCII control-char
    range (TOML basic strings reject unescaped 0x00–0x1F + 0x7F). The
    inputs the wizard collects (env-var names, file paths, endpoint
    URLs) should never contain control chars in practice — if one
    sneaks in via env / clipboard paste, we **reject** rather than
    silently emit an invalid TOML literal. The error name + offset
    surface in the resulting ``ValueError`` so the user can fix the
    source.
    """
    text = str(s)
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp == 0x7F or (cp < 0x20 and ch not in "\t\n\r"):
            raise ValueError(
                f"TOML emit refused: control character U+{cp:04X} at "
                f"offset {i} of input {text!r}. Strip the control "
                f"character (likely a stray newline / clipboard "
                f"artifact) and re-run the wizard."
            )
    return (
        text
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '\\r')
        .replace('\t', '\\t')
    )


# Mapping from wizard ``cloud_fields`` keys to (toml_section, toml_key)
# pairs. Drives both the emit logic and the strip-existing-sections
# logic so the two stay in sync.
_AUTH_FIELD_LAYOUT: dict[str, tuple[str, str, str]] = {
    # wizard key            (auth_kind,  toml key)
    "cloud_token_env":      ("bearer",   "token_env"),
    "cloud_username_env":   ("basic",    "username_env"),
    "cloud_password_env":   ("basic",    "password_env"),
    "cloud_cert_file":      ("mtls",     "cert_file"),
    "cloud_key_file":       ("mtls",     "key_file"),
}


def _write_daemon_cloud_config(provider_hint: str, auth_kind: str,
                               fields: dict) -> Path:
    """Persist daemon-0.5.0-shaped cloud config to ``config.toml``.

    Emits two sections::

        [backends.cloud]
        endpoint      = "..."           # if collected
        auth_kind     = "<kind>"
        provider_hint = "<provider>"    # display name only; informational

        [backends.cloud.auth.<kind>]
        # per-kind credential references, never the secret itself:
        # bearer → token_env
        # basic  → username_env, password_env
        # mtls   → cert_file, key_file

    The wizard owns both sections **wholesale**: existing
    ``[backends.cloud]`` and every ``[backends.cloud.auth.<kind>]`` is
    replaced on re-run (whether or not the user changed auth_kind, so
    stale subtables don't linger). All other sections — ``[server]``,
    ``[logging]``, ``[backends.rdp]``, etc. — are preserved verbatim.

    Hand-rolled TOML emit (no ``tomli_w`` dep). The shapes we produce
    are basic strings + scalars; daemon reads via stdlib ``tomllib``.
    """
    path = _daemon_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    owned = _cloud_owned_sections()
    # Read & strip any owned sections from the existing file.
    pre_lines: list[str] = []
    if path.exists():
        in_owned = False
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_owned = stripped in owned
                if not in_owned:
                    pre_lines.append(line)
                continue
            if not in_owned:
                pre_lines.append(line)

    # Build the new top-level [backends.cloud] block.
    top: list[str] = [_CLOUD_TOML_TOP_SECTION]
    if "cloud_endpoint" in fields:
        top.append(f'endpoint = "{_toml_escape(fields["cloud_endpoint"])}"')
    top.append(f'auth_kind = "{_toml_escape(auth_kind)}"')
    top.append(f'provider_hint = "{_toml_escape(provider_hint)}"')

    # Build the [backends.cloud.auth.<kind>] subtable from the fields
    # whose layout matches this auth_kind.
    sub: list[str] = [_CLOUD_TOML_AUTH_SECTION_FMT.format(kind=auth_kind)]
    sub_keys_emitted = 0
    for wkey, (kind, toml_key) in _AUTH_FIELD_LAYOUT.items():
        if kind != auth_kind or wkey not in fields:
            continue
        sub.append(f'{toml_key} = "{_toml_escape(fields[wkey])}"')
        sub_keys_emitted += 1

    while pre_lines and pre_lines[-1].strip() == "":
        pre_lines.pop()
    parts: list[str] = list(pre_lines)
    if parts:
        parts.append("")  # separator before our top section
    parts.extend(top)
    if sub_keys_emitted > 0:
        parts.append("")  # blank line between top and subtable
        parts.extend(sub)
    parts.append("")  # trailing newline
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


_VALID_CLOUD_PROVIDERS = ("browser-use", "browserless", "hyperbrowser", "generic")
_VALID_CLOUD_AUTH_KINDS = ("bearer", "basic", "mtls")
# Auth kinds the wizard recognises by name but refuses (until shipped).
# Keeps the user from typing a typo and getting "unknown auth_kind" when
# the real answer is "not yet implemented".
_COMING_CLOUD_AUTH_KINDS = {"oauth2": "v0.6"}


def _collect_cloud_fields(provider: str, auth_kind: str,
                          extras: Optional[dict] = None) -> dict:
    """Per-auth-kind credential *references* — never the secret itself.

    bearer → env var **name** holding the token (e.g. ``BROWSER_USE_API_KEY``);
             the daemon reads the live value at startup.
    basic  → endpoint URL with ``user:pass@`` embedded (URL-RFC).
    mtls   → cert + key file paths.

    ``extras`` (optional): doctor's ``extras`` dict from a previously-
    configured cloud backend entry. When present we use its values as
    prompt defaults so re-running ``browserwright install`` doesn't
    require the user to re-type everything.
    """
    extras = extras or {}
    out: dict = {"cloud_provider_hint": provider, "cloud_auth_kind": auth_kind}
    if auth_kind == "bearer":
        default_envvar = extras.get("token_env") or "BROWSER_USE_API_KEY"
        envvar = _prompt(
            "Env var name holding the bearer token (e.g. BROWSER_USE_API_KEY)",
            default=default_envvar,
        )
        if not envvar:
            raise ValueError("bearer auth requires an env-var name")
        out["cloud_token_env"] = envvar
        endpoint = _prompt("Cloud endpoint (wss://...)",
                           default=extras.get("endpoint") or "")
        if endpoint:
            out["cloud_endpoint"] = endpoint
    elif auth_kind == "basic":
        # v0.5 — header-mode basic auth (daemon ``BasicAuth`` default).
        # Credentials live in env at daemon `serve` time; the wizard only
        # collects the env-var **names**. URL stays credential-free.
        default_user_env = extras.get("username_env") or "BROWSERLESS_USER"
        default_pass_env = extras.get("password_env") or "BROWSERLESS_PASS"
        user_env = _prompt(
            "Env var name holding the basic-auth username (e.g. BROWSERLESS_USER)",
            default=default_user_env,
        )
        pass_env = _prompt(
            "Env var name holding the basic-auth password (e.g. BROWSERLESS_PASS)",
            default=default_pass_env,
        )
        if not user_env or not pass_env:
            raise ValueError("basic auth requires both username_env and password_env")
        out["cloud_username_env"] = user_env
        out["cloud_password_env"] = pass_env
        endpoint = _prompt(
            "Cloud endpoint (bare URL, no creds — wss://api.browserless.io/ws)",
            default=extras.get("endpoint") or "",
        )
        if not endpoint:
            raise ValueError("basic auth requires an endpoint URL")
        if "@" in endpoint:
            # URL-embedded creds are a daemon-side opt-in
            # (``embed_in_url=true``). The wizard's default flow uses
            # header mode; refuse the embedded form to avoid storing the
            # secret in plain memory.
            raise ValueError(
                "basic auth wizard collects credentials via env-var names "
                "(daemon header mode). Strip user:pass@ from the URL and "
                "supply env-var names instead."
            )
        out["cloud_endpoint"] = endpoint
    elif auth_kind == "mtls":
        cert = _prompt("Path to client cert (PEM)",
                       default=extras.get("cert_file") or "")
        key = _prompt("Path to client key (PEM)",
                      default=extras.get("key_file") or "")
        if not cert or not key:
            raise ValueError("mtls auth requires both cert and key paths")
        out["cloud_cert_file"] = cert
        out["cloud_key_file"] = key
        endpoint = _prompt("Cloud endpoint (wss://...)",
                           default=extras.get("endpoint") or "")
        if endpoint:
            out["cloud_endpoint"] = endpoint
    else:
        raise ValueError(f"unknown auth_kind: {auth_kind!r}")
    return out


def run() -> int:
    print("browserwright install wizard")
    print("=" * 32)
    print()
    # Single shared doctor probe (spec H3 zero-side-effect contract). Options
    # 3 (extension) and 4 (cloud) consume the same dict so the wizard pays
    # at most one subprocess regardless of backend count.
    backends = _wizard_doctor_backends()
    ext_live = _backend_available_from(backends, "extension")
    cloud_entry = _cloud_backend_entry(backends)
    cloud_live = bool(cloud_entry and cloud_entry.get("available"))
    cloud_extras = (cloud_entry or {}).get("extras") or {}

    print("Pick how Skill should connect to Chrome:")
    for key, _backend, label, desc in _OPTIONS:
        if key == "3" and not ext_live:
            shown_label = f"{label}  (daemon reports extension backend not yet available)"
        elif key == "4" and not cloud_live:
            shown_label = f"{label}  (daemon reports cloud backend not yet available)"
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
    _key, backend, label, _desc = match

    if choice == "3" and not ext_live:
        print()
        print("Extension backend is not yet available in your installed daemon.")
        print("Re-run this wizard after upgrading to a daemon build that ships")
        print("the extension backend, or pick option 1 / 2 / 4 for now.")
        return 1
    if choice == "4" and not cloud_live:
        print()
        print("Cloud backend is not yet available in your installed daemon.")
        print("Re-run this wizard after upgrading to a daemon build that ships")
        print("the cloud backend, or pick option 1 / 2 / 3 for now.")
        return 1

    extra_note = label
    cloud_fields: dict = {}
    if choice == "2":
        port = _prompt("Fingerprint browser CDP port (e.g. 9223)", default="9222")
        try:
            int(port)
        except ValueError:
            print(f"port must be an integer, got {port!r}", file=sys.stderr)
            return 1
        extra_note = f"{label}, port={port}"

    if choice == "4":
        # Pre-fill prompts from doctor's ``extras`` when the daemon
        # already has a cloud config — re-running install shouldn't make
        # the user re-type unchanged fields.
        default_provider = cloud_extras.get("provider") or "browser-use"
        provider = _prompt(
            "Provider (browser-use / browserless / hyperbrowser / generic)",
            default=default_provider,
        )
        if provider not in _VALID_CLOUD_PROVIDERS:
            print(f"unknown provider: {provider!r}. "
                  f"Pick one of {_VALID_CLOUD_PROVIDERS}.", file=sys.stderr)
            return 1
        default_auth = cloud_extras.get("auth_kind") or "bearer"
        auth_kind = _prompt(
            "Auth kind (bearer / basic / mtls; oauth2 coming v0.6)",
            default=default_auth,
        )
        if auth_kind in _COMING_CLOUD_AUTH_KINDS:
            target_ver = _COMING_CLOUD_AUTH_KINDS[auth_kind]
            print(f"{auth_kind!r} auth is coming in {target_ver} — "
                  "not yet supported by daemon. Pick bearer / basic / mtls.",
                  file=sys.stderr)
            return 1
        if auth_kind not in _VALID_CLOUD_AUTH_KINDS:
            print(f"unknown auth_kind: {auth_kind!r}. "
                  f"Pick one of {_VALID_CLOUD_AUTH_KINDS}.", file=sys.stderr)
            return 1
        try:
            cloud_fields = _collect_cloud_fields(provider, auth_kind,
                                                  extras=cloud_extras)
        except ValueError as e:
            print(f"cloud setup aborted: {e}", file=sys.stderr)
            return 1
        extra_note = f"{label}, provider={provider}, auth={auth_kind}"

    print()
    print(f"Selected: {label}")
    if not _yesno("Persist this preference to ~/.browserwright/global.md?"):
        print("ok, leaving global memory untouched. You can run the wizard again later.")
        return 0

    # Direct write — install wizard *is* the user confirmation step, so we
    # call set_preference with confirm=False intentionally.
    mem = global_memory()
    result = mem.set_preference("daemon.preferred_backend", backend, confirm=False)
    mem.set_preference("daemon.notes", extra_note, confirm=False)
    # v0.5: cloud-specific keys land under the same ``daemon:`` block so
    # everything cloud-relevant is co-located in one frontmatter section.
    for k, v in cloud_fields.items():
        mem.set_preference(f"daemon.{k}", v, confirm=False)
    print(f"wrote daemon.preferred_backend = {backend!r} to {mem.path}")
    if cloud_fields:
        # Daemon reads ``~/.config/browserwright-daemon/config.toml`` at
        # ``serve --backend cloud`` startup. The wizard is the canonical
        # writer of the ``[backends.cloud]`` section.
        try:
            cfg_path = _write_daemon_cloud_config(
                cloud_fields["cloud_provider_hint"],
                cloud_fields["cloud_auth_kind"],
                cloud_fields,
            )
            print(f"wrote [backends.cloud] + [backends.cloud.auth.*] sections to {cfg_path}")
        except OSError as e:
            # Best-effort — don't fail the wizard if we can't write the
            # daemon config (e.g. read-only home, sandboxed test). Memory
            # still got written so a re-run can retry.
            print(f"warning: could not write daemon config: {e}",
                  file=sys.stderr)
    if result.get("previous") and result["previous"] != backend:
        print(f"(previous was {result['previous']!r}; kept in notes)")
    print()
    print("Next steps:")
    if choice == "1":
        print("  - Run `browserwright-daemon launch-chrome` to start the isolated profile.")
        print("  - Then create a session: `browserwright session new --backend=rdp --create`.")
    elif choice == "2":
        print("  - Make sure your fingerprint browser is open on the chosen port.")
        print("  - Then attach a session: `browserwright session new --backend=rdp --attach=PORT`.")
    elif choice == "3":
        ext_dir = chrome_extension_path()
        print("  1. Install the unpacked Chrome extension:")
        if ext_dir:
            print(f"       - chrome://extensions → toggle 'Developer mode'")
            print(f"       - click 'Load unpacked' → pick:")
            print(f"           {ext_dir}")
        else:
            print("       - chrome://extensions → toggle 'Developer mode'")
            print("       - click 'Load unpacked' → pick the daemon's")
            print("         `chrome-extension/` directory.")
            print("         (Hint: `browserwright-daemon extension-path --json` prints it.)")
        print("  2. Start the daemon in extension-relay mode:")
        print("       browserwright-daemon serve --backend extension"
              f" --name {os.environ.get('BD_NAME', 'default')}")
        print("       (Mode B socket only — Mode A subprocess cannot host the")
        print("        relay; the daemon will raise DaemonUnavailable on Mode A.)")
        print("  3. In Chrome, click the extension icon → 'Attach this tab'.")
        print("     Verify with: `browserwright-daemon doctor --json` →")
        print("     look for `extension` backend `available=true` + `ws_url` set.")
        print("  4. Then create a session: `browserwright session new --backend=extension`.")
    elif choice == "4":
        provider = cloud_fields["cloud_provider_hint"]
        auth_kind = cloud_fields["cloud_auth_kind"]
        print("  1. Make sure `browserwright-daemon` v0.5+ (with cloud backend) is installed.")
        print(f"  2. Provider hint: {provider}.  Auth kind: {auth_kind}.")
        if auth_kind == "bearer":
            print(f"     Export the token before starting the daemon:")
            print(f"       export {cloud_fields['cloud_token_env']}=<your-token>")
        elif auth_kind == "basic":
            print(f"     Export the basic-auth env vars before starting the daemon:")
            print(f"       export {cloud_fields['cloud_username_env']}=<your-username>")
            print(f"       export {cloud_fields['cloud_password_env']}=<your-password>")
        elif auth_kind == "mtls":
            print(f"     Cert / key paths (daemon reads these at serve startup):")
            print(f"       cert: {cloud_fields['cloud_cert_file']}")
            print(f"       key:  {cloud_fields['cloud_key_file']}")
        print("  3. Start the daemon in cloud-relay mode:")
        print(f"       browserwright-daemon serve --backend cloud --provider {provider}"
              f" --name {os.environ.get('BD_NAME', 'default')}")
        print("       (daemon-impl-2 may also expose `browserwright-daemon set-credentials`")
        print("        — check `browserwright-daemon --help` once v0.5 is installed.)")
        print("  4. Verify: `browserwright-daemon doctor --json` →")
        print("     look for `cloud` backend `available=true` + `ws_url` set.")
    return 0
