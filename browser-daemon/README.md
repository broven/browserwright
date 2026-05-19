# browser-daemon

一个轻量的 CLI 工具，把 Chrome 的多种"远程调试入口"统一抽象成一个**本地 CDP WebSocket URL provider**。上层（REPL、固化脚本、MCP server 等）只需要问一句"给我一个能用的 ws URL"，不关心底层是 `--remote-debugging-port`、AutoConnect 还是浏览器插件 relay。

> **关于名字**：当前版本并没有长驻进程，本质是一个一次性的 resolver CLI（类似 `git config --get`）。保留 "daemon" 是预留给后续版本——届时会加入连接缓存、session 共享、health check 等真正需要长驻的能力。详见 [design.md](./design.md#naming)。

## Why

详见同目录的 [`../browser-connection.md`](../browser-connection.md)。简单说：

- `--remote-debugging-port=9222` 是经典做法，但 Chrome 新版本对默认 profile 越来越严格。
- Chrome AutoConnect / DevTools MCP 的新通道走的是 `DevToolsActivePort` 文件 + 用户授权弹窗。
- 浏览器插件 relay（如 Playwriter）走 `chrome.debugger` API，不触发 AutoConnect 弹窗，但端口/路径完全是工具自己定义的。

三种方式最终都收敛到一个 browser-level CDP WebSocket URL：

```
ws://127.0.0.1:<port>/devtools/browser/<browser-id>
```

`browser-daemon` 做的就是"无论你选哪种，告诉我 URL 长什么样"这件事，把发现/授权/fallback 逻辑封在一处。

## ⚠️ Chrome 144+ Allow-popup accumulation hazard

The `autoconnect` backend (DevToolsActivePort path) triggers Chrome's "Allow remote debugging?" popup **every** new WebSocket handshake (Chrome 144+ has zero memory between connections — verified empirically in `../browser-connection.md`). Worse: **Chrome itself has a bug where accumulating popups past some internal threshold can freeze the browser process**.

To protect users, the daemon does NOT trust developer discipline — it ships two layers of automatic defense:

1. **Rate-limit** — successful `autoconnect` resolves are throttled to one per 60 seconds, persisted via a timestamp file under `$XDG_RUNTIME_DIR` (cross-invocation). A second `browser-daemon url --backend autoconnect` within the window raises a clear `Unavailable` with both alternatives spelled out.
2. **Stderr warning** — `browser-daemon url --backend autoconnect` always prints a popup-hazard warning. Use `--quiet` only when you're sure you understand the trade-off (CI / known scripted flows).

**Two supported paths for repeated work** (use these, not autoconnect short-conn):

```bash
# Path A: Mode B long-running daemon — one popup per daemon lifetime, all
# client connections share the same upstream ws.
$ browser-daemon serve --backend autoconnect --name myrepl &
$ browser-daemon url --name myrepl --mode-b-proxy   # ↦ /tmp/browser-daemon-myrepl.sock

# Path B: Isolated Chrome on its own profile — zero popups, banner stays
# off-screen because the spawned Chrome runs detached.
$ browser-daemon launch-chrome --port 9333 --profile dev --persistent
ws://127.0.0.1:9333/devtools/browser/abc-...
```

Autoconnect Mode A short-conn (`browser-daemon url --backend autoconnect`) is **interactive use only** — one-off CLI invocations where the user is in front of the screen and can dismiss popups deliberately. Never script it in a loop.

**Expert escape hatch**: `BD_FORCE_AUTOCONNECT_RECONNECT=1` bypasses the rate-limit. Documented hazard: may freeze your Chrome.

## Quickstart

```bash
# 自动选择最佳可用 backend
$ browser-daemon url
ws://127.0.0.1:9222/devtools/browser/abc-123...

# 强制使用 remote-debug-port
$ browser-daemon url --backend rdp

# 列出所有 backend + 当前可达性
$ browser-daemon list-backends

# 诊断（每个 backend 为什么能/不能用）
$ browser-daemon doctor
```

典型 shell 用法：

```bash
export BD_CDP_WS="$(browser-daemon url)"
python my-script.py    # 脚本内直接读 BD_CDP_WS 连 CDP
```

Python 调用方（暂不导出 SDK，统一走 CLI subprocess）：

```python
import subprocess
ws = subprocess.check_output(["browser-daemon", "url"], text=True).strip()
```

## CLI 总览

| 命令 | 作用 |
|---|---|
| `browser-daemon url [--backend NAME] [--timeout SEC]` | 解析并输出 ws URL 到 stdout |
| `browser-daemon list-backends` | 列出已注册 backend 及当前可达性 |
| `browser-daemon doctor` | 详细诊断每个 backend 状态（端口、文件路径、HTTP 响应等） |
| `browser-daemon version` | 输出版本号 |

### Exit codes

| 码 | 含义 |
|---|---|
| 0 | 成功，stdout 有 ws URL |
| 1 | 用户错误（参数非法、未知 backend 名等） |
| 2 | 所有 backend 都不可用（Chrome 没开远程调试 / extension relay 没运行 / 等等） |
| 3 | 内部错误（崩溃、未预期异常） |

## 内置 Backend（MVP）

| name | 说明 | 优先级（默认 fallback chain 中） |
|---|---|---|
| `env` | 直接读环境变量 `BD_CDP_WS`（完整 ws URL）或 `BD_CDP_URL`（`http://host:port`，再走 `/json/version` 解析） | 1（最高） |
| `rdp` | 假设 Chrome 启动时带了 `--remote-debugging-port=9222`，HTTP 探测 `/json/version` | 2 |
| `autoconnect` | 扫描 Chrome user-data-dir 找 `DevToolsActivePort` 文件，再拼 ws URL | 3 |
| `extension` | 用户安装的 Chrome 扩展走 `chrome.debugger` API；daemon 在 `127.0.0.1:19989` 起 relay ws server，扩展连过来后 daemon 把标准 CDP 流量翻译成 `chrome.debugger.sendCommand` 调用。**v0.4 起真实装**。 | 默认不在链中，需 `--backend extension` 显式选 |
| `cloud` | 远程托管浏览器（Browser Use / Browserless / Hyperbrowser），daemon Mode B 自连 upstream ws 时按 AuthProvider 注入 `Authorization: Bearer ...` / mTLS client cert（v0.1 `env` backend 只能 URL-embedded token，所以专门拆这条）。**v0.5 起真实装**。 | 默认不在链中，需 `--backend cloud` 显式选 |

## v0.4 extension backend

`extension` backend 是一个 **LOCAL_RELAY**：daemon 不去连一个已有的 CDP 端口，而是 daemon 自己起一个 ws server，让用户日常 Chrome 装上配套扩展后连过来，daemon 把上层 Skill 发来的标准 CDP 命令翻译成 `chrome.debugger.sendCommand` 调用通过扩展打到 Chrome。

为什么要这条路径：用户日常使用的 Chrome（带 1Password、Bitwarden、所有书签、所有 cookie）**不能**重启加 `--remote-debugging-port`，否则丢 session、丢已登录状态、丢扩展状态。扩展模型让 daemon 既能用上用户日常 Chrome，又不要求用户改启动方式。

### 一次性安装（macOS）

1. **注册 daemon 为 LaunchAgent**：`browser-daemon install`

   写入 `~/Library/LaunchAgents/com.browser-daemon.default.plist` 并 `launchctl load`。daemon 会：
   - 每次登录自动启动（`RunAtLoad`）
   - 崩了 launchd 自动重启（`KeepAlive`，`Crashed=true`）
   - 本地 unix socket 永远在 `/tmp/browser-daemon-default.sock`
   - relay ws server 永远在 `ws://127.0.0.1:19989`（扩展通过此连）

   想换 name / 端口：`browser-daemon install --name X --extension-port N`。  
   想卸：`browser-daemon uninstall --name X`。  
   想查：`browser-daemon list`。

2. 把 `browser-daemon/chrome-extension/` 整个目录作为 **unpacked extension** 装到 Chrome：
   - 打开 `chrome://extensions/`
   - 右上角打开"开发者模式"
   - 点"加载已解压的扩展程序"，选 `chrome-extension/` 目录

3. 装好后，扩展会自动连接 daemon —— 不需要点扩展图标，也不需要手动 attach 任何 tab。后续 daemon 重启 / Chrome 重启 / extension service worker idle 都由 `maintainLoop` + `chrome.alarms` + `chrome.runtime.onStartup` 自动恢复，**零手动操作**。

### 使用 / Agent-driven attach

扩展默认**不**自动 attach 任何 tab —— Chrome 的"debugger 黄条"会出现在被 attach 的 tab 上，所以"装上扩展就自动 attach 所有 tab"会让每个 tab 都长出黄条。

正确用法：**Agent / Skill 在需要操作 tab 时主动 attach**。三个入口：

- `attach_active()` — 抓 Chrome focused window 的 active tab（黄条出现，因为这正是你想看到 Agent 操作的 tab）
- `open_background(url, group="Agent")` — 后台开新 tab + 加进名为 "Agent" 的 tab group，`active:false` 不抢焦点。黄条出现在那个 tab 上但你看不见
- `close_tab(target_id=...)` — Agent 操作完后显式关闭

对应 CLI：`browser-daemon attach-active` / `open-background --url X --group Agent` / `close-tab --target-id ext-tab-N`。

用户还可以走 popup 手动 attach（点扩展图标），跟 Agent 路径并存。

### doctor / health check

`browser-daemon doctor --backend extension --json` 会返回三种状态之一：

| `available` | `detail` | 含义 |
|---|---|---|
| `false` | "no extension relay listening on 127.0.0.1:19989…" | daemon 没启动 — 跑 `browser-daemon install` 一次性注册成 LaunchAgent，或临时 `browser-daemon serve --backend extension` |
| `false` | "extension relay is running but no Chrome extension has connected yet" | daemon 起来了，但 Chrome 扩展还没装/还没启动 |
| `true` | `"<N> extension(s) connected (install_ids=[…], attached tabs=N)"` | 健康 |

### 限制（已知不支持）

`Browser.crash` / `Browser.close` 等浏览器级别命令在 extension backend 下返回 `-32601 "method not implemented in extension backend"`——`chrome.debugger` API 没有对应的 hook。Page-level / Target-level 命令全部支持。

## v0.5 cloud backend

云端托管浏览器（Browser Use / Browserless / Hyperbrowser 等）的接入路径。**v0.1 `env` backend** 已经能覆盖 URL-embedded auth（`?api_key=...` / `wss://user:pass@host/`）；v0.5 `cloud` backend 专门加 **HTTP header auth 和 mTLS** 这两条 env backend 实现不了的。

### 配置示例

```toml
[backends.cloud]
endpoint = "wss://api.browser-use.com/cdp/session"
auth_kind = "bearer"      # "bearer" | "basic" | "mtls" | "oauth2"
provider_hint = "browser-use"

[backends.cloud.auth.bearer]
token_env = "BROWSER_USE_API_KEY"   # 推荐：把 API key 放 env 而不是 toml
header_name = "Authorization"        # 可选，默认就是 "Authorization"
header_prefix = "Bearer "            # 可选，默认就是 "Bearer "

# 或者 mTLS：
# [backends.cloud.auth.mtls]
# cert_file = "~/.config/browser-daemon/client.crt"
# key_file  = "~/.config/browser-daemon/client.key"
# ca_file   = "~/.config/browser-daemon/ca.pem"   # 可选自定义 CA
# key_password_env = "MY_KEY_PASSWORD"            # 可选加密 key 的密码

# 或者 basic：
# [backends.cloud.auth.basic]
# username_env = "PROVIDER_USERNAME"
# password_env = "PROVIDER_PASSWORD"
# embed_in_url = false   # true → 用 user:pass@host 替代 header（旧云厂商兼容）
```

### Env 覆盖

| 变量 | 含义 |
|---|---|
| `BD_CLOUD_ENDPOINT` | 等同 `[backends.cloud].endpoint` |
| `BD_CLOUD_AUTH_KIND` | 等同 `[backends.cloud].auth_kind` |
| `BD_CLOUD_PROVIDER_HINT` | 等同 `[backends.cloud].provider_hint` |

具体 auth payload 仍走 provider 自己的 env 变量（`token_env` / `username_env` 等），不再多加一层 `BD_CLOUD_TOKEN`——会跟 AuthProvider 的 explicit resolution 路径冲突。

### Mode B vs Mode A 取舍

- **Mode B（`browser-daemon serve --backend cloud`）**：daemon 自己连上游 ws，AuthProvider 在握手时注入 header / SSLContext。**Skill 端无需感知 auth**——只连 daemon socket，daemon 帮它完成认证
- **Mode A（`browser-daemon url --backend cloud`）**：daemon 把 ws URL 透传给 Skill，Skill 自己开 ws。**只对 URL-embedded auth 形态有意义**（basic / URL-token）。header / mTLS 在 Mode A 下不能用——Skill 没有 header 注入点

### `OAuth2Auth` 状态

`auth_kind = "oauth2"` 是 **v0.6 占位**。调用任意方法 raise `UserError("...placeholder...")`。v0.5 用户用 BearerTokenAuth 手动管 access token。

### doctor 三态

| `available` | `detail` 形如 | 含义 |
|---|---|---|
| `false` | "no cloud endpoint configured ..." | toml 没填 `[backends.cloud].endpoint` |
| `false` | "401 — auth rejected by upstream" | 配置好了但 token 无效 |
| `true` | "provider=browser-use, auth=bearer, ... OK" | 健康 |

## v0.5 observability

`browser-daemon stats` 子命令查活 daemon 的进程内计数器：

```bash
# 默认 tab-separated key\tvalue
$ browser-daemon stats --name myrepl
client_connected_total       3
proxy_pre_open_buffered_total 0
upstream_open_succeeded_total 1
upstream_frame_received_total 247
uptime_seconds                127.451
...

# JSON
$ browser-daemon stats --name myrepl --json | jq '.proxy_pre_open_overflow_total'
0
```

计数器分四组（`client_*` / `upstream_*` / `proxy_*` / `auth_*`）+ `uptime_seconds`。新增 / 重命名 counter 是 stats schema 的次版本 bump。

### JSON 日志

设 `BD_LOG_JSON=1` 启动 daemon-serve，每条 log 变成一行 JSON：

```json
{"ts":"2026-05-18T13:42:00Z","level":"INFO","logger":"browser_daemon.server.listener","msg":"client 3 connected (label=skill-repl, total=1)"}
```

字段：`ts`（ISO-8601 UTC）、`level`、`logger`、`msg`，可选 `extra`（来自 `logger.info(..., extra={...})`）、可选 `exc_info`。

无 `--backend` 时按 `env → rdp → autoconnect` 顺序尝试，第一个返回 URL 就用它。

## 配置（可选）

`~/.config/browser-daemon/config.toml`：

```toml
# 覆盖默认 fallback chain 的首选 backend（与 `BD_BACKEND` 等价；
# CLI `--backend` 仍最高优先级）
default_backend = "autoconnect"

[backends.rdp]
port = 9222

[backends.autoconnect]
# 自定义 Chrome user-data-dir。这些路径 **prepend** 到平台默认列表前，
# 用户可以加非默认 profile dir 而不丢平台 default 覆盖
profile_paths = ["~/Library/Application Support/Google/Chrome"]

[backends.extension]
# 覆盖 daemon 内 extension relay ws server 的绑定地址（默认 ws://127.0.0.1:19989）
# 默认 19989 是为了跟 playwriter (19988) 共存；如需进一步避冲突再调整
relay_url = "ws://127.0.0.1:19989"

[backends.cloud]
# 云端浏览器配置详见上文 §v0.5 cloud backend
```

> **fallback_chain 已撤掉**：v0.1 README 曾文档化 `fallback_chain = [...]`
> 但 parser 从来没读这个 key（REVIEW.md F-5 / Task #15）。v0.5+ 推荐用
> `_CHAIN_OPT_OUT` 机制：`extension` 和 `cloud` 默认不在 auto chain，
> 必须 `--backend` / `BD_BACKEND` / `default_backend` 显式选。已覆盖 ~80%
> 自定义顺序需求。

MVP 阶段 config 文件不是必须的——所有项都有合理默认值，env var 可以覆盖。

## 环境变量

| 变量 | 含义 |
|---|---|
| `BD_CDP_WS` | 直接指定 ws URL，`env` backend 读取这个 |
| `BD_CDP_URL` | 指定 HTTP discovery URL（如 `http://127.0.0.1:9222`），`env` backend 通过 `/json/version` 取 ws URL |
| `BD_BACKEND` | 等同于 `--backend`，命令行参数优先 |
| `BD_RDP_PORT` | `rdp` backend 的端口（v0.4.1 起）。优先级：CLI `--port` > `BD_RDP_PORT` > toml > 9222 默认。**配合 `BD_BACKEND=rdp` 锁定到隔离 Chrome 时务必同时设这个**——否则 daemon 用 9222 默认值撞上用户日常 Chrome，Allow 弹窗连发 |
| `BD_TIMEOUT` | 单 backend resolve 超时秒数 |
| `BD_NAME` | daemon 实例名（多实例区分用），影响 socket / pid 文件路径 |
| `BD_CHROME_BINARY` | 指定 Chrome 可执行文件路径（`launch-chrome` 用） |
| `BD_IDLE_CLOSE_AFTER` | Mode B serve idle 关 upstream 的秒数；不设/≤0 = 永不 |
| `BD_CONFIG` | 覆盖默认 config 文件路径 |
| `BD_FORCE_AUTOCONNECT_RECONNECT` | 绕过 `autoconnect` 60s rate-limit（仅当你完全理解 Chrome 144+ 弹窗累积 hazard 时）|
| `BD_PORT` | `BD_RDP_PORT` 的 deprecated alias（v0.5.3 起）。**v0.4 popup-storm 根因防御**：之前用户把 `BD_PORT=9444` 当作 rdp port 设，daemon silently 默认 9222 撞用户 Chrome。现在 `BD_PORT` 没设 `BD_RDP_PORT` 时按 alias 生效 + stderr 打 deprecation warning |
| `BD_EXTENSION_PORT` | extension backend relay ws server 的绑定端口（v0.5.3 起）。优先级：CLI `--extension-port` > `BD_EXTENSION_PORT` > toml `[backends.extension].port` > 默认 19989。默认就避开 playwriter 的 19988，但需要进一步避冲突（多 daemon 实例等）时用这个 |
| `BD_CLOUD_ENDPOINT` / `BD_CLOUD_AUTH_KIND` / `BD_CLOUD_PROVIDER_HINT` | cloud backend 配置 env shortcut（v0.5 起），等价 `[backends.cloud].*` toml key |
| `BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE` | EXPERT ESCAPE：绕过 launch-chrome 拒绝用户 default profile 的 guard。truthy 值 `1`/`true`/`yes`/`on`/`y`（case-insensitive）unlock。**仅当你完全理解会永久暴露日常 Chrome 给 CDP popup hazard 时** |
| `BD_LOG_JSON` | `1` / `true` / `yes` → daemon log 输出 JSON 行（`{ts, level, logger, msg, extra?, exc_info?}`），方便日志聚合器消费。默认 plaintext |

## 范围（MVP 不做的事）

- ❌ **不**内嵌任何 CDP 客户端逻辑。`browser-daemon` 只输出 URL，连接和 send/recv 由上层负责（cdp-use / playwright / 自己实现都行）。
- ❌ **不**启动 Chrome。Chrome 由用户自己运行。
- ❌ **不**做长驻进程、连接缓存、health monitor。这些是 v0.x 之后的事。
- ❌ **不**做截图、点击、DOM 读取。这些属于 Layer 2 的 skill。

## 状态

| 阶段 | 状态 |
|---|---|
| 设计文档 | v2 完成，见 [design-v2.md](./design-v2.md) |
| v0.1 Mode A MVP | ✅ |
| v0.2 Mode B 单 client serve | ✅ |
| v0.3 Mode B 多 client mux + sessionId 翻译表 + 单 attacher | ✅ |
| v0.4 extension backend (LOCAL_RELAY) + Chrome MV3 扩展 | ✅ |
| v0.5 `cloud` backend (Bearer / Basic / mTLS / OAuth2 stub) + observability (`stats` CLI + JSON logs) | ✅ |
| v0.6+ `OAuth2Auth` 真实装 / daemon-as-a-service / metrics push | ⏳ 未排期 |

详细设计见 [design-v2.md](./design-v2.md)。
