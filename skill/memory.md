# browserwright memory

The agent reads this file on every browserwright invocation. It carries two things:

1. **Backend capability table** — static reference for which Chrome the user can drive, and how.
2. **User preference** — mutable. The agent writes here when the user expresses a choice.

## Available backends

| Backend | Connects to | How to use |
|---|---|---|
| `rdp` | Isolated Chrome on a known port — zero popups, safe for iterative work | `browserwright-daemon launch-chrome --port 9333 --profile bs-dev --persistent` then prefix the call: `BD_PORT=9333 BD_BACKEND=rdp browserwright <<'PY' ... PY` |
| `extension` | The user's daily Chrome via unpacked relay extension — zero popups | `browserwright-daemon serve --backend extension` then load the bundled `chrome-extension/` directory |
| `cloud` | Hosted/remote Chrome (Browser Use, Browserless, Hyperbrowser) | `browserwright-daemon serve --backend cloud --provider <name>` + provider auth env vars |
| `env` | An externally-supplied CDP URL | Set `BROWSER_DAEMON_CDP_URL=ws://...` before calling |

## User preference

The user runs different kinds of work in different browsers. Match the task to a `scenarios:` entry (top-down, first match wins). If nothing matches, fall back to `default_backend`.

```yaml
default_backend: extension

scenarios:
  - name: personal
    when: 用户个人任务、需要已登录账号或 cookie ("我的 X" / 私信 / 消息 / 个人 dashboard / 已登录的网盘 / 邮件)
    backend: extension
    launch_command: browserwright-daemon serve --backend extension
    env: {}
    notes: |
      User has the unpacked extension loaded into their daily Chrome
      (chrome-extension/, v0.4+). The daemon relays CDP through the extension —
      no popups, no banner. Inline heredocs against extension are safe (no
      popup-accumulation hazard).
      已于 2026-05-19 在 doctor 里确认 extension ✓，直接 inline heredoc 即可。

  - name: public
    when: 公共页面、无需 cookie 的一次性抓取、UI 测试、文档/示例站、批量 http_get
    backend: rdp
    launch_command: browserwright-daemon launch-chrome --port 9333 --profile bs-dev --persistent
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
Each browserwright invocation:
  1. Read the task description the user just gave.
  2. Match it to a `when:` field above (top-down, first match wins).
  3. Use that scenario's `backend`, `launch_command`, and `env`. Prepend the env vars
     to the `browserwright` call. If the scenario needs user-specific values (e.g., a
     fingerprint browser port), ask the user before proceeding.
  4. If no scenario matches, use `default_backend` and consider asking the user
     whether this new type of work deserves its own scenario entry here.
-->
