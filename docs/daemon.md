# browserwright-daemon

**一个长驻的全局 daemon**，把 Chrome 的多种"远程调试入口"统一抽象成一个本地 CDP 代理。它监听固定的 unix socket（`${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock`），同时服务多个 session：extension session 共享一条 relay upstream（用户日常 Chrome），cdp session 各自拿到 daemon 启动并持有的隔离 Chrome。上层（`browserwright` skill CLI、固化脚本等）只连 daemon socket，不关心底层是 `--remote-debugging-port` 还是浏览器插件 relay。

daemon 只有长驻的 `serve` 模式：所有调用方（skill、固化脚本）都连 daemon socket，daemon 代理 CDP 流量。（旧的一次性 resolver 用法 `browserwright-daemon url`（Mode A）已移除；需要外部脚本直连时用 Playwright facade `ws://127.0.0.1:19990/cdp`。）

## Why

背景详见 [`archive/browser-connection.md`](./archive/browser-connection.md)。简单说：

- `--remote-debugging-port=9222` 是经典做法，但 Chrome 新版本对默认 profile 越来越严格。
- Chrome AutoConnect / DevTools MCP 的新通道走的是 `DevToolsActivePort` 文件 + 用户授权弹窗。
- 浏览器插件 relay（如 Playwriter）走 `chrome.debugger` API，不触发 AutoConnect 弹窗，但端口/路径完全是工具自己定义的。

三种方式最终都收敛到一个 browser-level CDP WebSocket URL：

```
ws://127.0.0.1:<port>/devtools/browser/<browser-id>
```

`browserwright-daemon` 做的就是"无论你选哪种，告诉我 URL 长什么样"这件事，把发现/授权/fallback 逻辑封在一处。

## Driving the user's daily Chrome

To drive the user's daily Chrome, use the **extension** backend (it relays CDP through the unpacked extension's `chrome.debugger` API — no remote-debugging port, no Allow popups):

```bash
# 1. Load the unpacked extension once (browserwright-daemon ships it under chrome-extension/).
$ browserwright-daemon extension-path --json    # prints the absolute path
# In Chrome: chrome://extensions → toggle Developer mode → Load unpacked → pick that path.

# 2. Start the relay (typically as a LaunchAgent / systemd unit).
#    One global daemon, fixed socket — `serve` needs no --backend (it serves
#    the shared extension upstream plus per-session cdp) and no --name.
$ browserwright-daemon serve
```

Zero popups, zero banner. For scripted work without touching the user's browser, prefer an isolated Chrome instead:

```bash
$ browserwright-daemon launch-chrome --port 9333 --profile dev --persistent
ws://127.0.0.1:9333/devtools/browser/abc-...
```

## Quickstart

```bash
# 启动单全局 daemon（通常注册成 LaunchAgent，见下文）
$ browserwright-daemon serve

# daemon 活着吗？endpoint 在哪？
$ browserwright-daemon status --json

# 诊断（每个 backend 为什么能/不能用）
$ browserwright-daemon doctor
```

## CLI 总览

| 命令 | 作用 |
|---|---|
| `browserwright-daemon serve` | 运行单全局 daemon（shared extension relay + per-session cdp） |
| `browserwright-daemon status [--json]` | 报告 daemon 存活状态、socket endpoint、facade 端口 |
| `browserwright-daemon stop` / `restart` | 停止 / 重启 daemon（已注册 LaunchAgent 时 launchd 会自动拉起；restart 走 LaunchAgent） |
| `browserwright-daemon doctor` | 详细诊断每个 backend 状态（端口、文件路径、HTTP 响应等） |
| `browserwright-daemon logs [-f]` | 打印 log 文件路径或 tail 之 |
| `browserwright-daemon launch-chrome` | 启动隔离 profile 的 Chrome 并输出其 ws URL |
| `browserwright-daemon version` | 输出版本号（`version check` 校验版本一致性） |

### Exit codes

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 用户错误（参数非法、未知 backend 名等） |
| 2 | 所有 backend 都不可用（Chrome 没开远程调试 / extension relay 没运行 / 等等） |
| 3 | 内部错误（崩溃、未预期异常） |

## 内置 Backend

