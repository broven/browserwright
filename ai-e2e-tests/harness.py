"""
browser-skill AI E2E harness — spawns a real Claude agent (via Claude Agent
SDK) and asks it to drive `browser-skill` through the four user stories in
`browser-skill/design.md §0`.

Run:
    .venv/bin/python harness.py [--dry-run] [--only US1[,US2,...]]

See README.md for prerequisites and what each US asserts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

# Force all httpx calls to bypass any system-wide proxy. The user has
# `http_proxy=http://127.0.0.1:6152` set (ClashX-style local proxy), which
# would otherwise eat our 127.0.0.1:9444 requests to Chrome's DevTools.
_HTTPX_KW: dict[str, Any] = {"trust_env": False}

# Repo paths (the harness is intentionally pinned to the layout of this repo).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent                              # .../labs/browser
SKILL_DIR = REPO_ROOT / "browser-skill"
DAEMON_DIR = REPO_ROOT / "browser-daemon"
SKILL_VENV_BIN = SKILL_DIR / ".venv" / "bin"
DAEMON_VENV_BIN = DAEMON_DIR / ".venv" / "bin"
SKILL_BIN = SKILL_VENV_BIN / "browser-skill"
DAEMON_BIN = DAEMON_VENV_BIN / "browser-daemon"

# Test scratch dirs.
ISOLATED_PROFILE = Path("/tmp/ai-e2e-profile")
BS_HOME = Path("/tmp/ai-e2e-bs-home")
ISOLATED_PORT = 9444
# The canonical autoconnect / `rdp` default port. If a Chrome is listening
# here when our harness starts, it's almost certainly the user's daily
# Chrome — refusing to start prevents a stray daemon-side fallback from
# punching into it. Kept as a module constant so it's easy to grep.
AUTOCONNECT_DEFAULT_PORT = 9222
TRANSCRIPT_DIR = HERE / "transcripts"
# Auto-generated raw report (pass/fail + transcript excerpts). The
# human-written findings report at HERE / "AI-E2E-REPORT.md" is preserved
# across runs; that's where framework-gap analysis lives.
REPORT_PATH = HERE / "AI-E2E-REPORT.auto.md"

# Per-turn timeout for the agent. US3 can require several heavy turns.
AGENT_TURN_TIMEOUT = 300  # seconds
# Idle timeout — if the agent stops emitting messages for this long, kill it.
AGENT_IDLE_TIMEOUT = 180

# ---------------------------------------------------------------------------
# System prompt — condensed browser-skill SKILL.md for the agent.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an AI agent under test. Your job is to drive `browser-skill` — a
CLI that lets agents control a Chrome browser via CDP — to satisfy each
user request. A real isolated Chrome is already running and reachable
through environment variables `BD_BACKEND=rdp` and `BD_PORT=9444`.
`BS_HOME` is set to a clean scratch dir, so any memory/site-skills writes
land there (no risk of polluting the user's data).

You have a Bash tool. Invoke `browser-skill` like this:

    browser-skill <<'PY'
    print(page_info())
    PY

The heredoc form is mandatory for multi-line scripts (it prevents shell
quoting from mangling embedded Python). Helpers are pre-imported — do not
import them yourself.

For workflows where multiple calls need to share REPL state (notably:
when `propose_solidify()` needs to see what you just did so it can score
readiness), use the long-lived REPL daemon instead:

    browser-skill repl start                      # one popup-free ws,
                                                  # session persists
    browser-skill exec 'print(page_info())'       # routed to daemon,
                                                  # shares history
    browser-skill exec 'remember(...)'
    browser-skill exec 'print(propose_solidify(name_hint=...))'
    browser-skill repl stop                       # tear down

Inline heredoc each call = fresh process = no shared history (so
propose_solidify will see nothing). `repl start` + `exec` = one daemon =
all calls share session = propose_solidify can see your trail.

Core primitives (all pre-imported in every browser-skill REPL invocation):

  goto_url(url)              # navigate the currently-attached tab
  new_tab(url="about:blank") # open and switch to a new tab
  switch_tab(target)
  current_page()             # ★US1★ user's visually-focused tab
  page_info()                # {url, title, w, h, ...}
  capture_screenshot(path=None)
  click_at_xy(x, y)
  fill_input(selector, text)
  press_key(key)
  scroll(x, y, dy=-300)
  js(expression, target_id=None)   # evaluate JS, returns value
  cdp(method, **params)            # raw CDP escape hatch
  wait_for_load(timeout=15.0)
  wait_for_element(selector, timeout=10.0)
  http_get(url)              # bypass browser entirely for static pages

Memory + site-skills:

  remember(host_or_url, text)     # ★US2★ append a non-obvious finding to
                                  # site memory. Auto-creates the dir.
  remember_global(text)
  remember_preference(key, value, confirm=True)
                                  # ★US4★ structured preference write to
                                  # global.md frontmatter. Raises
                                  # NeedsUserConfirm on the first call so
                                  # *you* (the agent) ask the user.
                                  # After the user says yes, re-call with
                                  # confirm=False.
  bootstrap_site(host)            # explicit dir creation (rarely needed)
  memory_read(site=None)          # bundle of global + per-site memory
  propose_solidify(name_hint=None) -> dict | None
                                  # ★US3★ After a successful one-shot,
                                  # ask Skill whether this looks
                                  # solidifiable. None = no. Dict = a
                                  # ready-to-commit spec.
  solidify(spec)                  # commit the dict from propose_solidify
                                  # (or use `browser-skill save <site>/<name>
                                  # --json-spec='...'`).

Working patterns:

  - Screenshots first when navigating something unfamiliar.
  - After actions that change page state, re-screenshot or re-`page_info()`
    to verify before assuming success.
  - Use `http_get` for static-content scraping when no JS is needed —
    it is much faster than the browser path.
  - If you hit an auth wall or CAPTCHA, stop and report to the user. Do
    not try to log in.

NEVER:
  - Modify browser-skill or browser-daemon source code. You are the user
    of these tools, not their developer.
  - Spawn extra Chrome processes. The isolated Chrome is already running.
  - Touch the user's daily Chrome (Allow-popup hazard).

When the user asks you to do something, perform the task, then briefly
summarize what you found / did. Prefer concise answers.
"""

# ---------------------------------------------------------------------------
# Infrastructure: isolated Chrome lifecycle
# ---------------------------------------------------------------------------


