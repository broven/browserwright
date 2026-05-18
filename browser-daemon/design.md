# browser-daemon — Design

本文是开发者视角的设计文档。用户视角看 [README.md](./README.md)。

## 1. 目标 / 非目标

### 目标

1. 提供一个**最小、清晰**的本地工具，把"如何拿到 Chrome 的 browser-level CDP WebSocket"这件事统一封装。
2. 支持多种发现/连接策略（backend），策略之间可独立演进、可插拔。
3. 输出格式稳定、易被 shell/Python/MCP server 调用。
4. 失败时给出可操作的诊断信息（doctor 子命令）。
5. 把"Chrome 横幅 + 授权弹窗"这两条 UX 影响压到最低。

### 非目标

- 不实现 CDP 客户端的应用层（截图、点击、DOM）——这些是 Layer 2 skill。
- 不启动 Chrome（独立自动化 profile 可能除外，见 §11 backend rdp）。
- 不做跨机分布式调度（v1 仅本地）。

## 2. 实测约束：Chrome 144+ 的 UX 硬约束 {#empirical}

实测脚本：`../cdp-popup-memory-test.mjs`、`../cdp-banner-test.mjs`。结论：

### 2.1 弹窗（"Allow remote debugging?"）

| Phase | 动作 | 结果 |
|---|---|---|
| 1 | 第一次 browser-level WS 连接 | POPUP |
| 2 | 关 ws 后立即重连 | POPUP |
| 3 | 关 ws 5s 后重连 | POPUP |
| 4 | 并行第二个 ws | POPUP |
| 5 | 串行同进程重连 | POPUP |
| 6 | Chrome 进程重启后重连 | POPUP |

→ **AutoConnect 路径下 Chrome 完全无记忆**，每次 browser-level WS 握手都重新要求授权。
→ 含义：autoconnect backend 的客户端必须**长连接、单连接**；任何短连接 / 并发多 ws 都会被弹窗淹没。

### 2.2 横幅（"Chrome is being controlled by automated test software"）

| 事件 | 行为 |
|---|---|
| WS open | 横幅立即出现 |
| WS idle 15s + idle 60s | 横幅保持（与流量无关） |
| WS close（唯一 ws） | 横幅立即消失 |
| WS 重新 open | 立即重新出现 |
| 并行第二个 ws | 横幅无变化（boolean，不是 counter） |
| 关其中一个 ws，另一个还在 | 横幅保持 |
| 关最后一个 ws | 立即消失 |
| 用户点横幅右侧 X 按钮 | 横幅消失，所有 WS **仍活着**、CDP 命令仍工作 |
| 多 WS 时用户点 X | 横幅消失，**所有** WS 都保持（X 与连接数无关） |
| X 之后**同一 WS** 上 createTarget / navigate / attach / evaluate | 横幅**不**重现 |
| X 之后用户手动 Cmd+T / 地址栏导航 | 横幅**不**重现 |
| X 之后开**新** WS | 横幅重新出现（dismiss 是 per-WS，新 WS 重新计数） |

→ 横幅严格绑"是否有任意 browser-level WS 活着" **且** 该 WS 上未被 X dismiss 过。dismiss 是 **per-WS 持久** —— 直到 WS close 才重置。
→ 横幅与具体协议**无关**——无论 autoconnect、`--remote-debugging-port`、还是浏览器扩展走 `chrome.debugger` API，只要外部 CDP client 在用 Chrome，横幅都会出现。
→ X 按钮是**纯 UI dismiss**，CDP 协议层不可见。daemon 不需要监听、不需要响应、也不可能预测。

### 2.3 握手 ↔ 横幅 严格同步（autoconnect 路径）

实测时序：

```
t=0           客户端发起 WebSocket 握手
              Chrome 弹出 Allow popup
              横幅: ❌ 不出现 (popup unanswered 阶段)
t=user_click  用户点击 Allow
              Chrome 立即完成 WS 握手（毫秒级）
              WS 进入 OPEN 状态
              横幅: ✅ 出现
```

实测中"用户点击 Allow → WS open" 的延迟接近 0（脚本输出的 `latency = -1603ms` 实际表明 WS open 发生在用户报告按键之前 1.6s，即握手在键盘反应时间内已完成）。