| name | 说明 | 选择方式 |
|---|---|---|
| `cdp` | 真实 browser-level CDP。`--create` 时 daemon 自己启动并持有隔离 Chrome；`--attach=<port\|url>` 时连接别人持有的浏览器——本机端口，或 `ws(s)://` / `http(s)://` 端点（anti-detect / 指纹 / 云浏览器）。`ws` 原样使用不做任何改写（token 常嵌在 URL 里），`http` 走 `/json/version` 解析 | 按 session ledger 分流（`browserwright session new --backend=cdp`）。端点是**每会话**的，所以一个 daemon 可同时驱动多个外部浏览器 |
| `extension` | 用户安装的 Chrome 扩展走 `chrome.debugger` API；daemon 在 `127.0.0.1:19989` 起 relay ws server，扩展连过来后 daemon 把标准 CDP 流量翻译成 `chrome.debugger.sendCommand` 调用。**驱动用户日常 Chrome 的唯一路径**。 | `browserwright-daemon serve` 默认启动这个 shared relay；具体 session 仍用 `browserwright session new --backend=extension` 选择 |

## v0.4 extension backend

`extension` backend 是一个 **LOCAL_RELAY**：daemon 不去连一个已有的 CDP 端口，而是 daemon 自己起一个 ws server，让用户日常 Chrome 装上配套扩展后连过来，daemon 把上层 Skill 发来的标准 CDP 命令翻译成 `chrome.debugger.sendCommand` 调用通过扩展打到 Chrome。

为什么要这条路径：用户日常使用的 Chrome（带 1Password、Bitwarden、所有书签、所有 cookie）**不能**重启加 `--remote-debugging-port`，否则丢 session、丢已登录状态、丢扩展状态。扩展模型让 daemon 既能用上用户日常 Chrome，又不要求用户改启动方式。

### 一次性安装（macOS）

1. **注册 daemon 为 LaunchAgent**：`browserwright-daemon install`

   写入 `~/Library/LaunchAgents/com.browserwright-daemon.plist` 并 `launchctl load`。daemon 会：
   - 每次登录自动启动（`RunAtLoad`）
   - **任何退出都会被 launchd 自动重启**（`KeepAlive`，`SuccessfulExit=true` + `Crashed=true`）——正常退出（包括 `stop`、控制 socket watchdog 自退）和崩溃都算；这样优雅退出 0 不会被 launchd 当成“任务完成、永不复活”（issue #39）。防 crash-loop 在 `serve` 层（启动时回收 stale 端口，issue #15 2.2），launchd 侧只负责复活
   - 本地 unix socket 永远在 `${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock`（全局唯一、无 name 后缀；第二个 daemon 起不来——stale-detect 拒绝）
   - relay ws server 永远在 `ws://127.0.0.1:19989`（扩展通过此连）

   注意：被 LaunchAgent 监管的 daemon，`stop` 只是临时停止——launchd 会在 ~10 秒后复活它。要**永久**停掉：`browserwright-daemon uninstall`（或 `launchctl unload ~/Library/LaunchAgents/com.browserwright-daemon.plist`）。前台 `serve`（未注册 LaunchAgent）不受影响，`stop` 照常永久停止。

   想换端口：`browserwright-daemon install --extension-port N`。  
   想卸：`browserwright-daemon uninstall`。  
   想查：`browserwright-daemon list`。

2. 把 `browserwright-daemon/chrome-extension/` 整个目录作为 **unpacked extension** 装到 Chrome：
   - 打开 `chrome://extensions/`
   - 右上角打开"开发者模式"
   - 点"加载已解压的扩展程序"，选 `chrome-extension/` 目录

3. 装好后，扩展会自动连接 daemon —— 不需要点扩展图标，也不需要手动 attach 任何 tab。后续 daemon 重启 / Chrome 重启 / extension service worker idle 都由 `maintainLoop` + `chrome.alarms` + `chrome.runtime.onStartup` 自动恢复，**零手动操作**。

升级已加载的 unpacked extension 时，先更新磁盘上的扩展目录并重启 daemon，然后运行：

```bash
browserwright-daemon extension reload
```