def _port_is_listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _kill_listeners_on_port(port: int) -> None:
    """Best-effort: kill anything already bound to our test port."""
    try:
        out = subprocess.check_output(["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for pid in out.splitlines():
        with suppress(Exception):
            os.kill(int(pid), signal.SIGTERM)
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Safety pre-flight — refuses to start if we'd accidentally hit the user's
# daily Chrome. These were added after a debug-iteration accident where
# `BD_BACKEND=rdp BD_PORT=9444` resolved to port 9222 (user's daily Chrome,
# not the harness's isolated Chrome) because `BD_PORT` is not honored by
# the rdp backend — gap #1 in AI-E2E-REPORT.md.
# ---------------------------------------------------------------------------


def assert_safe_environment(*, allow_port_9222_listener: bool = False) -> None:
    """Refuse to start if the autoconnect default port has a listener.

    Rationale: any Chrome on `:9222` is, by convention, the user's daily
    Chrome (running with autoconnect / `DevToolsActivePort`). Our test
    Chrome lives on a different port, so we never have a legitimate reason
    to find one here. If we do, the safe play is to bail out *before* any
    daemon fallback chain can drift onto it and pop the Allow dialog.

    Escape valve: `--allow-port-9222-listener` (CLI flag, explicit per
    run) lets a knowledgeable operator override this check. The other
    safety check — `assert_daemon_resolves_to_isolated` — is *not*
    bypassable and remains the load-bearing guarantee: even if the user's
    daily Chrome is up, the harness will only proceed when
    `browser-daemon url` resolves to our isolated port.
    """
    if ISOLATED_PORT == AUTOCONNECT_DEFAULT_PORT:
        return
    if _port_is_listening("127.0.0.1", AUTOCONNECT_DEFAULT_PORT):
        if allow_port_9222_listener:
            print(
                f"[setup] WARNING: a Chrome is listening on "
                f":{AUTOCONNECT_DEFAULT_PORT} (likely the user's daily Chrome). "
                f"Proceeding because --allow-port-9222-listener was given. "
                f"The daemon-url assertion remains the load-bearing safety "
                f"check; it must point at :{ISOLATED_PORT} or the harness "
                f"will still bail.",
                file=sys.stderr,
            )
            return
        raise RuntimeError(
            f"REFUSING TO START: a Chrome is already listening on "
            f":{AUTOCONNECT_DEFAULT_PORT} (the autoconnect default port). "
            f"This is almost certainly the user's daily Chrome. The "
            f"harness's isolated Chrome lives on :{ISOLATED_PORT}, but the "
            f"browser-daemon fallback chain can drift to :{AUTOCONNECT_DEFAULT_PORT} "
            f"and trigger an 'Allow remote debugging' popup that accumulates "
            f"and can freeze Chrome (per memory: chrome-popup-accumulation-bug). "
            f"Either shut down the Chrome on :{AUTOCONNECT_DEFAULT_PORT}, or "
            f"pass --allow-port-9222-listener if you understand the risk "
            f"(the daemon-url resolution check downstream will still refuse "
            f"to proceed unless the daemon actually points at :{ISOLATED_PORT})."
        )


def assert_daemon_resolves_to_isolated(env: dict[str, str]) -> None:
    """After Chrome is up, ask `browser-daemon url` what it would resolve
    to. Refuse if the URL doesn't point at our isolated port.

    This catches scenarios like the original debug-iteration bug: env was
    `BD_BACKEND=rdp BD_PORT=9444` but the rdp backend ignored BD_PORT and
    defaulted to 9222 — so every primitive call would have hit the user's
    daily Chrome on :9222. With this assertion, that misconfiguration
    fails loudly before any test runs.
    """
    proc = subprocess.run(
        [str(DAEMON_BIN), "url"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`browser-daemon url` failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    url = proc.stdout.strip()
    expected_substr = f":{ISOLATED_PORT}/"
    if expected_substr not in url:
        raise RuntimeError(
            f"REFUSING TO START: `browser-daemon url` resolved to {url!r}, "
            f"which does NOT contain {expected_substr!r}. The harness is "
            f"misconfigured — daemon would point browser-skill at the wrong "
            f"Chrome (likely the user's daily Chrome via autoconnect). "
            f"Check env_for_agent(): BD_CDP_URL should be set to "
            f"http://127.0.0.1:{ISOLATED_PORT}."
        )
    print(f"[setup] daemon url assertion passed: {url}", file=sys.stderr)


ISOLATED_PROFILE_NAME = "ai-e2e"  # short, matches BD_NAME regex


def launch_isolated_chrome() -> subprocess.Popen:
    """Start an isolated Chrome on ISOLATED_PORT via `browser-daemon launch-chrome`.

    Post browser-daemon 0.4.1:
      - The Chrome 148 macOS launcher-exit race is fixed (the daemon now
        distinguishes launcher exit from grandchild exit and keeps polling
        until either DevToolsActivePort or /json/version succeeds).
      - launch-chrome injects `--remote-allow-origins=*` itself, so we no
        longer have to add it manually for WS handshakes to succeed.

    The launched Chrome runs in a *separate* user-data-dir
    (`~/.cache/browser-daemon/profiles/ai-e2e`) — your daily Chrome is
    untouched. No Allow popups (rdp backend on isolated profile).

    Returns a `subprocess.Popen` proxy — the actual Chrome was detached by
    the daemon, so this Popen handle is only a sentinel; teardown goes
    through `kill_isolated_chrome()` (which closes /json + SIGTERMs the
    port listener).
    """
    if _port_is_listening("127.0.0.1", ISOLATED_PORT):
        print(f"[setup] port {ISOLATED_PORT} already in use — killing stale listener",
              file=sys.stderr)
        _kill_listeners_on_port(ISOLATED_PORT)

    # Wipe the profile dir managed by launch-chrome — leftover SingletonLock
    # files from aborted runs will refuse Chrome on the next launch otherwise.
    profile_dir = Path.home() / ".cache" / "browser-daemon" / "profiles" / ISOLATED_PROFILE_NAME
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)

    print(f"[setup] launching isolated Chrome on :{ISOLATED_PORT} via "
          f"`browser-daemon launch-chrome --profile {ISOLATED_PROFILE_NAME}`",
          file=sys.stderr)

    # IMPORTANT: pass env_for_agent() — particularly BD_CHROME_BINARY (set
    # to bypass the discover_chrome_binary broken-wrapper bug, see
    # env_for_agent for context).
    proc = subprocess.run(
        [
            str(DAEMON_BIN), "launch-chrome",
            "--profile", ISOLATED_PROFILE_NAME,
            "--port", str(ISOLATED_PORT),
            "--persistent",
            "--detach",
            "--json",
        ],
        env=env_for_agent(),
        capture_output=True, text=True, timeout=45,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"browser-daemon launch-chrome failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"browser-daemon launch-chrome stdout was not JSON: {proc.stdout!r}"
        )
    ws_url = result.get("ws_url")
    pid = (result.get("extras") or {}).get("pid")
    print(f"[setup] Chrome ready: ws_url={ws_url} pid={pid}", file=sys.stderr)

    # Double-check it's actually serving DevTools at our port. The daemon's
    # own readiness handshake might pass while our port is still negotiating;
    # one more probe before we declare ready costs nothing.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"http://127.0.0.1:{ISOLATED_PORT}/json/version",
                timeout=2.0, **_HTTPX_KW,
            )
            if r.status_code == 200 and "webSocketDebuggerUrl" in r.json():
                # We return a dummy Popen so main()'s `proc` handle stays
                # typed correctly; the actual Chrome was spawned detached
                # by the daemon (and so isn't waitable from here anyway).
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL)
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("isolated Chrome never confirmed on /json/version after launch-chrome reported ready")


def kill_isolated_chrome() -> None:
    """Tear down by closing the DevTools `/json/close/<id>` endpoints and
    killing anything still bound to the port. The Chrome that launch-chrome
    spawned is detached — we can't easily Popen.kill() it."""
    if _port_is_listening("127.0.0.1", ISOLATED_PORT):
        # Politely close all tabs first.
        with suppress(Exception):
            r = httpx.get(f"http://127.0.0.1:{ISOLATED_PORT}/json", timeout=2.0, **_HTTPX_KW)
            for tab in r.json() or []:
                tid = tab.get("id")
                if tid:
                    with suppress(Exception):
                        httpx.get(f"http://127.0.0.1:{ISOLATED_PORT}/json/close/{tid}", timeout=1.0, **_HTTPX_KW)
        # Then SIGTERM the listener.
        _kill_listeners_on_port(ISOLATED_PORT)


def seed_us1_page() -> None:
    """Open example.com in the isolated Chrome so US1's 'read the current
    page' has something deterministic to read."""
    # PUT /json/new?<url> creates a new tab at that URL.
    url = "https://example.com/"
    with httpx.Client(**_HTTPX_KW) as c:
        # Find or open a tab on example.com.
        r = c.get(f"http://127.0.0.1:{ISOLATED_PORT}/json", timeout=5.0)
        existing = [t for t in r.json() if t.get("type") == "page" and url in t.get("url", "")]
        if not existing:
            # PUT (new spec) or GET (legacy) — try both.
            try:
                c.put(f"http://127.0.0.1:{ISOLATED_PORT}/json/new?{url}", timeout=10.0)
            except Exception:
                c.get(f"http://127.0.0.1:{ISOLATED_PORT}/json/new?{url}", timeout=10.0)
        # Give Chrome a moment to actually load.
        time.sleep(3)
        # Activate it so getActiveTab returns it.
        r = c.get(f"http://127.0.0.1:{ISOLATED_PORT}/json", timeout=5.0)
        for t in r.json():
            if t.get("type") == "page" and "example.com" in t.get("url", ""):
                with suppress(Exception):
                    c.get(f"http://127.0.0.1:{ISOLATED_PORT}/json/activate/{t['id']}", timeout=5.0)
                return


def reset_bs_home() -> None:
    """Wipe the test BS_HOME so each run starts clean."""
    if BS_HOME.exists():
        shutil.rmtree(BS_HOME)
    BS_HOME.mkdir(parents=True, exist_ok=True)


def env_for_agent() -> dict[str, str]:
    """The env the spawned Claude agent inherits. Puts our venv bins on PATH
    and points everything at the isolated Chrome + scratch BS_HOME."""
    env = dict(os.environ)
    env["PATH"] = f"{SKILL_VENV_BIN}:{DAEMON_VENV_BIN}:" + env.get("PATH", "")
    # Canonical path (post browser-daemon 0.4.1):
    #   - BD_BACKEND=rdp pins the resolver to the rdp backend only
    #   - BD_RDP_PORT=9444 directs rdp at our isolated port
    # The chain-lock (BD_BACKEND pinned, no fallback) is still the
    # critical safety property — if rdp fails for any reason, the
    # resolver raises BackendUnavailable instead of cascading to
    # autoconnect / user's daily Chrome.
    env["BD_BACKEND"] = "rdp"
    env["BD_RDP_PORT"] = str(ISOLATED_PORT)
    env["BS_HOME"] = str(BS_HOME)
    # `browser-daemon` 0.4.1's `discover_chrome_binary()` resolves PATH names
    # in order (google-chrome → google-chrome-stable → chromium → ...). On
    # this system, `which chromium` returns the brew wrapper script
    # `/opt/homebrew/bin/chromium`, which `exec`s a non-existent
    # `/Applications/Chromium.app/Contents/MacOS/Chromium` and exits with
    # code 126 — exactly the "launcher exited 126" symptom the team thought
    # they fixed in 0.4.1. They fixed the poll race; the underlying
    # binary-discovery picks a broken executable. Hard-pin Google Chrome
    # here as a workaround; report filed alongside.
    chrome_app = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome_app.exists():
        env["BD_CHROME_BINARY"] = str(chrome_app)
    # Belt-and-suspenders: clear conflicting env-backend overrides so the
    # rdp backend is the only path that can succeed.
    env.pop("BD_CDP_WS", None)
    env.pop("BD_CDP_URL", None)
    # Defense-in-depth safety env. As of browser-daemon 0.3.0 this var is
    # not consumed by the daemon (it's a no-op), but it's the natural name
    # for the hardening the daemon team has on its plate (filed alongside
    # BD_RDP_PORT). When they ship it, this becomes a hard block; setting
    # it pre-emptively means the harness opts in to the safety net as soon
    # as it exists. If daemon picks a different env name, update here.
    env["BD_DISABLE_AUTOCONNECT"] = "1"
    # Make sure the agent's subprocesses (browser-skill -> httpx) don't
    # route 127.0.0.1 through the user's local proxy (ClashX-style).
    env["NO_PROXY"] = "127.0.0.1,localhost,*"
    env["no_proxy"] = "127.0.0.1,localhost,*"
    return env


# ---------------------------------------------------------------------------
# Agent runner — wraps Claude Agent SDK
# ---------------------------------------------------------------------------


@dataclass
class AgentTurn:
    """Captures one prompt-response round."""
    prompt: str
    assistant_text: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: Optional[str] = None
    duration_s: float = 0.0


@dataclass
class StoryRun:
    name: str
    turns: list[AgentTurn] = field(default_factory=list)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)