→ 横幅严格门控于 WS 握手完成，**不在 popup 显示时就出现**。
→ daemon 不可能"静默预热"上游 ws——只要 ws OPEN，横幅就显示，没有"已授权但不显示横幅"的中间状态。
→ 反过来这是**好消息**：mode B 的 lazy connect 用户体验上跟"长连"几乎一致（握手延迟为零，唯一摩擦是点一次 Allow）。

### 2.4 横幅 X dismiss 的意义

实测发现 X 按钮是**纯 UI dismiss + per-WS 持久**（见 §2.2 表 + `../cdp-banner-redisplay-test.mjs`）：

- daemon **不需要**监听 X 事件——根本看不见。
- Layer 2 **不需要**把"用户 dismiss 横幅"当成 disconnect 信号。
- 用户可以在自动化任务运行**中途**手动 dismiss 横幅，所有连接照常工作。
- **dismiss 在当前 WS 上完全持久**：实测确认 dismiss 后 `Target.createTarget` / `Page.navigate` / `Target.attachToTarget` / `Runtime.evaluate` / 用户手动 Cmd+T / 用户手动地址栏导航——**都不会**让横幅重现。横幅只在 WS 关掉再重新连接后才会重新出现。
- 实际含义：**一次 Allow + 一次 X dismiss = 整个工作 session 零干扰**。这强烈降低了 daemon 主动 idle close 的必要性——既然横幅可以被用户一次解决，daemon 就不需要靠 "断开上游" 来减轻横幅困扰了。idle close 此后只服务于"隐私"（不想让 ws 长期 open）这一个动机。

### 2.5 推论

横幅出现位置只取决于"连的是不是用户日常的 Chrome 实例"：

| Backend × Profile | 弹窗 | 横幅是否在用户视线里 |
|---|---|---|
| `rdp` + **独立自动化 profile**（后台 Chrome） | 无 | **否**（用户看不见那个窗口） |
| `rdp` + 用户日常 profile | 无 | 是 |
| `extension` relay（用户日常 Chrome） | 无 | 是 |
| `autoconnect`（用户日常 Chrome） | **每次连** | 是 |

唯一"无打扰长连"路径是 `rdp + 独立 profile`。所有其它路径都需要 daemon 在没人用浏览器时主动断开上游 ws，让横幅消失。

## 3. 两种 daemon 模式

实测约束（§2）逼出一个结论：**单一形态满足不了所有 backend**。所以 daemon 有两种模式，按场景选用。

### Mode A: URL resolver（无状态）

```
$ browser-daemon url --backend rdp
ws://127.0.0.1:9222/devtools/browser/...
```

- 一次性 CLI，解析并输出 ws URL，退出。
- 不持有任何连接、不维护任何状态。
- 上游 Chrome 由用户/启动器自己管理。
- 适用：`rdp + 独立 profile`、`env`（外部已注入）。

### Mode B: CDP proxy daemon（有状态，v0.2）

```
$ browser-daemon serve --backend autoconnect
[daemon] listening on /tmp/browser-daemon.sock
```

- 后台进程，监听本地 socket（unix socket 或 127.0.0.1:N）。
- 暴露一个 CDP-compatible WebSocket：Layer 2 client 连 daemon，daemon 替它复用单根上游 ws。
- 上游 ws 按需打开、按 idle policy 关闭。
- 适用：`autoconnect`、`extension`、`rdp + 用户日常 profile`——所有"用户能看见横幅"的路径。

### 决策矩阵

| Backend | 推荐 mode | 理由 |
|---|---|---|
| `env` | A | 外部已给 URL，daemon 不增价值 |
| `rdp` + 独立 profile | A | 用户看不见横幅，长连无害 |
| `rdp` + 用户日常 profile | B | 横幅在视线里，需要 idle close |
| `autoconnect` | B（**必需**） | 弹窗 + 横幅双重摩擦，必须单长连 |
| `extension` | B | 横幅在视线里 |

> MVP 范围：先做 mode A + `env`/`rdp`/`autoconnect` backend（autoconnect 用 mode A 暂时可用但摩擦明显，文档明确建议）。mode B 在 v0.2 跟进。

## 4. 架构