daemon 会让已连接的扩展调用 `chrome.runtime.reload()`，从磁盘重新加载代码并自动重连。`mise run upgrade-global` 已经包含这一步；只有首次安装或 reload 未确认时才需要回到 `chrome://extensions/` 手动加载/刷新。

### 使用 / Agent-driven attach

扩展默认**不**自动 attach 任何 tab —— Chrome 的"debugger 黄条"会出现在被 attach 的 tab 上，所以"装上扩展就自动 attach 所有 tab"会让每个 tab 都长出黄条。

正确用法：**Agent / Skill 在需要操作 tab 时主动 attach**。三个入口：

- `attach_active()` — 把 Chrome focused window 的 active tab **adopt 进本 session 的 tab group**（黄条出现，因为这正是你想看到 Agent 操作的 tab）；该 tab 已属于另一个 session 的 group 时拒绝、不抢
- `open(url, background=True)` — 统一开页动词，开新 tab 进本 session 的 group（`background=True` 在 extension 下 `active:false` 不抢焦点，cdp 下无人争焦点故为 no-op）。黄条出现在那个 tab 上但你看不见。`open_background`/`new_tab` 仍作为 deprecated 别名保留
- `close_tab(target_id=...)` — Agent 操作完后显式关闭

对应 CLI：`browserwright-daemon attach-active` / `open-background --url X` / `close-tab --target-id ext-tab-N`。

用户还可以走 popup 手动 attach（点扩展图标），跟 Agent 路径并存。

### doctor / health check

`browserwright-daemon doctor --backend extension --json` 会返回三种状态之一：

| `available` | `detail` | 含义 |
|---|---|---|
| `false` | "no extension relay listening on 127.0.0.1:19989…" | daemon 没启动 — 跑 `browserwright-daemon install` 一次性注册成 LaunchAgent，或临时 `browserwright-daemon serve` |
| `false` | "extension relay is running but no Chrome extension has connected yet" | daemon 起来了，但 Chrome 扩展还没装/还没启动 |
| `true` | `"<N> extension(s) connected (install_ids=[…], attached tabs=N)"` | 健康 |

### 限制（已知不支持）

`Browser.crash` / `Browser.close` 等浏览器级别命令在 extension backend 下返回 `-32601 "method not implemented in extension backend"`——`chrome.debugger` API 没有对应的 hook。Page-level / Target-level 命令全部支持。

## Observability

### JSON 日志

设 `BD_LOG_JSON=1` 启动 daemon-serve，每条 log 变成一行 JSON：

```json
{"ts":"2026-05-18T13:42:00Z","level":"INFO","logger":"browserwright.daemon.server.listener","msg":"client 3 connected (label=skill-repl, total=1)"}
```

字段：`ts`（ISO-8601 UTC）、`level`、`logger`、`msg`，可选 `extra`（来自 `logger.info(..., extra={...})`）、可选 `exc_info`。

`browserwright-daemon serve` 是单全局 daemon：无 `--backend` 时启动默认 shared `extension` relay，cdp sessions 根据 session ledger 懒创建自己的 upstream context。`--backend` 只用于覆盖 shared upstream（例如 env 调试），不是安装或启动一个“只服务某 backend”的 daemon。

## 配置（可选）

`~/.config/browserwright-daemon/config.toml`：

```toml
# 覆盖 daemon shared upstream（与 `BD_BACKEND` 等价；
# CLI `--backend` 仍最高优先级）。不影响 cdp session 按 ledger 分流。
default_backend = "extension"

[backends.cdp]
port = 9222

[backends.extension]
# 覆盖 daemon 内 extension relay ws server 的绑定地址（默认 ws://127.0.0.1:19989）
# 默认 19989 是为了跟 playwriter (19988) 共存；如需进一步避冲突再调整
relay_url = "ws://127.0.0.1:19989"

# Playwright facade 的绑定 host/port（默认 127.0.0.1:19990，loopback，绝不会误暴露）。
# 想让别的机器（例如经 Tailscale）`connect_over_cdp` 进来时，把 facade_host 设成
# 对应网卡 IP 或 0.0.0.0。优先级：CLI `--facade-host` > `BD_FACADE_HOST` > 此 toml key。
facade_host = "127.0.0.1"
facade_port = 19990
```