async def run_agent_turn(client, prompt: str) -> AgentTurn:
    """Send `prompt` and capture the streamed response. Stops when the agent
    yields a ResultMessage (which Claude Agent SDK emits at end of turn) or
    when no new message arrives for AGENT_IDLE_TIMEOUT seconds."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    turn = AgentTurn(prompt=prompt)
    start = time.time()
    await client.query(prompt)
    last_msg_at = time.time()

    try:
        async for msg in client.receive_response():
            last_msg_at = time.time()
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        turn.assistant_text.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        turn.tool_calls.append({
                            "name": block.name,
                            "input": block.input,
                        })
            elif isinstance(msg, ResultMessage):
                # End of turn.
                turn.final_text = "\n".join(turn.assistant_text).strip()
                break
            else:
                # UserMessage proxies the tool result back to the agent.
                # We try to capture any text content; structure varies by SDK
                # version, so best-effort.
                content = getattr(msg, "content", None)
                if content is not None:
                    text_chunks = []
                    if isinstance(content, list):
                        for c in content:
                            if hasattr(c, "content"):  # ToolResultBlock
                                inner = getattr(c, "content", "")
                                if isinstance(inner, list):
                                    for it in inner:
                                        if isinstance(it, dict) and "text" in it:
                                            text_chunks.append(it["text"])
                                else:
                                    text_chunks.append(str(inner))
                    if text_chunks:
                        turn.tool_results.append({"text": "\n".join(text_chunks)})

            # Idle / hard timeouts.
            if time.time() - last_msg_at > AGENT_IDLE_TIMEOUT:
                turn.error = f"idle timeout after {AGENT_IDLE_TIMEOUT}s"
                break
            if time.time() - start > AGENT_TURN_TIMEOUT:
                turn.error = f"turn timeout after {AGENT_TURN_TIMEOUT}s"
                break
    except Exception as e:
        turn.error = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)

    turn.duration_s = round(time.time() - start, 2)
    if not turn.final_text:
        turn.final_text = "\n".join(turn.assistant_text).strip()
    return turn


# ---------------------------------------------------------------------------
# User-story scripts
# ---------------------------------------------------------------------------


async def story_us1(client) -> StoryRun:
    """US1 — current-page one-shot. example.com is already loaded."""
    s = StoryRun(name="US1")
    prompt = (
        "用 browser-skill 抓**当前页面（用户视觉前台 tab）**的 H1 标题和首段文字。"
        "提示：用 current_page() 切到该 tab，再用 js() 或 page_info() 提取。"
        "完成后把结果直接告诉我（不需要保存到文件）。"
    )
    t = await run_agent_turn(client, prompt)
    s.turns.append(t)

    blob = (t.final_text + "\n" + "\n".join(r["text"] for r in t.tool_results)).lower()
    if "example domain" in blob:
        s.passed = True
        s.reasons.append("agent surfaced the 'Example Domain' H1")
    else:
        s.reasons.append("expected 'Example Domain' in agent output, not found")
    if "illustrative" in blob or "use in examples" in blob:
        s.reasons.append("first-paragraph content captured")
    if t.error:
        s.passed = False
        s.reasons.append(f"turn error: {t.error}")
    return s


async def story_us2(client) -> StoryRun:
    """US2 — new-tab one-shot + in-flight memory write."""
    s = StoryRun(name="US2")
    prompt = (
        "用 browser-skill 开**新 tab** 访问 https://news.ycombinator.com/ ，抓出"
        "首页 top 5 帖子的标题。完成后，如果你发现关于这个站点的任何非显然事实"
        "（比如 HTML 结构、稳定 selector、URL pattern），请调用 "
        "remember('news.ycombinator.com', '<事实>') 把它写进站点 memory。"
        "最后告诉我 top 5 标题 + 你 remember 了什么。"
    )
    t = await run_agent_turn(client, prompt)
    s.turns.append(t)

    # Assertions.
    blob = (t.final_text + "\n" + "\n".join(r["text"] for r in t.tool_results))
    # Title heuristic — HN front-page items use ".titleline" or similar; we
    # don't assert specific titles (volatile), only that the agent surfaced
    # at least 5 lines and looked at HN.
    if "news.ycombinator.com" in blob.lower() or "hacker news" in blob.lower():
        s.reasons.append("agent navigated to Hacker News")
    else:
        s.reasons.append("agent didn't appear to visit HN")

    # Did the agent write to site memory? host_stem("news.ycombinator.com")
    # resolves to "news" (the framework strips TLD + uses parts[0]) — so the
    # writable target is BS_HOME/site-skills/news/memory.md. Glob just in
    # case the agent passed a different host form.
    candidates = sorted((BS_HOME / "site-skills").glob("*/memory.md"))
    wrote_user = False
    written_body: Optional[str] = None
    for mem_path in candidates:
        body = mem_path.read_text(encoding="utf-8") if mem_path.exists() else ""
        # Skill bootstraps memory.md with a frontmatter + boilerplate body
        # ~250 bytes; we only count it as "agent-written" if the body is
        # appreciably larger than that.
        notes_section = body.split("## Notes", 1)[-1] if "## Notes" in body else ""
        if len(notes_section.strip()) > 30:
            wrote_user = True
            written_body = body
            s.reasons.append(f"memory written to {mem_path} (body={len(body)}b)")
            break
    if not wrote_user:
        s.reasons.append(
            f"no site-memory file with agent-written content under "
            f"BS_HOME/site-skills/*/memory.md"
        )

    # Pass = visited HN AND wrote SOMETHING to user memory.
    s.passed = wrote_user and "agent navigated to Hacker News" in s.reasons
    if t.error:
        s.passed = False
        s.reasons.append(f"turn error: {t.error}")
    return s


async def story_us3(client) -> StoryRun:
    """US3 — one-shot then propose_solidify, user accepts, agent saves."""
    s = StoryRun(name="US3")

    # Turn 1: do a small task, then ask Skill if it's solidifiable.
    prompt1 = (
        "我要把 'wikipedia.org/lookup' 这种小任务做成可复用脚本。"
        "请用 browser-skill 完成这一次任务：用新 tab 打开 "
        "https://en.wikipedia.org/wiki/Python_(programming_language) ，"
        "提取首段文字。完成后，调用 propose_solidify(name_hint='lookup') 看 Skill"
        "是否建议把它固化成 task。把它返回的 dict（或 None）原样打印给我看，"
        "然后等我决定是否 commit。"
    )
    t1 = await run_agent_turn(client, prompt1)
    s.turns.append(t1)

    # Turn 2: user says yes, agent commits.
    prompt2 = (
        "好，请把这个 task 固化下来。用 `browser-skill save <site>/<name> "
        "--json-spec='{...}'` 或调用 solidify(spec) 提交。完成后告诉我创建了"
        "哪个文件。"
    )
    t2 = await run_agent_turn(client, prompt2)
    s.turns.append(t2)

    # Assertion: any new task .py file in BS_HOME/site-skills/*/tasks/.
    # host_stem("en.wikipedia.org") = "en" — the framework's stem rule is
    # naive parts[0] after stripping www/m, so we don't predict the dir
    # name; we glob.
    any_task = sorted((BS_HOME / "site-skills").glob("*/tasks/*.py"))
    if any_task:
        s.passed = True
        s.reasons.append(f"task file created: {any_task[0]}")
    else:
        s.reasons.append("no new task .py file in BS_HOME/site-skills/*/tasks/")
    if t1.error or t2.error:
        s.passed = False
        s.reasons.append(f"turn errors: t1={t1.error} t2={t2.error}")
    return s


# ---------------------------------------------------------------------------
# US5 cloud-backend infrastructure
#
# US5 exercises the daemon's `cloud` backend (v0.5) end-to-end: the agent
# drives browser-skill, which talks to a daemon running in Mode B, which
# resolves and authenticates an upstream ws against a fake cloud-browser
# service, which itself proxies CDP to our isolated Chrome on :9444.
#
# Five processes in the chain:
#   1. isolated Chrome (existing, :9444)
#   2. fake cloud server (subprocess, :9555, auth gate + ws proxy)
#   3. browser-daemon serve --backend cloud (subprocess, Mode B unix socket)
#   4. browser-skill (per agent invocation, talks to daemon's socket)
#   5. Claude agent (our SDK client)
#
# All of (2) and (3) are owned by this harness; cleanup must kill both.
# ---------------------------------------------------------------------------

CLOUD_PORT = 9555
CLOUD_TOKEN = "us5-fake-token-do-not-trust"
CLOUD_DAEMON_NAME = "us5cloud"
CLOUD_CONFIG_PATH = Path("/tmp/ai-e2e-cloud-config.toml")


def _cloud_daemon_socket() -> Path:
    """Mirror browser-daemon's runtime_dir() logic for socket location.

    On macOS the daemon falls back to /tmp because $XDG_RUNTIME_DIR isn't
    set; on Linux it would honor XDG. The skill's Mode B client computes
    the same path from BD_NAME, so this must agree.
    """
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    return base / f"browser-daemon-{CLOUD_DAEMON_NAME}.sock"


def write_cloud_config() -> Path:
    """Write a daemon config.toml that selects the cloud backend with
    bearer auth pulled from US5_FAKE_TOKEN. Returns the config path."""
    CLOUD_CONFIG_PATH.write_text(
        textwrap.dedent(f"""\
        # ai-e2e cloud-backend test config — auto-generated by harness.py.
        # Picks the cloud backend, pointed at our local fake cloud-browser
        # service (which proxies upstream to the isolated Chrome on :9444).
        # Bearer token comes from US5_FAKE_TOKEN env var.

        default_backend = "cloud"

        [backends.cloud]
        endpoint = "http://127.0.0.1:{CLOUD_PORT}"
        auth_kind = "bearer"
        provider_hint = "ai-e2e-fake"

        [backends.cloud.auth.bearer]
        token_env = "US5_FAKE_TOKEN"
        """),
        encoding="utf-8",
    )
    return CLOUD_CONFIG_PATH


def launch_fake_cloud_server() -> subprocess.Popen:
    """Spawn fake_cloud_server.py and wait for its 'ready' marker on stderr.
    Token + port are pinned (CLOUD_TOKEN, CLOUD_PORT)."""
    if _port_is_listening("127.0.0.1", CLOUD_PORT):
        _kill_listeners_on_port(CLOUD_PORT)
    env = dict(os.environ)
    env["US5_FAKE_TOKEN"] = CLOUD_TOKEN
    proc = subprocess.Popen(
        [
            str(HERE / ".venv" / "bin" / "python"),
            str(HERE / "fake_cloud_server.py"),
            "--port", str(CLOUD_PORT),
            "--upstream-port", str(ISOLATED_PORT),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for the ready marker (printed on stderr) or a fast exit.
    deadline = time.time() + 10
    ready_seen = False
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"fake_cloud_server exited prematurely: {err!r}")
        # Probe HTTP endpoint as the real readiness signal.
        try:
            r = httpx.get(
                f"http://127.0.0.1:{CLOUD_PORT}/json/version",
                headers={"Authorization": f"Bearer {CLOUD_TOKEN}"},
                timeout=1.0, **_HTTPX_KW,
            )
            if r.status_code == 200:
                ready_seen = True
                break
        except Exception:
            pass
        time.sleep(0.2)
    if not ready_seen:
        with suppress(Exception):
            proc.terminate()
        raise RuntimeError(f"fake_cloud_server never answered on :{CLOUD_PORT}")
    print(f"[setup] fake cloud server ready on :{CLOUD_PORT}", file=sys.stderr)
    return proc


def launch_cloud_daemon() -> subprocess.Popen:
    """Spawn `browser-daemon serve --backend cloud` and wait for its
    unix socket to appear. Cleans up any stale socket first."""
    sock = _cloud_daemon_socket()
    if sock.exists():
        with suppress(Exception):
            sock.unlink()

    env = dict(os.environ)
    env["BD_NAME"] = CLOUD_DAEMON_NAME
    env["BD_BACKEND"] = "cloud"
    env["BD_CONFIG"] = str(CLOUD_CONFIG_PATH)
    env["US5_FAKE_TOKEN"] = CLOUD_TOKEN
    env["NO_PROXY"] = "127.0.0.1,localhost,*"
    env["no_proxy"] = "127.0.0.1,localhost,*"

    proc = subprocess.Popen(
        [str(DAEMON_BIN), "serve", "--backend", "cloud",
         "--name", CLOUD_DAEMON_NAME, "--config", str(CLOUD_CONFIG_PATH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"browser-daemon serve exited prematurely (rc={proc.returncode}): "
                f"stdout={out!r} stderr={err!r}"
            )
        if sock.exists():
            break
        time.sleep(0.2)
    else:
        with suppress(Exception):
            proc.terminate()
        raise RuntimeError(f"daemon socket {sock} never appeared")
    print(f"[setup] cloud daemon ready: socket={sock}", file=sys.stderr)
    return proc


def assert_cloud_daemon_resolves_correctly() -> None:
    """Verify that `browser-daemon url` via our cloud-configured daemon
    returns a ws URL pointing at the fake cloud (:9555), NOT the user's
    daily Chrome (:9222) or anywhere else.

    Equivalent to assert_daemon_resolves_to_isolated() but for the cloud
    pipeline. Without this guard, a misconfigured cloud daemon could
    silently fall through to a different backend.
    """
    env = dict(os.environ)
    env["PATH"] = f"{DAEMON_VENV_BIN}:{env.get('PATH', '')}"
    env["BD_NAME"] = CLOUD_DAEMON_NAME
    env["BD_BACKEND"] = "cloud"
    env["BD_CONFIG"] = str(CLOUD_CONFIG_PATH)
    env["US5_FAKE_TOKEN"] = CLOUD_TOKEN
    env["NO_PROXY"] = "127.0.0.1,localhost,*"

    proc = subprocess.run(
        [str(DAEMON_BIN), "url"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cloud `browser-daemon url` failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    url = proc.stdout.strip()
    if f":{CLOUD_PORT}/" not in url:
        raise RuntimeError(
            f"REFUSING TO PROCEED: cloud `browser-daemon url` resolved to {url!r}, "
            f"which does NOT contain :{CLOUD_PORT}/ — fake cloud server should "
            f"be the only valid hit. Misconfiguration could route through real "
            f"Chrome."
        )
    print(f"[setup] cloud daemon url assertion passed: {url}", file=sys.stderr)


def teardown_cloud_infra(*procs: Optional[subprocess.Popen]) -> None:
    """Best-effort teardown of fake cloud server + cloud daemon."""
    # Ask daemon to stop politely first (releases its upstream ws cleanly).
    with suppress(Exception):
        env = dict(os.environ)
        env["BD_NAME"] = CLOUD_DAEMON_NAME
        env["BD_CONFIG"] = str(CLOUD_CONFIG_PATH)
        subprocess.run(
            [str(DAEMON_BIN), "stop"],
            env=env, capture_output=True, text=True, timeout=5,
        )
    for p in procs:
        if p is None:
            continue
        with suppress(Exception):
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
    sock = _cloud_daemon_socket()
    if sock.exists():
        with suppress(Exception):
            sock.unlink()


async def story_us5_cloud(client) -> StoryRun:
    """US5 — drive the daemon's cloud backend end-to-end.

    Setup: launch fake cloud server (proxies to isolated Chrome), launch
    `browser-daemon serve --backend cloud`, verify its `url` resolution
    lands at the fake cloud port. The agent then runs a small task with
    `BD_NAME=us5cloud` in its env — browser-skill picks Mode B
    automatically, routes calls through the daemon, which authenticates
    upstream with the bearer token, which proxies CDP to real Chrome.

    Success = agent surfaces example.com's H1 text via the cloud path.
    """
    s = StoryRun(name="US5")
    cloud_server: Optional[subprocess.Popen] = None
    cloud_daemon: Optional[subprocess.Popen] = None
    try:
        write_cloud_config()
        cloud_server = launch_fake_cloud_server()
        cloud_daemon = launch_cloud_daemon()
        assert_cloud_daemon_resolves_correctly()
    except Exception as e:
        s.reasons.append(f"setup failed: {e}")
        teardown_cloud_infra(cloud_daemon, cloud_server)
        return s

    try:
        # Snapshot the current cloud env. Agent runs with BD_NAME=us5cloud
        # so browser-skill goes Mode B → our daemon. The daemon already
        # has the cloud creds baked into its config (via BD_CONFIG); the
        # agent's env doesn't need them.
        prompt = (
            "请用 browser-skill 通过 cloud backend 完成这个简单任务：开新 tab 访问 "
            "https://example.com/ ，提取 H1 标题，把结果告诉我。\n"
            "**重要**：这次环境变量 `BD_NAME=us5cloud` 让 browser-skill 通过 Mode B 接到一个"
            "**配置为 cloud backend** 的 daemon —— daemon 用 bearer token 鉴权一个 fake cloud "
            "browser service，service 再 proxy 到隔离 Chrome。所有调用走的是云路径而不是直连。\n"
            "你不需要也不应该自己 set 任何 cloud token / endpoint —— daemon 已经配好了，"
            "你只管正常调 browser-skill 即可。"
        )

        # Build a slightly-altered env for the agent: BD_NAME=us5cloud
        # (so Mode B finds our daemon's socket), and explicit clearing of
        # the rdp port hint (it would have routed direct to :9444 otherwise).
        env = env_for_agent()
        env["BD_NAME"] = CLOUD_DAEMON_NAME
        env.pop("BD_RDP_PORT", None)
        # We don't override BD_BACKEND on the skill side — Mode B socket
        # is found by BD_NAME, then the upstream backend (cloud) is the
        # daemon's concern, not skill's.

        # The Claude SDK client was constructed once with cwd + env baked
        # in at top of real_run(). For US5 we need a DIFFERENT env. Easiest
        # path: temporarily mutate os.environ for the duration of this
        # turn — the SDK forwards the parent's env to the bash subprocess
        # it spawns for each tool call.
        #
        # Critical: if the skill's Mode B socket connect EVER fails and it
        # falls back to Mode A (`browser-daemon url` subprocess), we MUST
        # make sure that fallback resolves to cloud — not rdp/9222 (which
        # would land on the user's daily Chrome and pop the Allow dialog).
        # So we set BD_BACKEND=cloud + BD_CONFIG + the token on the agent's
        # env too, as defense-in-depth.
        track_keys = ("BD_NAME", "BD_BACKEND", "BD_RDP_PORT", "BD_CONFIG", "US5_FAKE_TOKEN")
        original = {k: os.environ.get(k) for k in track_keys}
        os.environ["BD_NAME"] = CLOUD_DAEMON_NAME
        os.environ["BD_BACKEND"] = "cloud"
        os.environ["BD_CONFIG"] = str(CLOUD_CONFIG_PATH)
        os.environ["US5_FAKE_TOKEN"] = CLOUD_TOKEN
        os.environ.pop("BD_RDP_PORT", None)

        try:
            t = await run_agent_turn(client, prompt)
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        s.turns.append(t)

        blob = (t.final_text + "\n" + "\n".join(r["text"] for r in t.tool_results)).lower()
        if "example domain" in blob:
            s.passed = True
            s.reasons.append("agent retrieved 'Example Domain' via cloud-backed Chrome")
        else:
            s.reasons.append(
                f"expected 'Example Domain' in cloud-path output; got: "
                f"{(t.final_text or '')[:300]!r}"
            )
        if t.error:
            s.passed = False
            s.reasons.append(f"turn error: {t.error}")
    finally:
        teardown_cloud_infra(cloud_daemon, cloud_server)

    return s


# ---------------------------------------------------------------------------
# US-Ext (extension backend, F-4e) — runs the daemon's `extension` backend
# end to end via a fake chrome extension that proxies to the isolated Chrome.
#
# Path A from the F-4e review finding: the chrome-extension code itself
# isn't exercised (manifest / background.js / popup), but every other layer
# of the chain is real:
#   - daemon serve --backend extension binds the relay (port from config.toml)
#   - daemon's relay accepts our fake extension's hello + tracks it
#   - skill via Mode B → daemon socket → daemon-side router → relay →
#     fake extension → per-tab CDP ws to real Chrome
#
# We pick :19989 deliberately to dodge the user's `playwriter-ws-server`
# on :19988 — daemon 0.5.3+ honors `[backends.extension].relay_url` in
# config.toml (Task #24). Pre-flight `assert_extension_relay_safe()`
# (Task #25) refuses to start if :19989 is itself busy.
# ---------------------------------------------------------------------------

EXT_RELAY_PORT = 19989
EXT_DAEMON_NAME = "us-ext"
EXT_FAKE_INSTALL_ID = "ai-e2e-fake-ext"


def assert_extension_relay_safe() -> None:
    """Refuse to proceed if the extension relay port has a foreign listener.

    Mirror of `assert_safe_environment()` for the autoconnect :9222 case.
    The user's `playwriter-ws-server` lives on :19988 (which is the
    daemon's hardcoded DEFAULT_RELAY_PORT pre-0.5.3); we pick :19989 to
    avoid it, but if SOMETHING else has claimed :19989 too we don't
    want to silently fight over it. Emit an actionable error and exit.

    See REVIEW.md F-4e for the chain of finding that motivated this
    check.
    """
    if _port_is_listening("127.0.0.1", EXT_RELAY_PORT):
        # Try to identify the squatter.
        try:
            out = subprocess.check_output(
                ["lsof", f"-iTCP:{EXT_RELAY_PORT}", "-sTCP:LISTEN"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            out = "(lsof unavailable)"
        raise RuntimeError(
            f"REFUSING TO START US-Ext: port :{EXT_RELAY_PORT} (the harness's "
            f"extension relay port) already has a listener:\n{out}\n"
            f"This is NOT the daemon — we haven't started it yet. Stop the "
            f"squatter or pick another port via EXT_RELAY_PORT module constant."
        )


def launch_extension_daemon() -> subprocess.Popen:
    """Spawn `browser-daemon serve --backend extension --extension-port N`
    and wait for its Mode B unix socket to appear.

    Uses the daemon's `--extension-port` CLI flag (Task #24 expansion,
    shipped in daemon 0.5.3+). Pre-expansion this code path needed a
    runtime config.toml; the flag is cleaner — explicit in subprocess
    args, visible in transcripts, no temp-file lifecycle to manage.
    """
    sock = Path("/tmp") / f"browser-daemon-{EXT_DAEMON_NAME}.sock"
    if sock.exists():
        with suppress(Exception):
            sock.unlink()

    env = dict(os.environ)
    env["PATH"] = f"{DAEMON_VENV_BIN}:" + env.get("PATH", "")
    env["BD_NAME"] = EXT_DAEMON_NAME
    env["BD_BACKEND"] = "extension"
    # Defense-in-depth: even with the CLI flag below, propagate the port
    # via env too so any sub-tool spawned from this daemon picks the
    # right port without re-deriving it.
    env["BD_EXTENSION_PORT"] = str(EXT_RELAY_PORT)
    env["NO_PROXY"] = "127.0.0.1,localhost,*"
    env["no_proxy"] = "127.0.0.1,localhost,*"

    proc = subprocess.Popen(
        [str(DAEMON_BIN), "serve",
         "--backend", "extension",
         "--name", EXT_DAEMON_NAME,
         "--extension-port", str(EXT_RELAY_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"`browser-daemon serve --backend extension` exited "
                f"prematurely (rc={proc.returncode}): "
                f"stdout={out!r} stderr={err!r}"
            )
        if sock.exists() and _port_is_listening("127.0.0.1", EXT_RELAY_PORT):
            break
        time.sleep(0.2)
    else:
        with suppress(Exception):
            proc.terminate()
        raise RuntimeError(
            f"extension daemon socket {sock} or relay :{EXT_RELAY_PORT} "
            f"never appeared"
        )
    print(f"[setup] extension daemon ready: socket={sock}, "
          f"relay :{EXT_RELAY_PORT}", file=sys.stderr)
    return proc


def launch_fake_extension() -> subprocess.Popen:
    """Spawn the fake chrome extension proxy. It connects to the daemon's
    relay on :19989 and proxies all CDP work to the isolated Chrome
    on :9444. We poll daemon's __status__ until `extensions == 1`.
    """
    env = dict(os.environ)
    env["FAKE_EXT_RELAY_PORT"] = str(EXT_RELAY_PORT)
    env["FAKE_EXT_UPSTREAM_PORT"] = str(ISOLATED_PORT)
    env["NO_PROXY"] = "127.0.0.1,localhost,*"

    proc = subprocess.Popen(
        [str(HERE / ".venv" / "bin" / "python"),
         str(HERE / "fake_extension.py"),
         "--relay-port", str(EXT_RELAY_PORT),
         "--upstream-port", str(ISOLATED_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # The relay reports `extensions: N` over an HTTP status endpoint
    # on the same port as the relay ws. Wait for our install_id to
    # show up — anything else is a stale extension we'd be wrong to
    # trust.
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"fake_extension exited prematurely: {err!r}"
            )
        try:
            r = httpx.get(
                f"http://127.0.0.1:{EXT_RELAY_PORT}/__status__",
                timeout=1.0, **_HTTPX_KW,
            )
            body = r.json() if r.status_code == 200 else {}
        except Exception:
            body = {}
        ids = body.get("install_ids") or []
        if EXT_FAKE_INSTALL_ID in ids:
            print(
                f"[setup] fake extension registered with relay "
                f"(install_id={EXT_FAKE_INSTALL_ID})",
                file=sys.stderr,
            )
            return proc
        time.sleep(0.2)
    with suppress(Exception):
        proc.terminate()
    raise RuntimeError("fake_extension never registered with daemon's relay")


def teardown_extension_infra(*procs: Optional[subprocess.Popen]) -> None:
    with suppress(Exception):
        subprocess.run(
            [str(DAEMON_BIN), "stop"],
            env={**os.environ,
                 "BD_NAME": EXT_DAEMON_NAME,
                 "BD_EXTENSION_PORT": str(EXT_RELAY_PORT)},
            capture_output=True, text=True, timeout=5,
        )
    for p in procs:
        if p is None:
            continue
        with suppress(Exception):
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
    sock = Path("/tmp") / f"browser-daemon-{EXT_DAEMON_NAME}.sock"
    if sock.exists():
        with suppress(Exception):
            sock.unlink()


async def story_us_ext(client) -> StoryRun:
    """US-Ext — drive the daemon's extension backend end-to-end (path A).

    Setup: launch daemon serve --backend extension on :19989 (dodging
    the user's playwriter-ws-server on :19988), launch a fake chrome
    extension that connects to the relay and proxies CDP to the
    isolated Chrome. The agent then runs `current_page()` + `page_info()`
    via Mode B — that traffic flows through the daemon → relay → fake
    extension → real Chrome, and back.

    Pass = agent surfaces example.com's title via this path.
    """
    s = StoryRun(name="US-Ext")
    ext_daemon: Optional[subprocess.Popen] = None
    fake_ext: Optional[subprocess.Popen] = None

    try:
        assert_extension_relay_safe()
        ext_daemon = launch_extension_daemon()
        fake_ext = launch_fake_extension()
    except Exception as e:
        s.reasons.append(f"setup failed: {e}")
        teardown_extension_infra(ext_daemon, fake_ext)
        return s

    try:
        prompt = (
            "请用 browser-skill 通过 **extension backend** 完成这个简单任务："
            "用 `current_page()` 拿到当前 tab，然后告诉我它的 URL + 标题（用 page_info）。\n"
            "**重要**：这次环境变量 `BD_NAME=us-ext` 让 browser-skill 通过 Mode B 接到一个"
            "**配置为 extension backend** 的 daemon — daemon 内部走 chrome-extension 协议路径"
            "（我们用一个 fake extension proxy 到真 Chrome）。所有调用走的是 extension 路径而不是"
            "直连 CDP / rdp / cloud。你不需要也不应该自己 set 任何 backend / port — daemon 已经配好了。"
        )
        track_keys = ("BD_NAME", "BD_BACKEND", "BD_RDP_PORT",
                      "BD_EXTENSION_PORT", "BD_CDP_URL")
        original = {k: os.environ.get(k) for k in track_keys}
        os.environ["BD_NAME"] = EXT_DAEMON_NAME
        os.environ["BD_BACKEND"] = "extension"
        os.environ["BD_EXTENSION_PORT"] = str(EXT_RELAY_PORT)
        os.environ.pop("BD_RDP_PORT", None)
        os.environ.pop("BD_CDP_URL", None)
        try:
            t = await run_agent_turn(client, prompt)
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        s.turns.append(t)

        blob = (t.final_text + "\n" + "\n".join(r["text"] for r in t.tool_results)).lower()
        if "example domain" in blob or "example.com" in blob:
            s.passed = True
            s.reasons.append(
                "agent surfaced the example.com title via extension-backend path"
            )
        else:
            s.reasons.append(
                f"expected 'Example Domain' / 'example.com' in extension-path output; "
                f"got: {(t.final_text or '')[:300]!r}"
            )
        if t.error:
            s.passed = False
            s.reasons.append(f"turn error: {t.error}")
    finally:
        teardown_extension_infra(ext_daemon, fake_ext)

    return s


async def story_us3_repl(client) -> StoryRun:
    """US3R — the long-lived-REPL variant of US3.

    Background: in the inline-heredoc form (story_us3), each
    `browser-skill <<'PY' ... PY` invocation is a fresh process. REPL
    history doesn't persist across invocations, so when the agent calls
    `propose_solidify()` it sees no history — and the rich diagnostic
    dict returns `readiness_score: 0` with `warnings: ["no REPL history
    yet"]`. The agent gracefully falls back to hand-writing a spec, but
    propose's "look at what you just did and propose a task" path never
    engages.

    This variant tells the agent to start a long-lived REPL daemon
    (`browser-skill repl start`) and route all calls through
    `browser-skill exec '...'`, so the daemon's in-process session holds
    history across all calls. Then `propose_solidify` should have real
    history to chew on and ideally return `ready: true` with a
    draft_run_body the agent can pipe to `solidify()`.

    Tear-down: best-effort `browser-skill repl stop` regardless of pass.
    """
    s = StoryRun(name="US3R")

    prompt1 = (
        "我要把 'wikipedia.org/lookup' 这种小任务做成可复用脚本。这次请用 "
        "**long-lived REPL daemon** 路径（不是 inline heredoc），这样 "
        "propose_solidify() 才能看到你做过哪些步骤。具体：\n"
        "1) `browser-skill repl start` 起 daemon\n"
        "2) 所有 browser-skill 调用都走 `browser-skill exec '<python>'`，让 daemon 留存历史\n"
        "3) 在 REPL 里完成：用新 tab 打开 "
        "https://en.wikipedia.org/wiki/Python_(programming_language)，提取首段文字\n"
        "4) 调 propose_solidify(name_hint='lookup')\n"
        "把它返回的完整 dict 原样打印给我看（特别注意 ready / readiness_score / draft_run_body 字段），"
        "然后等我决定是否 commit。**不要**`repl stop`，等我下一条消息。"
    )
    t1 = await run_agent_turn(client, prompt1)
    s.turns.append(t1)

    prompt2 = (
        "好，把这个 task 固化下来。如果 propose 返回的 dict 有 `ready: true` + 完整 "
        "draft_run_body，直接调 `solidify(spec)` 让 Skill 自动写。如果还是不 ready，"
        "你可以手写一个 minimal spec 然后 `solidify(...)` 或走 `browser-skill save`。"
        "完成后 `browser-skill repl stop` 关掉 daemon，并告诉我创建了哪个文件 + 用了哪条路径"
        "（auto-solidify vs hand-written）。"
    )
    t2 = await run_agent_turn(client, prompt2)
    s.turns.append(t2)

    # Assertions:
    #   - Pass = any task .py file was written
    #   - Bonus = the agent's transcript shows propose_solidify returned ready=true
    #     (i.e., REPL history actually flowed)
    any_task = sorted((BS_HOME / "site-skills").glob("*/tasks/*.py"))
    if any_task:
        s.passed = True
        s.reasons.append(f"task file created: {any_task[0]}")
    else:
        s.reasons.append("no new task .py file in BS_HOME/site-skills/*/tasks/")

    # Bonus signal: did propose_solidify's "ready: true" actually appear?
    blob = "\n".join(
        r["text"] for t in s.turns for r in t.tool_results
    ) + "\n".join(t.final_text for t in s.turns)
    if "'ready': True" in blob or '"ready": true' in blob:
        s.reasons.append("propose_solidify returned ready=True (REPL history captured)")
    elif "no REPL history" in blob:
        s.reasons.append(
            "propose_solidify still reports 'no REPL history' — REPL state may "
            "not be wired through the exec path either; investigate skill side"
        )
    else:
        s.reasons.append(
            "propose_solidify did not surface a `ready: true` — check transcript "
            "for the exact return value"
        )

    if t1.error or t2.error:
        s.passed = False
        s.reasons.append(f"turn errors: t1={t1.error} t2={t2.error}")

    # Always try to stop the repl daemon, regardless of pass/fail. The
    # agent was instructed to stop it on success, but on failure it might
    # still be alive and we don't want it stuck holding a ws to the
    # isolated Chrome.
    try:
        subprocess.run(
            [str(SKILL_BIN), "repl", "stop"],
            env=env_for_agent(), capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass

    return s


async def story_us4(client) -> StoryRun:
    """US4 — backend preference written to global memory.

    Two-turn: agent calls remember_preference(confirm=True), which raises
    NeedsUserConfirm — agent should surface a confirmation question. We
    answer yes. Agent re-calls with confirm=False; preference lands in
    global.md frontmatter.
    """
    s = StoryRun(name="US4")

    prompt1 = (
        "我想以后 browser-skill 默认用 'rdp' backend 连我的浏览器。"
        "请用 browser-skill 的 remember_preference() 把这个偏好记到 global memory。"
        "如果 Skill 提示需要确认，把它问我的内容贴给我看（不要自己替我决定）。"
    )
    t1 = await run_agent_turn(client, prompt1)
    s.turns.append(t1)

    prompt2 = (
        "确认：是的，把 daemon.preferred_backend = 'rdp' 写进 global memory。"
        "请用 confirm=False 重新调一次并报告写入结果。"
    )
    t2 = await run_agent_turn(client, prompt2)
    s.turns.append(t2)

    # Assertion: BS_HOME/global.md exists with a daemon block / preferred_backend.
    gpath = BS_HOME / "global.md"
    if gpath.exists():
        body = gpath.read_text(encoding="utf-8")
        if "rdp" in body and ("daemon" in body.lower() or "preferred_backend" in body):
            s.passed = True
            s.reasons.append(f"global.md has rdp preference; size={len(body)}")
        else:
            s.reasons.append(f"global.md exists but no rdp preference; body={body[:300]!r}")
    else:
        s.reasons.append("global.md was not created")
    if t1.error or t2.error:
        s.passed = False
        s.reasons.append(f"turn errors: t1={t1.error} t2={t2.error}")
    return s


# ---------------------------------------------------------------------------
# Dry-run mode — exercise the harness without spawning Claude
# ---------------------------------------------------------------------------


def dry_run_assertions() -> list[StoryRun]:
    """Validate that the setup half of the harness works (Chrome up, BS_HOME
    fresh, env wired) without ever calling the SDK. Useful when there's no
    API key available."""
    print("[dry-run] running setup + manual probe (no Claude agent)", file=sys.stderr)
    runs: list[StoryRun] = []

    # 0. Confirm Chrome is up.
    r = httpx.get(f"http://127.0.0.1:{ISOLATED_PORT}/json/version", timeout=5.0, **_HTTPX_KW)
    assert "webSocketDebuggerUrl" in r.json()

    # 1. Use browser-skill directly to read example.com via current_page.
    s1 = StoryRun(name="US1")
    s1.turns.append(AgentTurn(prompt="(dry-run: harness invokes browser-skill directly)"))
    proc = subprocess.run(
        [str(SKILL_BIN)],
        input=textwrap.dedent("""
            t = current_page()
            print(page_info())
        """).strip(),
        capture_output=True, text=True, env=env_for_agent(),
        timeout=60,
    )
    s1.turns[0].final_text = proc.stdout
    if "example.com" in proc.stdout.lower():
        s1.passed = True
        s1.reasons.append("current_page() returned example.com tab info")
    else:
        s1.reasons.append(f"unexpected output: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    runs.append(s1)

    # 2. Drive remember() ourselves.
    s2 = StoryRun(name="US2")
    proc = subprocess.run(
        [str(SKILL_BIN)],
        input=textwrap.dedent("""
            new_tab("https://news.ycombinator.com/")
            wait_for_load()
            titles = js('''Array.from(document.querySelectorAll(".titleline > a")).slice(0,5).map(a=>a.textContent)''')
            print("TITLES:", titles)
            remember("news.ycombinator.com",
                     "Top stories live in .titleline > a (5 per page, ranked).")
        """).strip(),
        capture_output=True, text=True, env=env_for_agent(),
        timeout=90,
    )
    s2.turns.append(AgentTurn(prompt="(dry-run)", final_text=proc.stdout))
    # host_stem("news.ycombinator.com") = "news"; glob to keep the test
    # resilient to stem-resolution surprises.
    mem_hits = list((BS_HOME / "site-skills").glob("*/memory.md"))
    matched = [p for p in mem_hits if "titleline" in p.read_text(encoding="utf-8")]
    if matched:
        s2.passed = True
        s2.reasons.append(f"remember() wrote to {matched[0]}")
    else:
        s2.reasons.append(
            f"no site-memory file under {BS_HOME / 'site-skills'} contains the "
            f"appended marker; existing files={mem_hits}; stderr={proc.stderr!r}"
        )
    runs.append(s2)

    # 3. propose_solidify() — drive it manually. Fall back to a synthetic
    # spec if the heuristic decides "not yet ready" (which is its default
    # for trivial probes — that's a separate concern from harness shape).
    s3 = StoryRun(name="US3")
    proc = subprocess.run(
        [str(SKILL_BIN)],
        input=textwrap.dedent("""
            import json
            new_tab("https://en.wikipedia.org/wiki/Python_(programming_language)")
            wait_for_load()
            first_p = js('document.querySelector("#mw-content-text p:not(.mw-empty-elt)").innerText')
            print("FIRST_P:", first_p[:120])
            spec = propose_solidify(name_hint="lookup")
            print("SPEC:", json.dumps(spec, default=str)[:500] if spec else None)
            if not spec:
                # Heuristic refused — drive solidify directly with a minimal
                # spec to verify the scaffolder writes a file.
                spec = {
                    "site": "wikipedia.org",
                    "suggested_name": "lookup_dryrun",
                    "draft_run_body": (
                        'def run(title: str = "Python (programming language)"):\\n'
                        '    new_tab(f"https://en.wikipedia.org/wiki/{title}")\\n'
                        '    wait_for_load()\\n'
                        '    return js(\\'document.querySelector("#mw-content-text p:not(.mw-empty-elt)").innerText\\')\\n'
                    ),
                    "draft_args_schema": {"title": {"type": "str", "default": "Python (programming language)"}},
                    "readiness_score": 0.5,
                }
            result = solidify(spec)
            print("SOLIDIFY RESULT:", result)
        """).strip(),
        capture_output=True, text=True, env=env_for_agent(),
        timeout=90,
    )
    s3.turns.append(AgentTurn(prompt="(dry-run)", final_text=proc.stdout))
    any_task = list((BS_HOME / "site-skills").glob("*/tasks/*.py"))
    if any_task:
        s3.passed = True
        s3.reasons.append(f"task scaffolded: {any_task[0]}")
    else:
        s3.reasons.append(
            f"no task file scaffolded; stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    runs.append(s3)

    # 4. remember_preference() — confirm flow.
    s4 = StoryRun(name="US4")
    proc = subprocess.run(
        [str(SKILL_BIN)],
        input=textwrap.dedent("""
            from browser_skill.errors import NeedsUserConfirm
            try:
                remember_preference("daemon.preferred_backend", "rdp", confirm=True)
            except NeedsUserConfirm as e:
                print("CONFIRM_NEEDED:", e)
                remember_preference("daemon.preferred_backend", "rdp", confirm=False)
                print("WRITTEN")
        """).strip(),
        capture_output=True, text=True, env=env_for_agent(),
        timeout=60,
    )
    s4.turns.append(AgentTurn(prompt="(dry-run)", final_text=proc.stdout))
    gpath = BS_HOME / "global.md"
    if gpath.exists() and "rdp" in gpath.read_text(encoding="utf-8"):
        s4.passed = True
        s4.reasons.append("global.md has rdp preference")
    else:
        s4.reasons.append(
            f"global.md missing/no rdp; stderr={proc.stderr!r}"
        )
    runs.append(s4)

    return runs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_transcripts(runs: list[StoryRun], label: str = "") -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f".{label}" if label else ""
    for run in runs:
        path = TRANSCRIPT_DIR / f"{run.name}{suffix}.json"
        path.write_text(
            json.dumps(
                {
                    "name": run.name,
                    "passed": run.passed,
                    "reasons": run.reasons,
                    "turns": [
                        {
                            "prompt": t.prompt,
                            "assistant_text": t.assistant_text,
                            "tool_calls": t.tool_calls,
                            "tool_results": t.tool_results,
                            "final_text": t.final_text,
                            "error": t.error,
                            "duration_s": t.duration_s,
                        }
                        for t in run.turns
                    ],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )


def write_report(runs: list[StoryRun], mode: str, label: str = "") -> None:
    report_path = (
        HERE / f"AI-E2E-REPORT.{label}.auto.md" if label else REPORT_PATH
    )
    lines = [
        "# AI E2E Report",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Mode: **{mode}**",
        "",
        "## Summary",
        "",
        "| US | Pass | Reasons |",
        "|----|------|---------|",
    ]
    for r in runs:
        status = "PASS" if r.passed else "FAIL"
        reasons = "; ".join(r.reasons).replace("|", "\\|")
        lines.append(f"| {r.name} | {status} | {reasons} |")
    lines.extend(["", "## Per-US transcript excerpts", ""])
    for r in runs:
        lines.extend([f"### {r.name} ({'PASS' if r.passed else 'FAIL'})", ""])
        for i, t in enumerate(r.turns):
            lines.extend([
                f"**Turn {i + 1}** (duration={t.duration_s}s, tool_calls={len(t.tool_calls)})",
                "",
                f"_Prompt:_ {t.prompt[:400]}",
                "",
                f"_Final response (truncated):_ {(t.final_text or '')[:1200]}",
                "",
            ])
            if t.tool_calls:
                lines.append("_Tool calls:_")
                for tc in t.tool_calls[:8]:
                    cmd = ""
                    if isinstance(tc.get("input"), dict):
                        cmd = tc["input"].get("command") or json.dumps(tc["input"])[:200]
                    lines.append(f"  - `{tc['name']}`: `{(cmd or '')[:200]}`")
                lines.append("")
            if t.error:
                lines.extend([f"_Error:_ `{t.error}`", ""])
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[done] wrote {report_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def real_run(only: set[str]) -> list[StoryRun]:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=["Bash", "Read"],
        permission_mode="bypassPermissions",  # accept all tool uses
        cwd=str(SKILL_DIR),
        env=env_for_agent(),
        max_turns=60,
    )

    stories: list[tuple[str, Callable]] = [
        ("US1", story_us1),
        ("US2", story_us2),
        ("US3", story_us3),
        # US3R: the long-lived-REPL variant. Exercises the path where
        # propose_solidify can actually see REPL history (which inline
        # heredoc cannot share across invocations). Sister case to US3.
        ("US3R", story_us3_repl),
        ("US4", story_us4),
        # US5: cloud backend (v0.5). Adds two more processes (fake cloud
        # server + a daemon serve), so it's heavier than the others.
        # Last to run so a failure here doesn't poison preceding tests.
        ("US5", story_us5_cloud),
        # US-Ext: extension backend (v0.5.3, F-4e). Adds a daemon serve +
        # fake chrome extension proxy. Like US5, fully self-contained.
        ("US-Ext", story_us_ext),
    ]
    runs: list[StoryRun] = []
    async with ClaudeSDKClient(options=options) as client:
        for name, fn in stories:
            if only and name not in only:
                continue
            print(f"\n========== {name} ==========", file=sys.stderr)
            try:
                r = await fn(client)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                r = StoryRun(name=name, passed=False, reasons=[f"exception: {e}"])
            print(f"[{name}] {'PASS' if r.passed else 'FAIL'} — {'; '.join(r.reasons)}",
                  file=sys.stderr)
            runs.append(r)
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip Claude agent; exercise harness setup + skill primitives directly.")
    ap.add_argument("--only", default="",
                    help="Comma-separated US names to run (e.g. 'US1,US3'). Empty = all.")
    ap.add_argument(
        "--label", default="",
        help=(
            "Optional suffix for output files — e.g. `--label rerun` writes "
            "transcripts to `US{n}.rerun.json` and the auto report to "
            "`AI-E2E-REPORT.rerun.auto.md`. Useful for comparing runs "
            "before / after framework changes without overwriting baselines."
        ),
    )
    ap.add_argument(
        "--allow-port-9222-listener", action="store_true",
        help=(
            "Override the pre-flight check that refuses to start when a "
            "Chrome is listening on :9222 (the autoconnect default port, "
            "i.e. the user's daily Chrome). Use only when you've verified "
            "the daemon's env routing (BD_CDP_URL → isolated port) prevents "
            "the harness from touching that Chrome. The downstream daemon-url "
            "assertion is NOT bypassable and still must pass."
        ),
    )
    args = ap.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()}

    # Sanity: required binaries.
    for name, p in [("browser-skill", SKILL_BIN), ("browser-daemon", DAEMON_BIN)]:
        if not p.exists():
            print(f"FATAL: {name} binary not found at {p}", file=sys.stderr)
            return 2

    # Pre-flight: API key OR Claude Code OAuth must exist (only if not dry-run).
    if not args.dry_run:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        has_oauth = Path("~/.claude/.credentials.json").expanduser().exists()
        if not has_key and not has_oauth:
            print(
                "FATAL: neither ANTHROPIC_API_KEY nor ~/.claude/.credentials.json found.\n"
                "Either export ANTHROPIC_API_KEY or `claude login` first.\n"
                "(You can still smoke-test the harness with --dry-run.)",
                file=sys.stderr,
            )
            return 2

    # Set up.
    #
    # Order matters here:
    #   1. assert_safe_environment FIRST — refuse if user's daily Chrome
    #      is on :9222 (before we spawn anything; nothing to clean up if
    #      we bail).
    #   2. reset_bs_home, then launch isolated Chrome, then seed page.
    #   3. assert_daemon_resolves_to_isolated AFTER Chrome is up — verifies
    #      the env is wired such that the daemon points browser-skill at
    #      OUR Chrome, not some other one. Catches gap #1 regressions.
    try:
        assert_safe_environment(allow_port_9222_listener=args.allow_port_9222_listener)
    except RuntimeError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    reset_bs_home()
    proc = None
    try:
        proc = launch_isolated_chrome()
        try:
            assert_daemon_resolves_to_isolated(env_for_agent())
        except RuntimeError as e:
            kill_isolated_chrome()
            print(f"FATAL: {e}", file=sys.stderr)
            return 2
        seed_us1_page()

        if args.dry_run:
            runs = dry_run_assertions()
            mode = "dry-run (no Claude agent)"
        else:
            runs = asyncio.run(real_run(only))
            mode = "live (Claude Agent SDK)"

        write_transcripts(runs, label=args.label)
        write_report(runs, mode, label=args.label)

        # Print summary to stderr.
        all_pass = all(r.passed for r in runs)
        print("\n=========== SUMMARY ===========", file=sys.stderr)
        for r in runs:
            print(f"  {r.name}: {'PASS' if r.passed else 'FAIL'}", file=sys.stderr)
        print(f"Overall: {'ALL PASS' if all_pass else 'SOME FAILED'}", file=sys.stderr)
        return 0 if all_pass else 1

    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print(f"FATAL: harness crashed — {e}", file=sys.stderr)
        return 3
    finally:
        # Best-effort teardown. Order matters: stop REPL daemon (which
        # holds a ws to Chrome) BEFORE killing Chrome, so the daemon
        # shuts down cleanly instead of erroring on a dropped ws. Either
        # may already be gone; suppress.
        with suppress(Exception):
            subprocess.run(
                [str(SKILL_BIN), "repl", "stop"],
                env=env_for_agent(), capture_output=True, text=True, timeout=5,
            )
        with suppress(Exception):
            kill_isolated_chrome()
        # Leave BS_HOME and TRANSCRIPT_DIR around for forensics; they're under
        # /tmp so the OS reaps them eventually.


if __name__ == "__main__":
    sys.exit(main())