```
                Mode A: stateless                  Mode B: stateful
                ───────────────                    ─────────────────
   CLI invocation                                  long-running process
        │                                                  │
        ▼                                                  ▼
   ┌──────────┐                                  ┌──────────────────┐
   │ Resolver │                                  │ Proxy Server     │
   │ - parse  │                                  │ - listen sock    │
   │ - dispatch                                  │ - track clients  │
   │ - print  │                                  │ - state machine  │
   └────┬─────┘                                  │ - idle timer     │
        │                                        └────────┬─────────┘
        │                                                 │
        ▼                                                 ▼
   ┌──────────────────────────────────────────────────────────┐
   │ Backend Registry (共享)                                    │
   │ env / rdp / autoconnect / extension                       │
   └──────────────────────────────────────────────────────────┘
        │                                                 │
        ▼                                                 ▼
   stdout: ws URL                          single upstream WS  →  Chrome
   stderr: 诊断                            (daemon manages lifecycle)
   exit:   code
```

模块布局：

```
browser-daemon/
├── pyproject.toml
├── README.md
├── design.md
└── src/
    └── browser_daemon/
        ├── __init__.py
        ├── cli.py              # argparse + subcommand dispatch
        ├── config.py           # toml + env merge
        ├── errors.py           # exception → exit code
        ├── platforms.py        # Chrome user-data-dir per OS
        ├── resolver.py         # Mode A: 一次性 URL 解析
        ├── proxy/              # Mode B: 状态化 CDP proxy
        │   ├── __init__.py
        │   ├── server.py       # listen socket, accept clients
        │   ├── state.py        # DISCONNECTED / CONNECTING / CONNECTED
        │   ├── idle.py         # idle timer 策略
        │   ├── multiplex.py    # sessionId 多路复用
        │   └── upstream.py     # 单根上游 ws lifecycle
        └── backends/
            ├── __init__.py     # registry
            ├── base.py         # Backend Protocol
            ├── env.py
            ├── rdp.py
            ├── autoconnect.py
            └── extension.py    # MVP 占位
└── tests/
    ├── test_resolver.py
    ├── test_backends_*.py
    ├── test_proxy_state.py     # mode B
    ├── test_proxy_idle.py
    └── test_cli.py
```

## 5. Backend Protocol

backend 给两种模式都用——它只关心"如何拿到 ws URL"，不关心调用方是 CLI 还是 daemon。

```python
from typing import Protocol, runtime_checkable
from dataclasses import dataclass

@dataclass
class DoctorResult:
    name: str
    available: bool
    detail: str
    extras: dict | None = None
    ux_warning: str | None = None   # e.g. "每次连接都触发 Chrome 授权弹窗"

@runtime_checkable
class Backend(Protocol):
    name: str
    recommended_mode: str  # "A" | "B"

    async def resolve(self, timeout: float) -> str | None:
        """返回 browser-level ws URL，或 None 表示当前不可用。"""
        ...

    async def doctor(self) -> DoctorResult:
        """cheap probe，结构化诊断。"""
        ...
```

要点：
- backend 之间互不依赖；fallback 由 resolver/proxy 调度。
- 每个 backend 自己处理超时。
- `recommended_mode` 给 CLI / doctor 用，提示"这个 backend 用 mode A/B 更合适"。

## 6. Mode A 逻辑：Resolver（一次性 URL 输出）

```python
async def resolve(backend_arg: str | None, timeout: float, config: Config) -> str:
    if backend_arg:
        backend = registry.get(backend_arg)
        url = await backend.resolve(timeout)
        if url is None:
            raise BackendUnavailable(backend.name)
        return url

    chain = config.fallback_chain   # 默认: ["env", "rdp"]，autoconnect/extension 不在默认链
    last_errors = []
    for name in chain:
        backend = registry.get(name)
        try:
            url = await backend.resolve(timeout)
            if url:
                return url
            last_errors.append((name, "unavailable"))
        except Exception as e:
            last_errors.append((name, str(e)))
    raise AllBackendsUnavailable(last_errors)
```

**默认 fallback chain 不含 autoconnect/extension**，理由：

- autoconnect：默认参与意味着用户每次调用 `browser-daemon url` 都可能触发弹窗。
- extension：依赖外部扩展运行，"悄悄 fail-through"会浪费 timeout 预算。

要走这两个 backend，必须 `--backend autoconnect` 或写进 config `fallback_chain`。

## 7. Mode B 逻辑：状态机 + Idle Policy

### 7.1 状态机

