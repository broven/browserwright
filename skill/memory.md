# browser-skill memory

The agent reads this file on every browser-skill invocation. It carries two things:

1. **Backend capability table** — static reference for which Chrome the user can drive, and how.
2. **User preference** — mutable. The agent writes here when the user expresses a choice.

## Available backends

| Backend | Connects to | How to use |
|---|---|---|
| `rdp` | Isolated Chrome on a known port — zero popups, safe for iterative work | `browser-daemon launch-chrome --port 9333 --profile bs-dev --persistent` then prefix the call: `BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY' ... PY` |
| `autoconnect` | The user's running daily Chrome | `browser-skill repl start` — one "Allow remote debugging?" popup, every subsequent `exec` reuses the same ws |
| `extension` | The user's daily Chrome via unpacked relay extension (v0.4+) — no popups | One-time setup: `browser-daemon install` registers a macOS LaunchAgent so the daemon is always-on (default `BD_NAME=default`, port 19989). Load the bundled `chrome-extension/` once. After that the agent just calls `attach_active()` / `open_background(url)` — never `browser-daemon serve` |
| `cloud` | Hosted/remote Chrome (Browser Use, Browserless, Hyperbrowser) | `browser-daemon serve --backend cloud --provider <name>` + provider auth env vars |
| `env` | An externally-supplied CDP URL | Set `BROWSER_DAEMON_CDP_URL=ws://...` before calling |

## Popup safety (autoconnect only)

Chrome 144+ fires "Allow remote debugging?" on each fresh CDP ws. The framework already prevents accumulation — inline heredocs against `autoconnect` hard-abort with exit 2 by default; `browser-skill repl start` and `browser-daemon serve` open one ws and reuse it. Don't add safeguards on top.

## Daemon lifecycle (extension backend)

`browser-daemon install` registers a macOS LaunchAgent (`~/Library/LaunchAgents/com.browser-daemon.default.plist`) that:
- Starts the daemon at login (`RunAtLoad`).
- Restarts on crash (`KeepAlive` with `Crashed=true`).
- Default name `default`, default port `19989`.

After install, agents never call `browser-daemon serve` — the socket at `/tmp/browser-daemon-default.sock` is always live. `browser-daemon list` shows running instances; `browser-daemon uninstall` removes the LaunchAgent.

## User preference

The user runs different kinds of work in different browsers. Match the task to a `scenarios:` entry (top-down, first match wins). If nothing matches, fall back to `default_backend`.

```yaml
default_backend: extension

scenarios:
  - name: personal
    when: 用户个人任务、需要已登录账号或 cookie ("我的 X" / 私信 / 消息 / 个人 dashboard / 已登录的网盘 / 邮件)
    backend: extension
    launch_command: null  # daemon is a LaunchAgent — already running, don't spawn
    env: {}
    notes: |
      用户已 `browser-daemon install` (LaunchAgent, name=default, port 19989) +
      在日常 Chrome 装好了 chrome-extension/。Agent 不需要起 daemon。
      入口 primitive:
        - `attach_active()` → 用户当前 focused window 的 active tab (有黄条)
        - `open_background(url, group="Agent")` → 后台新 tab，黄条隐形
        - `close_tab(target_id=...)` → 显式关闭
      `list_tabs()` 在 0 ghost target 时会 raise NeedsUserConfirm 提示去 attach。
      不要再用 autoconnect — 用户明确偏好 extension，inline heredocs 也安全。

  - name: public
    when: 公共页面、无需 cookie 的一次性抓取、UI 测试、文档/示例站、批量 http_get
    backend: rdp
    launch_command: browser-daemon launch-chrome --port 9333 --profile bs-dev --persistent
    env:
      BD_PORT: 9333
      BD_BACKEND: rdp
    notes: |
      Zero popups, safe for iterative inline heredocs.

  - name: fingerprint
    when: 批量注册、反爬严重的站点、指纹浏览器场景 (AdsPower / MultiLogin / GoLogin / 比特浏览器)、需要独立账号身份的工作
    backend: rdp
    launch_command: 用户在指纹浏览器内启动目标账号并暴露 CDP 端口
    env:
      BD_PORT: <ask user — varies by fingerprint tool / profile>
      BD_BACKEND: rdp
    notes: |
      Ask the user which fingerprint browser + which port at the start of each session;
      never assume a default port. One isolated Chrome per account profile.
```

<!--
Each browser-skill invocation:
  1. Read the task description the user just gave.
  2. Match it to a `when:` field above (top-down, first match wins).
  3. Use that scenario's `backend`, `launch_command`, and `env`. Prepend the env vars
     to the `browser-skill` call. If the scenario needs user-specific values (e.g., a
     fingerprint browser port), ask the user before proceeding.
  4. If no scenario matches, use `default_backend` and consider asking the user
     whether this new type of work deserves its own scenario entry here.
-->


