# browserwright memory

The agent reads this file on every browserwright invocation. It carries two things:

1. **Backend capability table** — static reference for which Chrome the user can drive, and how.
2. **User preference** — mutable. The agent writes here when the user expresses a choice.

## Available backends

| Backend | Connects to | How to use |
|---|---|---|
| `cdp` | Isolated Chrome on a known port — zero popups, safe for iterative work | `sid=$(browserwright session new --backend=cdp --create --name=task-label)` then run `browserwright -s "$sid" -e '...'` |
| `extension` | The user's daily Chrome via unpacked relay extension — zero popups | `browserwright-daemon serve` (one global daemon) then load the bundled `chrome-extension/` directory |
| `env` | An externally-owned browser via a CDP URL you supply (e.g. an anti-detect / fingerprint profile) — attach-owned, never closed on `session end` | Start the daemon against it: `BD_CDP_WS=ws://... browserwright-daemon serve --backend env` (or `BD_CDP_URL=http://host:port` for `/json/version` discovery), then `sid=$(browserwright session new --backend=env --name=task-label)` and `browserwright -s "$sid" -e '...'` |

## User preference

The user runs different kinds of work in different browsers. Match the task to a `scenarios:` entry (top-down, first match wins). If nothing matches, fall back to `default_backend`.

```yaml
default_backend: extension

# `session new --name` is the Chrome tab group title the user may see.
# Prefer short task-specific labels over generic names like "personal".

scenarios:
  - name: personal
    when: 用户个人任务、需要已登录账号或 cookie ("我的 X" / 私信 / 消息 / 个人 dashboard / 已登录的网盘 / 邮件)
    backend: extension
    launch_command: browserwright-daemon serve
    env: {}
    notes: |
      User has the unpacked extension loaded into their daily Chrome
      (chrome-extension/, v0.4+). The daemon relays CDP through the extension —
      no popups, no banner. Inline `browserwright -s <id> -e ...` calls against
      extension are safe (no popup-accumulation hazard).
      已于 2026-05-19 在 doctor 里确认 extension ✓，直接用 `-s/-e` 调用即可。

  - name: public
    when: 公共页面、无需 cookie 的一次性抓取、UI 测试、文档/示例站、批量 http_get
    backend: cdp
    launch_command: browserwright session new --backend=cdp --create --name=<task-label>
    env: {}
    notes: |
      Zero popups, safe for iterative inline `-s/-e` calls.

  - name: fingerprint
    when: 批量注册、反爬严重的站点、指纹浏览器场景 (AdsPower / MultiLogin / GoLogin / 比特浏览器)、需要独立账号身份的工作
    backend: cdp
    launch_command: 用户在指纹浏览器内启动目标账号并暴露 CDP 端口
    env:
      session: browserwright session new --backend=cdp --attach=<port> --name=<profile>
    notes: |
      Ask the user which fingerprint browser + which port at the start of each session;
      never assume a default port. One isolated Chrome per account profile.
```

<!--
Each browserwright invocation:
  1. Read the task description the user just gave.
  2. Match it to a `when:` field above (top-down, first match wins).
  3. Use that scenario's `backend` and `launch_command`, keep the returned session id,
     and pass it with `browserwright -s <id> -e ...`. If the scenario needs user-specific values (e.g., a
     fingerprint browser port), ask the user before proceeding.
  4. If no scenario matches, use `default_backend` and consider asking the user
     whether this new type of work deserves its own scenario entry here.
-->