```
        ┌──────────────┐
        │ DISCONNECTED │ ← 初始
        └──────┬───────┘
               │ 第一个 client 连入
               ▼
        ┌──────────────┐
        │ CONNECTING   │ ← 上游 ws 握手中
        │              │   (autoconnect 在此时弹 Allow)
        └──────┬───────┘
               │ upstream open
               ▼
        ┌──────────────┐
        │ CONNECTED    │ ← 横幅可见，处理 client 流量
        └──────┬───────┘
               │ 最后一个 client 走 + idle timeout
               │ 或显式 disconnect
               ▼
        ┌──────────────┐
        │ CLOSING      │ ← drain upstream
        └──────┬───────┘
               ▼
            DISCONNECTED
```

异常路径：
- upstream 自己掉线（Chrome 退出）→ 直接 → DISCONNECTED，所有 client 收到 ws close。
- backend 握手失败 → CONNECTING 回退 DISCONNECTED，告知 client。

### 7.2 Idle 监控

**判定"空闲"= client 集合为空**。这是连接级 idle，daemon 天然知道。

```python
async def on_client_connect(c):
    self.clients.add(c)
    if self.idle_timer: self.idle_timer.cancel()
    if self.upstream is None:
        await self._open_upstream()      # (autoconnect 在此弹 Allow)

async def on_client_disconnect(c):
    self.clients.discard(c)
    if not self.clients and self.upstream:
        self.idle_timer = asyncio.create_task(self._idle_close())

async def _idle_close(self):
    await asyncio.sleep(self.idle_close_after_s)
    if not self.clients:
        await self.upstream.close()      # 横幅消失
```

**半死连接兜底**：

1. WebSocket ping/pong：daemon 每 20s 发 ping，client 60s 不回 → 强制 close 该 slot。
2. OS TCP keepalive：兜底，几十秒内检测半开。

**流量级 idle**（默认不做）：可选 `traffic_idle_after`，跟踪每个 client 最后一次 CDP 命令时间戳，超时主动 close 该 client。Layer 2 REPL 进程在思考时间长，不要默认启用。

### 7.3 按 backend 配置 idle policy

```toml
# ~/.config/browser-daemon/config.toml

[backends.rdp]
idle_close_after = "never"       # 独立 profile, 横幅不可见, 长连无害

[backends.autoconnect]
idle_close_after = "never"       # 横幅可由 X dismiss 一次性解决；
                                  # autoconnect 重连必弹 Allow, 主动断 = 强制再点 Allow

[backends.extension]
idle_close_after = "1h"          # 无弹窗摩擦, 折中：长任务能跑完, 长 idle 也保护隐私

[proxy]
traffic_idle_after = "never"     # 默认不启用流量级 idle
ping_interval = "20s"
ping_timeout = "60s"
```

`idle_close_after` 接受 `"never"` / `"0"` / `"5m"` / `"30m"` 等。`0` = 立即（client 一走就断），适合极注重隐私的用户。

**为什么 `autoconnect` 默认 `"never"`，`extension` 默认 `"1h"`**：

- §2.4 实测确认横幅 dismiss 是 per-WS 持久，所以"主动断开"不再是"减轻横幅干扰"的有效手段。idle close 现在只服务于隐私动机。
- `autoconnect`：每次断开后重连**必然**弹 Allow（§2.1 实测无记忆）。主动断 = 强制用户重新点 Allow。横幅可由 X dismiss 解决，弹窗不能。所以默认偏向"少弹一次是一次"。
- `extension`：扩展走 `chrome.debugger` 权限模型，重连零弹窗摩擦。`1h` 折中：典型工作任务跑完后断，长 idle 保护隐私。
- `rdp` + 独立 profile：用户根本看不见横幅，长连无害，`never`。

### 7.4 SessionId 多路复用

daemon 替每个 client 维护独立的 CDP session 视图，但底层共用一根上游 ws。要点：

- 每个 client 连入时，daemon 分配一个"虚拟 browser session"——但仍然把上游 `Browser.*` 命令转发到同一根 ws。
- `Target.attachToTarget` 返回的 sessionId 直接透传，client 后续命令带原始 sessionId 即可。
- **事件 fanout**：上游下来的 CDP event，按 sessionId 路由到 attach 它的 client；没有 sessionId 的 browser-level event 默认广播给所有 client。
- 多 client 操作**同一个 target**的策略：MVP 阶段先禁止（第二个 client attach 已被 attach 的 target 返回错误），避免互相干扰。

> 这部分是 mode B 的工程难点。Playwriter 的 extension relay 已经做过类似实现，可以参考其 multiplexer。

## 8. CLI 设计