> **fallback_chain 已撤掉**：v0.1 README 曾文档化 `fallback_chain = [...]`
> 但 parser 从来没读这个 key（REVIEW.md F-5 / Task #15）。当前 daemon
> 不再靠 fallback chain 表达日常使用路径；session backend 写在 session
> ledger，shared upstream 才读取 `--backend` / `BD_BACKEND` / `default_backend`。

MVP 阶段 config 文件不是必须的——所有项都有合理默认值，env var 可以覆盖。

## 环境变量

| 变量 | 含义 |
|---|---|
| `BD_CDP_WS` | 直接指定 ws URL，`env` backend 读取这个 |
| `BD_CDP_URL` | 指定 HTTP discovery URL（如 `http://127.0.0.1:9222`），`env` backend 通过 `/json/version` 取 ws URL |
| `BD_BACKEND` | 等同于 `--backend`，命令行参数优先 |
| `BD_CDP_PORT` | `cdp` backend 的端口（v0.4.1 起）。优先级：CLI `--port` > `BD_CDP_PORT` > toml > 9222 默认。**配合 `BD_BACKEND=cdp` 锁定到隔离 Chrome 时务必同时设这个**——否则 daemon 用 9222 默认值撞上用户日常 Chrome，Allow 弹窗连发 |
| `BD_TIMEOUT` | 单 backend resolve 超时秒数 |
| `BD_CHROME_BINARY` | 指定 Chrome 可执行文件路径（`launch-chrome` 用） |
| `BD_IDLE_CLOSE_AFTER` | Mode B serve idle 关 upstream 的秒数；不设/≤0 = 永不 |
| `BD_CONFIG` | 覆盖默认 config 文件路径 |
| `BD_PORT` | `BD_CDP_PORT` 的 deprecated alias。之前用户把 `BD_PORT=9444` 当作 cdp port 设，daemon silently 默认 9222 撞用户 Chrome。现在 `BD_PORT` 没设 `BD_CDP_PORT` 时按 alias 生效 + stderr 打 deprecation warning |
| `BD_EXTENSION_PORT` | extension backend relay ws server 的绑定端口（v0.5.3 起）。优先级：CLI `--extension-port` > `BD_EXTENSION_PORT` > toml `[backends.extension].port` > 默认 19989。默认就避开 playwriter 的 19988；e2e 测试用它隔离（29989）|
| `BD_FACADE_PORT` | Playwright facade 的绑定端口。优先级：CLI `--facade-port` > `BD_FACADE_PORT` > toml `facade_port` > 默认 19990。`0` = 显式关闭 facade |
| `BD_FACADE_HOST` | Playwright facade 的绑定 host（默认 `127.0.0.1`，loopback）。优先级：CLI `--facade-host` > `BD_FACADE_HOST` > toml `facade_host` > 默认。设成 Tailscale/LAN IP 或 `0.0.0.0` 让别的机器 `connect_over_cdp` 进来；facade 的 `/json/version` 会按请求的 `Host` 头回填 `webSocketDebuggerUrl`，远端拿到的就是它自己用的地址 |
| `BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE` | EXPERT ESCAPE：绕过 launch-chrome 拒绝用户 default profile 的 guard。truthy 值 `1`/`true`/`yes`/`on`/`y`（case-insensitive）unlock。**仅当你完全理解会永久暴露日常 Chrome 给 CDP popup hazard 时** |
| `BD_LOG_JSON` | `1` / `true` / `yes` → daemon log 输出 JSON 行（`{ts, level, logger, msg, extra?, exc_info?}`），方便日志聚合器消费。默认 plaintext |

## 范围（Layer 1 不做的事）

- ❌ **不**做截图、点击、DOM 读取、snapshot。这些属于 Layer 2 的 skill（`browserwright` CLI）。
- ❌ **不**做 session 语义之上的业务逻辑（site skills / tasks / memory）——同样是 Layer 2。

## End-to-end tests with a real Chrome

If you edit the extension (`chrome-extension/background.js`) or daemon
internals, validate against a real Chrome:

    tests/daemon/e2e/run.sh -v

This spawns an isolated Chrome for Testing with a patched copy of the
extension, talking to a test daemon on port 29989. It will not touch your
daily Chrome.

See `tests/daemon/e2e/README.md` for details.