### Mode A 子命令

```
browser-daemon url [--backend NAME] [--timeout SEC] [--config PATH]
browser-daemon list-backends [--json]
browser-daemon doctor [--json]
browser-daemon version
```

`url` 行为见 §6。`doctor` 每个 backend 输出包含 `ux_warning`，例如：

```
autoconnect  available  port=49623 path=/devtools/browser/...
             ⚠ 每次新 WS 连接都会触发 Chrome "Allow remote debugging?" 弹窗。
             推荐: 改用 mode B (`browser-daemon serve --backend autoconnect`) 单长连接。
```

### Mode B 子命令（v0.2）

```
browser-daemon serve   [--backend NAME] [--socket PATH] [--port N]
browser-daemon stop
browser-daemon status  [--json]
browser-daemon connect           # 强制立即打开上游 ws（预热）
browser-daemon disconnect        # 强制立即关上游 ws (banner 消失)
browser-daemon logs    [--follow]
```

`status` 输出格式（人类可读）：

```
state:        CONNECTED
backend:      autoconnect
upstream ws:  ws://127.0.0.1:49623/devtools/browser/abc...
clients:      2
  - pid=51234 connected 00:12:34 ago (browser-skill-repl)
  - pid=51299 connected 00:00:08 ago (daily-task:damai-check)
idle timer:   not running (clients > 0)
banner:       visible (upstream alive)
```

clients = 0 时：

```
state:        CONNECTED (idle countdown 03:42 → DISCONNECT)
clients:      0
idle timer:   will close upstream in 03:42
banner:       visible (upstream alive)
```

### Exit codes

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 用户错误（参数非法、未知 backend） |
| 2 | 后端不可用 |
| 3 | 内部错误 |
| 4 | daemon 未运行（mode B 控制命令调用时） |
| 5 | daemon 已在运行（serve 时） |

### 全局参数

| 参数 | 等价 env | 说明 |
|---|---|---|
| `--backend NAME` | `BD_BACKEND` | 强制指定 backend |
| `--timeout SEC` | `BD_TIMEOUT` | 单 backend 超时，默认 5s |
| `--config PATH` | `BD_CONFIG` | config 文件 |
| `-v` / `--verbose` | `BD_VERBOSE=1` | 写诊断到 stderr |
| `--json` | — | `list-backends` / `doctor` / `status` |

## 9. 各 Backend 细节

### env

- `BD_CDP_WS` 存在 → 直接返回。
- 否则 `BD_CDP_URL` 存在 → HTTP GET `{BD_CDP_URL}/json/version` → 读 `webSocketDebuggerUrl`。
- 都没有 → None。
- `recommended_mode = "A"`。

### rdp

- 读 config `backends.rdp.port`（默认 9222）。
- HTTP GET `http://127.0.0.1:<port>/json/version`。
- 失败 → None。
- `recommended_mode`：依赖配置——如果用户配了独立 profile（`backends.rdp.user_data_dir` 指向非默认路径）→ A；否则 → B。
- v0.2 可加 `browser-daemon launch-chrome` 子命令帮用户起独立 profile Chrome（avoiding 让用户记一长串参数）。

### autoconnect

- 平台默认 Chrome user-data-dir：
  - macOS: `~/Library/Application Support/Google/Chrome`
  - Linux: `~/.config/google-chrome`
  - Windows: `%LOCALAPPDATA%\Google\Chrome\User Data`
- 递归找 `DevToolsActivePort`，拼 ws URL。
- 多个 profile 都有 → 取最新 mtime + stderr 提示。
- `recommended_mode = "B"`。
- doctor 必输出 `ux_warning`：弹窗 + 横幅双重摩擦，推荐 mode B 单长连。

### extension（MVP 占位）

- 接口预留：连 `config.backends.extension.relay_url`，发 ping 验证。
- MVP 返回 None + doctor 提示 not implemented。
- `recommended_mode = "B"`。

## 10. 错误模型

```python
class BrowserDaemonError(Exception):
    exit_code: int

class UserError(BrowserDaemonError):
    exit_code = 1

class BackendUnavailable(BrowserDaemonError):
    exit_code = 2

class AllBackendsUnavailable(BrowserDaemonError):
    exit_code = 2

class DaemonNotRunning(BrowserDaemonError):
    exit_code = 4

class DaemonAlreadyRunning(BrowserDaemonError):
    exit_code = 5
```

CLI 顶层 catch：对应 exit code + stderr 写消息。

## 11. 测试策略

**Mode A**:
- env / autoconnect：mock 文件系统 + env。
- rdp：aiohttp fake server。
- resolver：mock backend，测 fallback 顺序、显式 `--backend`、聚合错误。
- CLI：subprocess 调用，断言 exit code + stdout/stderr。

**Mode B**:
- state machine：单元测试状态转换。
- idle timer：fake clock 测 timeout 取消、reschedule。
- multiplexer：模拟两个 client 共用上游，断言 sessionId 路由 + event fanout。
- 半死连接：mock 不发 close 的 client，断言 ping/pong 超时后清理。
- 集成测试（可选）：真实 headless Chrome + 真实 daemon + 多 client。

## 12. 命名与边界 {#naming}

为什么叫 `browser-daemon` 但 MVP（mode A）并非 daemon？

- 名字预留给 mode B（真正的长驻 daemon）。
- 用户文档里 daemon 这个词已经反复出现（Layer 1 通称），保留便于跨文档引用。
- mode A 单独看更像 `git config --get`——一次性查询。

**关键边界**：所有 CDP 协议级的"应用"工作（截图、点击、DOM、自动化高层 API）都属于 Layer 2，不在 daemon 范围内。daemon 只做"提供 ws URL"（mode A）或"代理 ws 流量 + 管理生命周期"（mode B）。

## 13. 与上层 Layer 2 的接口

Layer 2 怎么用 daemon，取决于 mode：

### Mode A

```python
# 一次性拿 URL
import subprocess
ws_url = subprocess.check_output(["browser-daemon", "url"], text=True).strip()
# 然后 Layer 2 用 CDP 客户端直接连 ws_url
```

或 shell 注入：
```bash
export BD_CDP_WS=$(browser-daemon url)
browser-skill <<'PY' ... PY
```

### Mode B

```python
# Layer 2 连 daemon 暴露的本地 socket，而不是 Chrome 直接
import websockets
async with websockets.connect("ws+unix:///tmp/browser-daemon.sock") as ws:
    # 跟连 Chrome 一样发 CDP 命令
    await ws.send('{"id":1,"method":"Target.getTargets"}')
    ...
```

或通过 `browser-daemon url --mode-b-proxy` 拿到 proxy 端点：
```bash
$ browser-daemon url --mode-b-proxy
ws+unix:///tmp/browser-daemon.sock
```

Layer 2 不需要知道底层是哪个 backend / Chrome 在哪。daemon 替它处理所有 lifecycle。

### 解耦原则

- Layer 2 不直接 import `browser_daemon`。
- Mode A：subprocess + stdout。
- Mode B：socket + 标准 CDP 协议（WebSocket text frames + JSON）。
- 升级 daemon 不影响 Layer 2 二进制依赖。
- Layer 2 测试可以用 env 注入 mock URL，或起一个 mock proxy server。

## 14. 开放问题

- [ ] config schema 校验：pydantic vs 手写？倾向手写（避免重依赖）。
- [ ] Windows 平台 Chrome 路径，Edge/Brave/Chromium 兼容范围？MVP 只测 Chrome stable。
- [ ] doctor 的"下一步建议"i18n？暂时只英文。
- [ ] Mode B：多 client 同时 attach 同一 target 怎么处理？MVP 禁止，文档说明。
- [ ] Mode B：上游 ws 断了重连时，已有 client 的 sessionId 失效该怎么通知？倾向于"广播 fake `Target.detachedFromTarget` 事件 + 关 client 连接"。
- [ ] `launch-chrome` 子命令的实现细节（独立 profile 路径策略、Chrome 二进制查找）。

## 15. 后续版本路线

- **v0.1 (MVP)**: Mode A + `env` / `rdp` / `autoconnect` backend（autoconnect 在 mode A 下文档明确摩擦，作为权宜）。`extension` 占位。
- **v0.2**: Mode B 上线——proxy server、状态机、idle policy、sessionId 多路复用。autoconnect / extension 推荐 mode B。
- **v0.3**: `browser-daemon launch-chrome` 帮用户启动独立 profile Chrome（rdp + 隔离 profile 一站式）。
- **v0.4**: `cloud` backend（Browser Use 等远程浏览器服务）。
- **v0.5**: 跟 Layer 2 skill 仓库整理 sensible defaults、性能调优、observability。
