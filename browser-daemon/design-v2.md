# browser-daemon — Design v2

> **历史说明（2026-05）**：原先的 `autoconnect` backend（通过 Chrome `--remote-debugging-port=9222` 直连日常浏览器）已经彻底删除。要驱动用户日常 Chrome，请使用 `extension` backend。下文保留的 `autoconnect` 字眼属于历史决策记录，**不再反映当前实现**。

**状态**：v1 draft，所有节实质内容已落。基于 `browser-skill/design.md` §D（Daemon ↔ Skill 边界）+ skill-architect 通过 SendMessage 签字的 US1-4 整合 + 双向澄清往返。每条 daemon 设计决策都 trace 回 §3 硬/软需求清单。

> 历史参考：`./design.md`（v0，前一轮架构师笔记，结构性决策已被本文件覆盖，但 §2 实测约束源自该文件，物理事实不可变）；`./design-review.md`（旧版的逐节 critique，已退役，有用素材吸收到本文件附录 A/B）。

---

## 0. 大纲（叙事顺序）

```
§1  为什么 daemon 存在（一句话）
§2  实测约束（物理事实，不变）
§3  Skill 的硬需求（11 条硬 + 3 条软，trace 来源）
§4  由此推出的 daemon 形态
§5  Mode A 协议合同（v0.1：url / doctor / list-backends / active-tab / launch-chrome / version）
§6  Mode B 协议合同（v0.2+：socket + 标准 CDP + BrowserDaemon.* 命名空间）
§7  v0.1 / v0.2 / v0.3 / v0.4 / v0.5 路线图
§8  Backend 实现细节
§9  测试策略
§10 开放问题（已收敛）
§11 命名 / 边界
附录 A — 参考项目挖矿（borrow / reject 双栏）
附录 B — 拒绝清单（防 scope creep）
```

---

## 1. 为什么 daemon 存在

**一句话**：把"如何拿到 Chrome 的 browser-level CDP WebSocket"这件事统一封装，让上层 Skill 不关心底层是 `--remote-debugging-port` / Chrome AutoConnect / 浏览器扩展 relay。

更长版本：Skill 层（REPL + 站点固化层 + memory）需要一根稳定的 CDP 连接。这根连接的获取方式跨平台、跨 Chrome 版本、跨用户配置有多条路径，每条都有自己的弹窗 / 横幅 / 授权 / 端口漂移问题。Skill 不该自己背这一摊；daemon 把它收敛成两条接口：

- **subprocess 出一个 ws URL**（Mode A，stateless）
- **socket 出一根 CDP 复用 channel**（Mode B，stateful）

这是 daemon 全部存在意义。daemon **不**做截图 / 点击 / DOM / 应用层重试 / cookie 管理——那是 Skill。

---

## 2. 实测约束（物理事实，不变）

源自旧 `design.md` §2，实测脚本在 `../cdp-popup-memory-test.mjs`、`../cdp-banner-test.mjs`、`../cdp-banner-redisplay-test.mjs`。**这一节是物理事实，所有设计决策必须服从**。

### 2.1 Allow remote debugging 弹窗（autoconnect 路径）

| Phase | 动作 | 结果 |
|---|---|---|
| 1 | 第一次 browser-level WS 连接 | POPUP |
| 2 | 关 ws 后立即重连 | POPUP |
| 3 | 关 ws 5s 后重连 | POPUP |
| 4 | 并行第二个 ws | POPUP |
| 5 | 串行同进程重连 | POPUP |
| 6 | Chrome 进程重启后重连 | POPUP |

→ Chrome 144+ **完全无记忆**。每次 browser-level WS 握手都重新要求授权。
→ 含义：autoconnect 路径必须**单长连接**；任何短连重连都会被弹窗淹没。

### 2.2 "Chrome is being controlled..." 横幅

| 事件 | 行为 |
|---|---|
| WS open | 横幅立即出现 |
| WS idle 15s / 60s | 横幅保持（与流量无关） |
| WS close（唯一 ws） | 横幅立即消失 |
| 并行第二个 ws | 横幅无变化（boolean，不是 counter） |
| 用户点 X dismiss 横幅 | 横幅消失，所有 WS **仍活**，CDP 仍工作 |
| dismiss 后 createTarget / navigate / attach / evaluate | 横幅**不**重现 |
| dismiss 后开**新** WS | 横幅重新出现（per-WS 重置） |

→ X dismiss 是 per-WS 持久。**一次 Allow + 一次 X dismiss = 整个 session 零干扰**。
→ idle close 不再服务"减轻横幅"——只服务隐私。

### 2.3 握手 ↔ 横幅 严格同步

WS OPEN 的瞬间横幅出现，零延迟、无"已授权但不显示"的中间状态。daemon 无法"静默预热"upstream ws。

### 2.4 Backend × Profile 矩阵

| Backend × Profile | 弹窗 | 横幅是否在用户视线里 |
|---|---|---|
| `rdp` + **独立自动化 profile** | 无 | **否**（后台 Chrome） |
| `rdp` + 用户日常 profile | 无 | 是 |
| `extension` relay（用户日常 Chrome） | 无 | 是 |
| `autoconnect`（用户日常 Chrome） | **每次连** | 是 |

唯一"无打扰长连"路径是 `rdp + 独立 profile`。

### 2.5 推论（约束如何决定设计）

- 短连重连模式被 Chrome 144+ 实测排除 → autoconnect 必须 Mode B 或 Skill 长驻 ws。
- daemon 主动 idle close 仅服务隐私 → 默认 `idle_close_after = never`，显式 `disconnect` 子命令暴露给 Skill 用作主动隐私决策（详见 §6）。
- doctor probe **不能预热 ws**，否则一次 doctor = 一次弹窗 → doctor 默认零 ws 副作用，`--probe-ws` opt-in（详见 §5）。

---

## 3. Skill 的硬需求

源自 `browser-skill/design.md` §D（Daemon ↔ Skill 边界）+ 经我和 skill-architect 双向 SendMessage 确认 + US1-4 整合。**这一节是 daemon 存在意义的"答案对照表"**——每条 daemon 设计决策都要 trace 回这里。

### 3.1 硬需求清单

> **Cross-ref scheme (B)**：`browser-skill/design.md §D.2.x` 是需求正本（编号 frozen，未来只追加）。本表 H 编号与 skill-architect §D.9 cross-ref 节对齐。如果 skill-architect 追加新 D 条目，新增对应 H 行即可。

| # | 需求 | D.2.x | 落在本文档 |
|---|---|---|---|
| H1 | `browser-daemon url` subprocess + stdout 单行 ws URL + 稳定 exit codes (0/1/2/3) | §D.2.1 | §5.1 |
| H2 | `browser-daemon doctor --json` 稳定 JSON shape（`schema_version=1`）**+ 默认零 ws 副作用**（`--probe-ws` opt-in） | §D.2.2 + §D.2.3 | §5.2 |
| H3 | `list-backends --json` 含 `needs_user_action` + `ux_cost` 机器可读字段 | §D.2.4 | §5.3 |
| H4 | Mode B 标准 CDP wire format，任意 CDP 客户端库（cdp-use / 自卷）零改动 | §D.2.5 | §6.3 |
| H5 | sessionId 行为：**v0.2 passthrough**（单 client 无冲突）；v0.3 加翻译表 | §D.2.6 | §6.3 + §3.4 |
| H6 | stale session → daemon 关 client ws **1011** + 顺序 fake `Target.detachedFromTarget` + `BrowserDaemon.upstreamClosed` event | §D.2.7 | §6.5 |
| H7 | **v0.3+** 单 target 单 attacher 规则（v0.2 由 Skill 进程内 discipline 保证；v0.3 daemon 实装 + 第二次 attach 同 target 返回 `-32602`） | §D.2.10 | §3.4 + §7 v0.3 + §9.5 |
| H8 | `BrowserDaemon.getActiveTab` RPC + `accuracy` 字段 + `subscribeFocus` 推 `activeTabChanged` 事件 | §D.2.8 + §D.2.9 | §5.4 + §6.4 + §6.4.1 |
| H9 | `browser-daemon launch-chrome` 子命令拉到 **v0.1**，install wizard 用 | §D.2.12 | §5.5 |
| H10 | `--backend <name>` flag + `BD_BACKEND` env 接受 Skill 从 memory 读到的 backend 偏好（US4） | §D.2.13 | §5.1 args + §8 |

**Meta-原则**（不占 H 编号，因为不是 user-facing 需求，是 daemon 的内部 scope discipline）：

- daemon **不**做应用层（截图缓存、retry、cookie 管理、CDP recording）。落在 §11 边界 + 附录 B 拒绝列表。
- daemon **不**自动重连 upstream。落在 §6.5。

**D.2.14 / D.3.3-5 等 decision / 砍掉 / 推迟项** 不占 H 编号，反映在 §3.3：

- §D.2.14 socket 握手切 backend = 不做 → §3.3 + §6.2.1
- §D.3.3 → 用 endpoint 替代，不做
- §D.3.4 → 砍掉
- §D.3.5 → 推迟 v0.3+

### 3.2 软需求清单

| # | 需求 | D.x.y | 落点 |
|---|---|---|---|
| S1 | `BrowserDaemon.uiState` 查询（`ws_count` + `last_popup_resolved_at` + `banner_visible_estimated`），Skill 用作 doctor 报告 + 重连后避免重复提示 | §D.3.1 | §6.4 |
| S2 | `?client=skill-repl`（v0.2 唯一值）/ `?client=skill-task`（v0.3+）query label，纯诊断用 | §D.2.11 | §6.2.1 |
| S3 | backend 命名透明给 Skill，case-on-name 仅限 UI 文案 | §D.3.2 | §6.1 + §11 |

### 3.3 砍掉 / 不做的需求

避免读 design-review.md 历史草稿时被误导，明确列：

- ❌ `BrowserDaemon.setLabel` —— `?client=` query 静态 label 够用，v0.2 单进程 repl 不需要运行时改名。
- ❌ `BrowserDaemon.listClients` —— v0.2 单 client 没列表意义；v0.3 多 client 也都是 Skill 自家进程，`browser-daemon status` 子命令足够，不上 RPC。
- ❌ `?intent=` query 单独字段 —— 用 `?client=` 推断。
- ❌ `tapCommandLog`（让 Skill 拿到 daemon 见到的所有 CDP 流量） —— overreach，Skill 自己记 history。
- ❌ 运行时 socket 握手层切 backend（§D.2.14）—— 已 running daemon 切 backend = 双 upstream，是 cloud daemon scope。Skill 在 launch daemon 时 fix backend（H10 走 CLI `--backend` flag / `BD_BACKEND` env）。
- ❌ daemon 自动重连 upstream —— autoconnect 重连 = 弹窗，daemon 不能凭空冒弹窗给用户。
- ❌ daemon 启动 Chrome —— 例外是 `launch-chrome` 子命令（H9），它**只是 helper**，不是 daemon 内置自启动行为。

### 3.4 v0.2 / v0.3 client multiplex 划分

（对应 Skill §D.2.10 decision 项。）skill-architect 在 US1-4 整合（design.md v1）里 finalize 了：

> **v0.2 = 单 skill-repl client。** Skill 在自己进程内做 multiplex：长驻 repl daemon 持一根 ws 到 browser-daemon，REPL inline heredoc + task 调用都复用这根 ws（Skill 进程内分发命令、按内部 sessionId-table 路由响应）。daemon 只看到 ONE client connection。
>
> **v0.3 = daemon 上 multi-client mux。** daemon 接受多 client 连接、单 target 单 attacher 规则、event fanout 按 sessionId 路由、可选 opt-in "shared read" 模式（第二个 attacher 拿只读 sessionId）。

这样 v0.2 daemon 实装大幅简化（不需要 multiplexer），Skill 进程级 multiplex 是 Skill 的事——daemon **只是一根 ws 的透明 CDP proxy**。

**v0.2 仍要做的事**（即使单 client）：

- sessionId pass-through（client attach 出的 sessionId 透传，daemon 不翻译）
- `BrowserDaemon.*` 命名空间（subscribeFocus、disconnect 等都是单 client 也需要的 RPC）
- upstream 关闭礼仪（H6，单 client 一样要 1011 + fake detached）
- 状态机、idle policy、`disconnect` 子命令

**v0.2 不做的事**：

- 第二个 client connection 直接 reject（HTTP 503 或 ws upgrade refuse），文档说明 v0.3 才上。
- sessionId 翻译表（v0.2 单 client 无冲突，passthrough 即可；v0.3 多 client 才需要）。
- 事件路由仲裁（v0.2 只一个 client，所有 event 都给他）。

---

---

## 4. 由此推出的 daemon 形态

§2 物理约束 + §3 Skill 需求合并 → 形态收敛到：

### 4.1 两种调用形态共存

- **Mode A — URL resolver（无状态 CLI）**
  `browser-daemon url` 一次性输出 ws URL 退出。daemon 不持有任何连接，不做长驻进程。**Skill 自己开 ws 自己 hold**。适用于 `rdp + 独立 profile`（无弹窗、无横幅，长连无害）和 `env`（外部已注入）。也适用于 autoconnect / extension，但用户体验上每次 spawn = 每次弹窗（autoconnect）/ 每次横幅闪（其它）——通过 Skill 层 `repl start` 长驻形态规避。

- **Mode B — CDP proxy daemon（有状态 socket）**
  `browser-daemon serve` 长驻进程，监听 unix socket（POSIX）/ TCP loopback（Windows），暴露标准 CDP browser-level WebSocket。多个 Skill 进程（或同一 Skill 的多次调用）连同一个 socket，daemon 复用单根上游 ws。适用于"用户能看见横幅"的所有路径（autoconnect / extension / rdp + 用户日常 profile），把 §2.1 "短连重连 = 反复弹窗" 的硬约束规避掉。

### 4.2 同一 Backend Protocol 服务两种形态

backend 不关心调用者是 Mode A CLI 还是 Mode B proxy。它只回答一件事："在当前主机/用户/Chrome 状态下，给我一个 browser-level CDP ws URL（或宣告不可用 + 原因）"。

```python
class Backend(Protocol):
    name: str
    kind: BackendKind     # UPSTREAM_WS | LOCAL_RELAY
    recommended_mode: Mode  # A | B
    ux_cost: UxCost       # "none" | "banner" | "popup-per-ws+banner" | "extension-permission"

    async def probe(self) -> DoctorResult:
        """cheap, side-effect-free. NO ws open. NO Chrome popup."""
        ...

    async def resolve(self, timeout: float) -> ResolveResult:
        """may trigger UI in autoconnect path. Returns ws URL or raises."""
        ...
```

`probe` 和 `resolve` 拆开是 §3 需求 #3 的直接产物——doctor 调 `probe`，永远不预热 ws。`url` 子命令调 `resolve`，仍然不开 ws（让 Skill 开第一条 ws），只是 HTTP discovery / 文件系统读这些零副作用动作。

### 4.3 Backend 列表（v0.1 范围）

| name | kind | recommended_mode | ux_cost | 何时可用 |
|---|---|---|---|---|
| `env` | UPSTREAM_WS | A | none | `BD_CDP_WS` 或 `BD_CDP_URL` 已注入 |
| `rdp` | UPSTREAM_WS | A 或 B | none/banner | Chrome 启动时带 `--remote-debugging-port=NNNN` |
| `autoconnect` | UPSTREAM_WS | B (v0.2 后) | popup-per-ws+banner | 用户在 `chrome://inspect/#remote-debugging` 启用 |
| `extension` | LOCAL_RELAY | B | extension-permission | 用户安装并启用 daemon 配套 Chrome 扩展（v0.4 实装） |

`kind=UPSTREAM_WS`：daemon 拿到 ws URL 后自己开 ws（或让 Skill 开）。
`kind=LOCAL_RELAY`：daemon 自己当中间人，通过另一条协议（Chrome 扩展的 `chrome.debugger` API）与浏览器通信，对上层暴露的 ws 是 daemon 模拟的。

### 4.4 支持的浏览器源全景

§4.3 的 4 个 backend 名字内敛，让"daemon 支持哪些浏览器形态"看上去窄。下表把"用户场景"映射到 backend，澄清覆盖面：

| 用户场景 | 对应 backend | v0.X 状态 | 备注 |
|---|---|---|---|
| 用户日常 Chrome（已开着，Chrome 144+） | `autoconnect` | v0.1 ✅ | Mode A 每次新 ws 触发弹窗；Mode B (v0.2) 长驻规避 |
| 用户日常 Chrome（已开着，`--remote-debugging-port` 启动） | `rdp` | v0.1 ✅ | 用户日常 profile，横幅在视线里 |
| 脚本 launched Chrome（隔离 profile） | `rdp` + `launch-chrome` (H9) | v0.1 ✅ | 横幅在后台窗口，用户看不见；唯一"无打扰长连"组合（§2.4 表） |
| 指纹浏览器（AdsPower / MultiLogin / GoLogin / 比特浏览器 / Hidemyacc 等） | `rdp` | v0.1 ✅（隐式） | 它们都遵循 `/json/version` HTTP discovery 协议；用户 `--backend rdp --port <动态端口>` 即可。daemon 不需要为每个指纹浏览器写特殊路径 |
| 云端浏览器（Browser Use / Browserless / Hyperbrowser，**用户已手动拿到 ws URL**） | `env` | v0.1 ✅ | 设 `BD_CDP_WS=wss://...` 或 `BD_CDP_URL=https://...`；daemon 透明 forward。鉴权范围见 §8.1.1 |
| 云端浏览器（一等公民、auto-spawn session、计费监控、cloud-side reconnect） | `cloud`（专用 backend） | v0.5 ⏸ | profile sync、session 生命周期、HTTP header 鉴权抽象——env 模式覆盖不了的部分 |
| 浏览器扩展 relay（playwriter 风格 / 自家扩展） | `extension` | v0.4 ⏸ | LOCAL_RELAY kind，走 `chrome.debugger` API 模型 |

**设计原则**：daemon 不为"哪个具体厂商"开洞。任何暴露标准 CDP discovery (`/json/version`) 或 CDP ws 入口的源都能通过 `env` / `rdp` 接入。指纹浏览器、PortableChrome、Ungoogled-Chromium、Cypress 内置 Chromium 等隐式归到 `rdp` —— 不需要列举支持矩阵，**协议兼容性 = 兼容性**。

特殊化（`cloud` / `extension`）只在两件事达不到时引入：(1) discovery 协议不是标准 `/json/version`，(2) 鉴权超出 URL 内嵌 token 范围（详见 §8.1.1）。

---

## 5. Mode A 协议合同（v0.1）

> ★ 这是 Skill v0.1 的唯一入口。Skill 通过 subprocess + stdout 解析对接，**不 import** `browser_daemon` python 包。

### 5.1 `browser-daemon url`

**调用**

```
browser-daemon url [--backend NAME] [--timeout SEC] [--json] [--config PATH] [-v]
```

**stdout 行为**

- 非 `--json`：精确一行，以 `\n` 结尾，URL 以 `ws://` 或 `wss://` 开头，无前后空白。**除此之外不输出任何东西到 stdout**。Skill 用 `subprocess.check_output(...).strip()` 直接拿到。
- `--json`：单行 JSON：
  ```json
  {"schema_version":1,"ws_url":"ws://...","backend":"rdp","extras":{"isolated_profile":false,"profile_path":"/Users/.../Default"}}
  ```
  `extras` 中已知字段：`isolated_profile: bool`（仅 rdp）、`profile_path: str | null`（rdp/autoconnect）。

**stderr 行为**

- 默认：仅在失败时输出 1-3 行人类可读 reason。
- `-v`：每个 backend 一次尝试的诊断（"trying env... BD_CDP_WS not set"、"trying rdp... HTTP 200 ok"），帮助调试。

**Exit codes**

| code | 含义 |
|---|---|
| 0 | 成功，stdout 有 URL |
| 1 | 用户错误（参数非法、未知 backend 名） |
| 2 | 所有 backend 不可用 / 显式 backend 不可用 |
| 3 | 内部错误（崩溃、未预期异常） |

Skill 据此决定：3 = 报 bug；2 = 提示 user 起 Chrome / 启用 inspect；1 = 拒绝 Skill 自身配置。

**环境变量**

| 优先级 | 变量 | 含义 |
|---|---|---|
| 1 | `BD_BACKEND` | 等价 `--backend`，命令行优先 |
| 2 | `BD_CDP_WS` | env backend 直读 |
| 2 | `BD_CDP_URL` | env backend 走 `/json/version` |
| 3 | `BU_CDP_WS` / `BU_CDP_URL` | compat alias（browser-harness 命名），doctor 提示迁移 |
| - | `BD_CONFIG` | config 文件路径 |
| - | `BD_TIMEOUT` | 单 backend 超时秒数，默认 5 |
| - | `BD_NAME` | Mode B 多实例名（Mode A 忽略），默认 `default` |

**URL 稳定窗口语义**

daemon **不**承诺 URL 永久有效。Chrome 重启 / port 漂移 / autoconnect ws 失效都会让缓存的 URL 失效。Skill 自己负责：

```python
# Skill 端的标准用法（已在 browser-skill/design.md §D.3 落地）
class ModeAClient:
    async def get_cdp_connection(self):
        if self._cached_url:
            try:
                return await CDPConn.connect(self._cached_url)
            except (ConnectionRefusedError, InvalidStatus):
                self._cached_url = None  # 触发 re-resolve
        proc = await asyncio.create_subprocess_exec(*self._cmd, ...)
        # ... parse stdout, cache, connect
```

第一次失败 → 重 resolve 一次 → 再失败抛 `DaemonUnavailable`。**绝不**指数退避循环重试。

### 5.2 `browser-daemon doctor`

**调用**

```
browser-daemon doctor [--backend NAME] [--probe-ws] [--json] [-v]
```

**默认行为：零 ws 副作用**

doctor 默认只做：
- HTTP GET `/json/version`（对 rdp）
- 文件系统读 `DevToolsActivePort`（对 autoconnect）
- env 变量检查（对 env）
- 配置加载

**不**打开任何 ws，**不**触发 Chrome popup，**不**让横幅闪。`autoconnect` backend 的 `ws_url` 字段在默认 doctor 输出里是 `null`（"我看到 DevToolsActivePort 存在但没真握手过"）。

`--probe-ws` 是 opt-in：对每个 available backend 真开一次 ws 握手验证活性。这条**会**触发 autoconnect 弹窗，文档显式说明，用户自己选。

**JSON shape（locked, schema_version=1）**

```json
{
  "schema_version": 1,
  "recommended": "rdp",
  "backends": [
    {
      "name": "rdp",
      "available": true,
      "ws_url": "ws://127.0.0.1:9222/devtools/browser/abc...",
      "detail": "DevToolsActivePort matched on /Users/.../Default port 9222",
      "ux_warning": null,
      "needs_user_action": null,
      "ux_cost": "none"
    },
    {
      "name": "autoconnect",
      "available": true,
      "ws_url": null,
      "detail": "DevToolsActivePort present at /Users/.../Default, ws not probed",
      "ux_warning": "每次新 WS 握手都触发 Chrome \"Allow remote debugging?\" 弹窗",
      "needs_user_action": "first time: tick chrome://inspect/#remote-debugging checkbox then accept Allow popup",
      "ux_cost": "popup-per-ws+banner"
    },
    {
      "name": "env",
      "available": false,
      "ws_url": null,
      "detail": "BD_CDP_WS not set, BD_CDP_URL not set",
      "ux_warning": null,
      "needs_user_action": null,
      "ux_cost": "none"
    },
    {
      "name": "extension",
      "available": false,
      "ws_url": null,
      "detail": "not implemented (v0.4)",
      "ux_warning": null,
      "needs_user_action": "install browser-daemon Chrome extension (planned v0.4)",
      "ux_cost": "extension-permission"
    }
  ]
}
```

**契约约束**：

- `schema_version=1` 在 v0.x 内永远不变。break 必须 bump major。
- `ux_cost` 受限枚举：`"none" | "banner" | "popup-per-ws+banner" | "extension-permission"`。Skill 按枚举排序，安装 UX 用。
- 所有字段在所有 backend 上都必须出现，即使值为 `null`——避免 Skill 做存在性检查。
- 顺序无意义（Skill 用 `recommended` 字段，不依赖列表顺序）。

### 5.3 `browser-daemon list-backends`

短形式查询，doctor 的轻量子集。`--json` 输出仅 `{schema_version, backends: [{name, kind, recommended_mode, ux_cost, needs_user_action}]}`，**不**做 probe（即便 `--probe-ws` 也不接受——查可用性走 doctor）。

### 5.4 `browser-daemon active-tab`（v0.1 CLI）

Mode A 没有持久 RPC channel，Skill 每次想知道"当前活跃 tab"就 subprocess 调一次。这是 H8 / US1 在 v0.1 的实现路径。

**调用**

```
browser-daemon active-tab [--backend NAME] [--timeout SEC] [--json]
```

**stdout 行为**

- 非 `--json`：单行 `<targetId>\t<url>\t<title>\t<accuracy>\n`，tab 分隔。无 active tab 时输出空行 + exit code 2。
- `--json`：
  ```json
  {"schema_version":1,"targetId":"...","url":"...","title":"...","accuracy":"heuristic-recent-activate","since_seconds":12.3}
  ```
  无 active tab 时输出 `{"schema_version":1,"targetId":null,"accuracy":"unknown","since_seconds":null}`。

**实现说明**：Mode A 每次调用都要 spawn 一个新 ws + `Target.getTargets` + 内部 last-activated 启发表。这条命令**会**触发 autoconnect popup（每次新 ws），所以 Skill 在 autoconnect 路径下应当**通过 repl daemon 的长连 ws 走**（详见 `browser-skill/design.md` §A.5），不依赖此 CLI。CLI 是 fallback。Mode B v0.2 后此功能走 `BrowserDaemon.getActiveTab` RPC。

### 5.5 `browser-daemon launch-chrome`（v0.1，H9）

为 Skill install wizard 服务的 Chrome 启动 helper。Skill 选"隔离 profile"路径时调一次，把"用户手动敲一长串 `chrome --remote-debugging-port=N --user-data-dir=...`" 替换为 subprocess 调用。

**调用**

```
browser-daemon launch-chrome [--profile NAME] [--persistent | --tmp] [--chrome-binary PATH]
                              [--port N] [--detach] [--json]
```

**行为**

1. 查 Chrome binary：`--chrome-binary` > `BD_CHROME_BINARY` env > `which google-chrome` / `which chromium` > 平台默认路径列表（macOS `/Applications/Google Chrome.app/...`、Linux `/usr/bin/google-chrome` 等，沿用 browser-harness 的平台路径侦测）。
2. 分配 user-data-dir：
   - `--persistent`（默认）：`${XDG_CACHE_HOME:-~/.cache}/browser-daemon/profiles/<NAME>/`，跨 launch 复用。NAME 默认 `isolated`，校验同 `BD_NAME`（`^[A-Za-z0-9_-]{1,64}$`）。
   - `--tmp`：`mktemp -d`，进程退出时**不**自动清理（避免 race）；用户手动清。
3. 选端口：`--port N` 或默认 `--remote-debugging-port=0`（OS 选）。
4. spawn Chrome **detached**（POSIX `start_new_session=True`，Windows `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`）。沿用 browser-harness `_ipc.py:68-76` 的 `spawn_kwargs()` 模式。
5. 轮询 `<user-data-dir>/DevToolsActivePort` 直到出现（默认 timeout 30s）。
6. 读 port + ws path，输出 ws URL（同 `url` 子命令 stdout 格式：单行 ws URL 或 `--json` 完整对象）。
7. 写 pid 到 `${XDG_RUNTIME_DIR:-/tmp}/browser-daemon-chrome-${NAME}.pid`，供 Skill 后续可选 `kill <pid>`。
8. exit 0。Chrome 继续在后台跑。

**`--detach`** 默认 false：subprocess 调用者得到 ws URL 后 daemon 进程 exit，Chrome detached 继续跑。`--detach=false`（即 foreground）会让 `launch-chrome` 持有 Chrome 子进程并 stdout 流，调试时用。

**Exit codes**：复用 §5.1 的（0/1/2/3）。新增 6 = Chrome binary 找不到。

**用法举例**

```bash
# Skill install wizard 内部：
$ ws=$(browser-daemon launch-chrome --profile isolated --persistent)
$ echo "BD_CDP_WS=$ws" >> ~/.browser-skill/env
# 之后 browser-skill repl 启动时读 env，零摩擦。

# 用户后续清理：
$ kill $(cat /tmp/browser-daemon-chrome-isolated.pid)
$ rm -rf ~/.cache/browser-daemon/profiles/isolated/
```

**约束**：
- **不**自动 attach 到这个 Chrome 实例做任何 CDP 操作——只是启动 + 输出 ws URL，daemon 不持有连接。
- **不**接受自定义 Chrome flag（如 `--no-sandbox`、`--lang=zh-CN`）—— 这是用户启动浏览器的偏好，应该走 Chrome 自身配置或用户自己 wrap script。daemon 只保证"启动 + 暴露 CDP"两件事。
- Chrome 退出后 DevToolsActivePort 文件被 Chrome 自身清理；daemon 不维护这个状态。

### 5.6 `browser-daemon version`

输出形如 `browser-daemon 0.1.0\n`，无别的。

---

## 6. Mode B 协议合同（v0.2+）

> ★ Skill 在 v0.2 开始使用。**对 Skill 完全 CDP-compatible**——任意标准 CDP 客户端（cdp-use / pyppeteer / 手卷）零改动直接连。

### 6.1 Endpoint 发现

```
browser-daemon url --mode-b-proxy
```

输出**裸 socket path**（POSIX）或 `host:port + token`（Windows），不包装成 URL scheme。这与 Skill `ModeBClient.connect_unix(path)` API 风格对齐。

POSIX 输出：
```
/run/user/1000/browser-daemon-default.sock
```
（路径由 `XDG_RUNTIME_DIR` / fallback `/tmp` + `BD_NAME` 决定，详见 §6.7）

Windows 输出（默认 stdout 单行）：
```
127.0.0.1:8541 token=4f3c9a...
```

`--json` 输出：
```json
{"schema_version":1,"transport":"unix","path":"/run/user/1000/browser-daemon-default.sock","name":"default"}
{"schema_version":1,"transport":"tcp","host":"127.0.0.1","port":8541,"token":"4f3c9a...","name":"default"}
```

### 6.2 鉴权

- **POSIX**：socket file 权限 0600。daemon 启动时用 `umask(0o077)` 包住 `bind()`，避免 TOCTOU。**沿用 browser-harness `src/browser_harness/_ipc.py:166-170`**——该模式经过线上验证。
- **Windows**：TCP loopback 127.0.0.1:N。`token` 32 字节十六进制由 `secrets.token_hex(32)` 生成，daemon 启动时写入 `%TEMP%/browser-daemon-${BD_NAME}.port` JSON 文件，atomic write（写 `.tmp` 然后 `os.replace`）。客户端必须在 ws upgrade 的 query string 带 `?token=<hex>`，daemon 端比对。

### 6.2.1 Client handshake query string

WebSocket upgrade URL 支持可选 query：

| key | 必需 | 含义 |
|---|---|---|
| `token` | Windows 必需 | 鉴权 token，匹配 port file 中的值 |
| `client` | 否，强烈推荐 | client label，出现在 `browser-daemon status` 列表 + daemon 日志。规定值：v0.2 实际只可能是 `skill-repl`（只一个 client 能连）；v0.3 多 client 后才会出现 `skill-task` 等其它值。任意 ASCII 都接受 |

例：
```
ws+unix:///run/user/1000/browser-daemon-default.sock?client=skill-repl
ws://127.0.0.1:8541?token=4f3c9a...&client=skill-task
```

未列出的 query key（如曾经计划的 `?intent=`）**被 daemon 静默忽略**——不报错，但也不生效。v0.x 内 query 字段集 frozen，将来加新 key 通过 capability negotiation 而非 query。

### 6.3 协议：标准 CDP

WebSocket text frames，每帧一个 JSON 对象，**外观与直连 Chrome browser-level WS 完全一致**：

- Request：`{"id":N,"method":"X.y","params":{...},"sessionId":"<opt>"}`
- Response：`{"id":N,"result":{...}}` 或 `{"id":N,"error":{...}}`
- Event：`{"method":"X.y","params":{...},"sessionId":"<opt>"}`

daemon 对绝大多数命令做 transparent proxy。具体规则：

| 命令类别 | daemon 行为 |
|---|---|
| `Browser.*` | 直接转发到 upstream |
| `Target.getTargets` | 转发，返回完整 target 列表（不隐藏） |
| `Target.createTarget` / `closeTarget` / `activateTarget` | 转发；`activateTarget` 同时更新 daemon 内部 last-activated 表（用于 `BrowserDaemon.getActiveTab`） |
| `Target.attachToTarget` | 转发；sessionId 直接 passthrough（v0.2 单 client 无冲突）；v0.3 加 sessionId 翻译表 + 单 attacher 规则 |
| `Target.setDiscoverTargets` / `setAutoAttach` | 转发；event 透传到 client（**这是 Skill US2 拿更宽事件流的标准 CDP 路径，daemon 零定制**） |
| 带 sessionId 的命令（Page/DOM/Runtime/Network/Input/...） | 转发；sessionId v0.2 透传，v0.3 翻译 |
| `BrowserDaemon.*`（非标准） | daemon 自己应答，不转发 upstream（见 §6.4） |

### 6.4 `BrowserDaemon.*` 命名空间

daemon-only 概念走私有 CDP 命名空间，不污染标准 CDP 语义。**Skill 是唯一消费方**。

**方法**

| method | params | result | 用途 |
|---|---|---|---|
| `BrowserDaemon.getActiveTab` ★ | `{}` | `{targetId, url, title, accuracy, since_seconds}` | 用户当前看的 tab。`accuracy` ∈ `{"exact","heuristic-recent-activate","stale","unknown"}`，告诉 Skill 这个答案值多少。详见 §6.4.1。 |
| `BrowserDaemon.getBackendInfo` | `{}` | `{name, kind, ux_warnings: [...], schema_version}` | Skill 用作 UI 文案（不要 case on `name` 做业务逻辑） |
| `BrowserDaemon.disconnect` | `{reason?: str}` | `{ok: true}` | 显式关闭 upstream ws，让 §2 横幅消失。Skill 在 repl idle 时调用（详见 §6.5） |
| `BrowserDaemon.version` | `{}` | `{browser_daemon_version: str, schema_version: int}` | daemon 进程的版本字符串 + Mode B 协议 schema 版本。Skill 在 REPL 启动时探 daemon 兼容性（v0.5 起，REVIEW.md F-6 文档化）|
| `BrowserDaemon.stats` | `{}` | `Metrics.snapshot()` 字典 — 详见 v0.5 observability section | 实时计数器（client_connected_total / upstream_open_succeeded_total / proxy_pre_open_overflow_total 等）+ uptime_seconds。供 `browser-daemon stats` CLI 子命令 + 外部监控走（v0.5 ship） |

**事件**

| event | params | 触发时机 |
|---|---|---|
| `BrowserDaemon.upstreamConnecting` | `{backend, hint?: str}` | upstream ws 即将握手（autoconnect 弹窗冒出来的瞬间） |
| `BrowserDaemon.upstreamReady` | `{ws_url, isolated_profile?: bool}` | upstream ws OPEN 之后 |
| `BrowserDaemon.upstreamClosed` | `{reason}` | upstream 被关闭，daemon 在此 event 之后立即关 client ws with 1011。`reason` ∈ `{"chrome_exit","backend_lost","idle_close","daemon_shutdown","skill_disconnect"}` |

**保留并补全**（skill-architect 在 H8 / S1 确认要做）：

| method | params | result | 用途 |
|---|---|---|---|
| `BrowserDaemon.subscribeFocus` | `{}` | `{ok: true}` | 之后此 client 收到 `BrowserDaemon.activeTabChanged` 推送 |
| `BrowserDaemon.unsubscribeFocus` | `{}` | `{ok: true}` | 停止订阅 |
| `BrowserDaemon.uiState` | `{}` | `{ws_count: int, last_popup_resolved_at: float\|null, banner_visible_estimated: bool, client_count: int}` | repl daemon 重连后查 ws_count + popup 历史，决定是否要重复提示用户。**v0.3 起加了 `client_count`**：spec v1 草稿只列前 3 keys，v0.3 多 client 实装顺手加了 `client_count` 供 doctor 渲染"几个 skill 进程在用同一个 daemon"，REVIEW.md F-6 把这条补回 spec |

**补一条事件**：

| event | params | 触发时机 |
|---|---|---|
| `BrowserDaemon.activeTabChanged` | `{targetId, url, title, accuracy, reason}` | 仅订阅了 subscribeFocus 的 client 收到。`reason` ∈ `{"activated","navigated","closed"}` |

**subscribeFocus 的真实价值**：不是"多 client 协调"，是 **user-attention awareness**。US1 user model："agent 帮我填这个表单"——用户在 form 上输了一半切去检查邮件，agent 不能傻乎乎继续在原 tab 上操作。subscribeFocus 让 Skill REPL 立刻知道"用户视线已经走了"，agent 可以暂停 / 询问 / 自动 fallback。即使**单 client** 也需要这个 event（"我"和"我跟踪的 user attention"是两个概念，subscribeFocus 是后者）。

实现最小：daemon 已经为 `getActiveTab` 维护了 last-activated 表（§6.4.1 选项 A），订阅 = 在该状态变化时给订阅了的 client 推一份。零额外成本。

**砍掉的事**（明确不做，避免读 design-review.md 误导）：

- ❌ `BrowserDaemon.setLabel` —— `?client=` query 静态 label 够用。
- ❌ `BrowserDaemon.listClients` RPC —— `browser-daemon status` 子命令足够。
- ❌ `BrowserDaemon.tapCommandLog` —— overreach，Skill 自己记 history。
- ❌ `?intent=repl|task|oneshot` query —— 用 `?client=` 推断。

#### 6.4.1 getActiveTab 准确性等级

skill-architect D-1 已拍板 v0.1 用选项 A（heuristic-recent-activate），**不**做 visibility-poll。理由：US1 "帮我填这个表单" user model 是"用户刚刚操作过浏览器，agent 来接手"——通常用户**刚切到目标 tab**，"最近一次 activate" 启发式天然吻合。选项 B 的 O(N) 隐式 attach 副作用与"daemon 不做应用层"哲学冲突。

**v0.1 (Mode A 仅 CLI 子命令 `browser-daemon active-tab --json`)**

跑 heuristic：daemon 监听 `Target.targetInfoChanged` event 维护 `last_activated_at: dict[targetId, timestamp]`；任何路径 (Skill 调 `Target.activateTarget`、daemon 自己显式 activate) 的 activate 都更新这张表；查询时返回 `max(last_activated_at)` 且仍存活的 real page。

返回的 `accuracy="heuristic-recent-activate"`。**已知限制**（Skill 端要 documented）：用户在 Chrome UI 手动点 tab 切换**不触发** `Target.activateTarget` event，daemon 看不见。所以"用户视线焦点"和 daemon 返回值的偏离上界 = 用户最近一次 CDP-programmatic activate 距今的时间。

`since_seconds` 字段告诉 Skill 这个答案有多旧；Skill 用它决定信任度（例如 > 60s 就当 stale）。

**v0.4 (extension backend)**

走 `chrome.tabs.query({active: true, lastFocusedWindow: true})`。返回 `accuracy="exact"`，零成本。

**未实装的 B 选项**（visibilityState polling）：

如果 Skill US1 必须 exact 且不能等 v0.4，再考虑：对每个 page target 跑 `Runtime.evaluate("document.visibilityState")`，取 `"visible"` 那一个。代价：(a) O(N) CDP roundtrip 100-300ms@N=20；(b) daemon 必须隐式 attach 所有 page target（不增加横幅，但增加 CDP 流量）；(c) minimized window 时全 hidden 没答案。**等 Skill 拍板再决定**。

### 6.5 Upstream 关闭礼仪

daemon 检测到 upstream 关闭时（Chrome 退出 / `Inspector.detached` event / upstream ws read fail / 显式 `disconnect` 子命令调用 / idle policy 触发），按顺序：

1. 对该 client 持有的每个 sessionId 发标准 CDP event：`{"method":"Target.detachedFromTarget","params":{"sessionId":"<...>","targetId":"<...>"}}`
2. 发 `{"method":"BrowserDaemon.upstreamClosed","params":{"reason":"<...>"}}`
3. WebSocket close frame with code **1011**（server error） + reason "upstream closed"

Skill 端响应（已在 `browser-skill/design.md` §D.3 落地）：

- 抛 `DaemonUnavailable` → 单次 retry：重新发起 socket connect。
- 第二次失败 → 抛给 agent，由 agent 决定是 `browser-skill doctor` 还是问 user 起 Chrome。

**daemon 关键不变量**：**daemon 不自动重连 upstream**。重连权在 Skill 手里——Skill 重连 socket 时 daemon 才 lazy 重开 upstream。这条对 autoconnect 路径尤其重要：autoconnect 重连 = 弹窗，daemon 在用户没操作时自动重连 = 凭空冒弹窗，**禁止**。

### 6.6 显式 `disconnect` 子命令

Skill repl daemon 在自己 idle 时调用 `browser-daemon disconnect`，让 §2 横幅消失，但**不**杀 daemon 进程。下次 Skill 操作时 socket 仍 alive，daemon lazy 重开 upstream。

CLI 形式：
```
browser-daemon disconnect [--name NAME]
```

也可通过 socket 内 `BrowserDaemon.disconnect` RPC 调用（同效）。两者等价。

两层 idle 是**互补的**：
- Daemon idle = 防止 Skill 崩溃 / 忘了 disconnect 时的兜底（默认 `never`，可配 `5m` 之类）。
- Skill idle = 主动隐私决策（daemon 不知道 Skill "用户用完了"，必须 Skill 喊）。

### 6.7 多实例 / BD_NAME

通过 `BD_NAME` 环境变量隔离多个 daemon 实例。每个 NAME 对应：

- POSIX socket：`${XDG_RUNTIME_DIR:-/tmp}/browser-daemon-${NAME}.sock`
- POSIX log：`${TMPDIR:-/tmp}/browser-daemon-${NAME}.log`
- POSIX pid：`${XDG_RUNTIME_DIR:-/tmp}/browser-daemon-${NAME}.pid`
- Windows port file：`%TEMP%\browser-daemon-${NAME}.port`

**NAME 校验**：必须匹配 `^[A-Za-z0-9_-]{1,64}$`（path-traversal 防护，**沿用 browser-harness `_ipc.py:31-33`**）。

**stale 检测**：`browser-daemon serve` 启动前先 ping 已有 socket。`ping` 不是裸 connect，是发一个 `{"id":1,"method":"Browser.getVersion"}` 看回包——任何回 `result` 的就是活 daemon，回别的 / 没回 / 连接 refuse 都算 stale，清掉 socket file + pid file 重 bind。**沿用 browser-harness 的 ping handshake 反 port reuse 模式（`_ipc.py:105-123`）**。

---

## 7. v0.1 / v0.2 / v0.3 路线图

skill-architect D-2 拍板：**Mode B 不前推 v0.1**。autoconnect 用户走"一次 `repl start`"长驻形态规避反复弹窗。

### v0.1

**Daemon 范围**：

- Mode A 全部：`browser-daemon url` / `doctor` / `list-backends` / `version`。
- Backend：`env`（含 `BU_*` compat alias）/ `rdp`（含 Chrome 136/147+ 404 fallback）/ `autoconnect`。`extension` doctor 占位 "not implemented (v0.4)"。
- CLI 子命令 `active-tab --json`（H8 / US1，v0.1 Mode A 路径）。
- CLI 子命令 `launch-chrome`（H9，install wizard 用）。
- doctor JSON schema_version=1 锁定（H2、H3）。
- 平台 profile 表整张搬 browser-harness（macOS / Linux / Linux Flatpak / Windows × 多 Chromium 系浏览器）。

**对应 Skill 范围**（来自 `browser-skill/design.md` §10 v0.1）：

- REPL 三形态（inline heredoc / `repl start/stop/exec` 长驻 / `task` 直调）
- `daemon_client.py` Mode A 实现 + 失败 retry 一次
- memory（global / site / repl）
- bundled site-skills 起步集
- install wizard 调 `launch-chrome` 给"隔离 profile"路径，调 `doctor --json` 给 backend 选择面板

### v0.2

**Daemon 范围**：

- Mode B 上线：socket listen + 标准 CDP transparent proxy。
- `BrowserDaemon.*` 命名空间：`getActiveTab` / `getBackendInfo` / `disconnect` / `uiState` / `subscribeFocus` + 事件 `upstreamConnecting` / `upstreamReady` / `upstreamClosed` / `activeTabChanged`。
- 单 skill-repl client connection（H10、§3.4）。第二个 ws upgrade 直接 reject + HTTP 503。Skill 内部自己 multiplex repl + task。
- Upstream 关闭礼仪（H6 / §6.5）。
- idle policy（默认 `never`，可配；`browser-daemon disconnect` 子命令暴露给 Skill）。
- `extension` 仍占位。

**对应 Skill 范围**（v0.2）：

- Mode B 客户端 + `auto` 切换（先试 socket，没起 fallback Mode A）
- inline heredoc 在 Mode B 下变首选，文档去掉"反复弹窗"警告
- 其它 v0.2 Skill 项（selftest cache、OUTPUT_SCHEMA、project-level site-skills、memory forget/replace、cross-site evaluation 升级）daemon 无关。

### v0.3

**Daemon 范围**：

- **Multi-client connections**：daemon 接受 N 个 client connect 到同一 socket（Skill 进程外的别的 client，如 Layer 3 task scheduler 进程）。
- **单 target 单 attacher 规则**：同一 upstream targetId 同一时刻只一个 client 能 `Target.attachToTarget`，第二个返回 CDP error `-32602`。
- **sessionId 翻译表**：每 client 独立 sessionId 名空间；daemon 维护 (client_id, client_local_session) ↔ upstream_session 映射，事件按反查表路由到 owner client。
- **Event fanout 仲裁**：session-scoped event 路由 owner，browser-level event (无 sessionId) 广播给所有 client。
- **可选 opt-in "shared read"**：`Target.attachToTarget` 带 `flags.allowSecondaryReadOnly=true` 拿只读 sessionId（只收事件不发命令）。
- Layer 3 task 编排支持的相关 RPC（如有）。

**对应 Skill 范围**（v0.3）：

- 与 Layer 3 集成（多 task 并发、subscription 拉别人的 site-skills、selftest cron）

### v0.4

**Daemon 范围**：

- `extension` backend 真实装：本地 relay server + Chrome 扩展协议、`Target.getTargets` 用 ghost target 模拟、单 tab attach 模型、anti-CSRF verifyClient。
- `BrowserDaemon.getActiveTab` 在 extension backend 下 accuracy 升 `"exact"`（走 `chrome.tabs.query`）。

**对应 Skill 范围**：

- extension backend UI 集成（install wizard 加 "browser extension" 选项）。

### v0.5

- `cloud` backend（Browser Use 等远程 Chrome 服务）。
- observability / metrics / structured logging。
- v0.x 内打磨 doctor 报告质量、performance tuning。

---

## 8. Backend 实现细节

### 8.1 env

- `BD_CDP_WS` → 直接返回（不校验内容，trust env）。
- 否则 `BD_CDP_URL` → HTTP GET `{URL}/json/version` → 读 `webSocketDebuggerUrl` 字段。
- 都没有 → resolve 返回不可用。
- compat：`BU_CDP_WS` / `BU_CDP_URL` 作为 alias 读，doctor 输出"deprecated, please use BD_*"。

#### 8.1.1 远端 / 云端 ws 鉴权范围（v0.1）

`env` backend 是云端浏览器（Browser Use / Browserless / Hyperbrowser 等）在 v0.1 的接入路径。**鉴权能力受限于"URL 内嵌"**：

| 鉴权形式 | v0.1 状态 | 备注 |
|---|---|---|
| `wss://host/path?api_key=...` / `?token=...`（URL-embedded） | ✅ 支持 | daemon transparent forward。Mode A：原样 stdout 给 Skill；Mode B：daemon 自连 upstream 时把 URL 包含 query 整段 passthrough |
| `wss://host:port/sub-path/...` 无 token（TLS-only） | ✅ 支持 | 同上，纯连接 |
| Basic auth in URL（`wss://user:pass@host/...`） | ✅ 支持 | URL RFC 范围，daemon 不剥不改 |
| HTTP Header 鉴权（`Authorization: Bearer ...` / `X-API-Key: ...`） | ❌ 不支持 | Mode B daemon 自持 upstream ws 时没有补 header 的机制；Mode A 透传 URL 时 Skill 端可自加 header，但 daemon 不知道、也不该知道 |
| mTLS client cert | ❌ 不支持 | 同上，daemon 没有 cert 管理 |

需要 header 鉴权 / mTLS / OAuth flow 的云服务等 **v0.5 `cloud` backend 专用支持**——届时 daemon 内置 auth provider 抽象（per-backend 注入 header / refresh token / cert），但这是 v0.5 范围。v0.1 用户走 `env` + URL-embedded token，绝大多数 cloud 厂商都接受这条路径。

**Skill 端 install wizard 文案应当明确**：选"云端浏览器"时只支持 URL-embedded auth；如果用户的云服务必须 header，提示等 v0.5 / fallback 用 wrapper proxy 自己注入 header。

### 8.2 rdp

读 config `backends.rdp.port`（默认 9222），HTTP GET `http://127.0.0.1:<port>/json/version`，返回 `webSocketDebuggerUrl`。

**Chrome 136/147+ default-profile 404 fallback**——必做。Chrome 在 default user-data-dir 上启用 `--remote-debugging-port` 时，从 136 起对 `/json/version` 越来越严格，到 147 直接 404；但 `DevToolsActivePort` 文件里第二行 ws path 仍可用。fallback 流程：

```python
async def resolve(self, timeout):
    try:
        resp = await http_get(f"http://127.0.0.1:{port}/json/version", timeout=1)
        return resp["webSocketDebuggerUrl"]
    except HTTPError as e:
        if e.code != 404:
            raise
        # Chrome 147+ default-profile lock: HTTP discovery disabled,
        # but DevToolsActivePort still has the ws path.
        for profile_path in PROFILES:
            active_port_file = profile_path / "DevToolsActivePort"
            if not active_port_file.exists():
                continue
            lines = active_port_file.read_text().splitlines()
            if lines and lines[0].strip() == str(port) and len(lines) > 1:
                return f"ws://127.0.0.1:{port}{lines[1].strip()}"
        raise
```

**沿用 browser-harness `daemon.py:83-101` 的 `_ws_from_devtools_active_port` 实现**——已经处理过 IPv6 host bracket、stale DevToolsActivePort 等边角。

`isolated_profile` 判定：比较 `--user-data-dir` 是否 = 平台默认 Chrome profile 路径。如果命令行不可见（CDP 不暴露），fallback 比较 `/json/version` 里的 `Browser` 字段 + 是否有 user-facing tab heuristic；MVP 不做精确判定，`extras.isolated_profile = null` 表示未知。

### 8.3 autoconnect

扫描 Chrome user-data-dir 找 `DevToolsActivePort`。profile 路径列表：

**沿用 browser-harness `daemon.py:36-65` 的全表**——覆盖 macOS / Linux / Linux Flatpak / Windows × Chrome (Stable/Canary) / Chromium / Edge (Stable/Beta/Dev/Canary) / Brave / Arc / Dia / Comet。这个表是吃过 bug 才完整的，重新发明 = 重新踩坑。

多个 profile 都有 `DevToolsActivePort`：取 mtime 最新的。doctor 在 `-v` 下提示存在多个。

doctor 行为（§5.2 已说）：`probe` 仅检查文件存在 + 可读，**不**握手 ws。`resolve` 也不预热 ws——只把 URL 出来给 Skill，弹窗在 Skill 第一次 `ws.connect()` 时发生。这是 §2.3 物理约束（握手 ↔ 横幅同步）的直接产物：daemon 任何时候真握手都会让用户看到副作用，所以必须把"何时握手"权交给 Skill。

### 8.4 extension（v0.4 占位）

v0.1 doctor 输出 `available: false, detail: "not implemented (v0.4)"`。v0.4 实装时形状：

- 默认 relay endpoint：`ws://127.0.0.1:19989`（比 playwriter 的 19988 高一位，默认共存不冲突）。
- 扩展握手：扩展连过来时发 `{"type":"hello","installId":"...","browser":"chrome","version":"..."}`。
- 用户手动 attach 模型：扩展不自动 attach 所有 tab，用户点扩展图标 → 扩展通过 `chrome.debugger.attach` → 通知 daemon。
- daemon 把 attach 过的 tab 当成 ghost target 在 `Target.getTargets` 里返回。
- daemon 调 `Target.attachToTarget` 时通过扩展的 `chrome.debugger.sendCommand` 实现。
- 不支持的 browser-level 命令（如 `Browser.crash`）返回 `{code: -32601, message: "method not implemented in extension backend"}`。

**借鉴**：OpenCLI `OpenCLI/src/daemon.ts:194-455` 的 anti-CSRF `verifyClient`（拒非空 `Origin` 防止恶意网页 drive-by）、playwriter `cdp-relay.ts:39-64` 的 target 黑白名单过滤、OpenCLI `extension/src/cdp.ts:96-150` 的 3-retry chrome.debugger 冲突处理。

### 8.5 Mode B daemon 进程模型

daemon 进程结构：

```
serve() {
  load config
  bind socket (umask 0o077 for POSIX 0600)
  spawn:
    - listener task (accept clients)
    - upstream lifecycle task (lazy open / close / idle timer)
    - keepalive task (downstream ping/pong + upstream Browser.getVersion heartbeat)
  wait stop signal
  graceful shutdown: send upstreamClosed events, close clients with 1011, close upstream ws
}
```

**集中状态**：所有 mutable state（clients map, upstream state, sessionId 翻译表, last_activated_at）放在一个 dataclass，每次状态变更走一个集中的 `apply(transition)` 函数，副作用走 subscribe pattern。**借鉴 playwriter `relay-state.ts` + `docs/plan-centralize-relay-state.md`**：状态可单元测，副作用集中。Python 版无需 zustand，手写 immutable dataclass + observer 即可。

**反模式**（明确不做）：

- 没有"daemon 重启 chrome"逻辑——chrome 死了告诉 client 就行。
- 没有 retry framework / session manager / config-system 膨胀。一个 toml 文件 + env 覆盖即可。
- 没有插件系统 / hook 机制。backend 是 Python class 注册，不是动态加载。

---

## 9. 测试策略

### 9.1 Mode A 单测

- `env`：mock env 变量，mock HTTP server 回 `/json/version`。断言 stdout 单行 URL 等于预期；断言 exit code。
- `rdp`：aiohttp fake server 跑 200 + 404 两条路径。404 fallback 路径 mock `DevToolsActivePort` 文件，断言读到正确 ws URL。
- `autoconnect`：mock 文件系统包含多个 profile，断言取 mtime 最新；断言 doctor 默认**不**握手 ws（assert 没有 ws connect 调用）。
- `resolver`：mock backend，测 fallback 顺序、显式 `--backend`、聚合错误。
- CLI：subprocess 调用真实 binary，断言 exit code + stdout/stderr 精确字节。
- **doctor JSON schema 测试**：固化 schema_version=1 形状，加 schema validator（jsonschema 或手写），确保所有字段在所有 backend 上都出现，未来 break 跑 CI 红。

### 9.2 Mode B 单测

- 状态机：单测 DISCONNECTED → CONNECTING → CONNECTED → CLOSING → DISCONNECTED 转换；超时 / fail 路径。
- 单 client connection enforcement（v0.2）：第一个 ws 连上后，第二个 ws upgrade 收到 HTTP 503 / ws close 1013。
- sessionId pass-through（v0.2）：client 发的 sessionId 不被 daemon 改写，response/event 原样回。
- sessionId 翻译表（v0.3）：单测 attach / detach 映射 + per-client 名空间隔离。
- 事件 fanout（v0.3）：mock 上游 event，断言 session-scoped 路由给 owner client、无 sessionId 的 browser-level event 广播。
- `BrowserDaemon.*` namespace：mock client 发各 method，断言 daemon 自答（不转发 upstream）。
- IPC socket safety：bind 后断言文件权限 0o600；ping handshake stale 检测正确。

### 9.3 集成测试（关键的几条）

- **autoconnect Allow timeout**：fake CDP server 模拟"用户没点 Allow"。断言 CONNECTING 进 DEGRADED 或 DISCONNECTED，pending client 收到正确 error code。
- **upstream Chrome 中途退出**：mock CDP server 在第 N 条命令后关 ws。断言 daemon 对每个 client 发 `Target.detachedFromTarget` + `BrowserDaemon.upstreamClosed` + close 1011。Skill 重连测试一并跑通。
- **v0.2 第二个 client 连接被 reject**：第一个 ws 连上之后，第二个 ws upgrade 应该收到 HTTP 503（或 ws close 1013），第一个不受影响。
- **v0.3 多 client 同 target attach**：两个 client 都 `Target.attachToTarget(X)`。断言第二个收到 `error.code=-32602`、第一个不受影响、第一个的 sessionId 仍正常发命令。
- **v0.3 多 client 事件隔离**：client A 在 target X 上 attach、client B 在 target Y 上 attach。断言 X 上发的 `Network.*` 事件**只**给 A，Y 上的**只**给 B。browser-level `Target.targetCreated` 广播给两者。
- **v0.3 opt-in shared read**：第二个 attach 同 target 拿只读 sessionId，只收事件不发命令。
- **真实 headless Chrome**：可选，CI 跑一次 smoke test 确认协议端到端。

### 9.4 反测试（确认不发生）

- doctor 默认调用 zero ws connect。**测试断言**：所有 backend 的 doctor 跑完 → upstream 端无 ws 连接被打开。
- daemon idle close 时**不**发起 upstream 重连。
- chrome 退出时 daemon **不**自动重启 chrome。

### 9.5 兼容性 case（验证 §4.4 全景覆盖）

- **指纹浏览器风格 rdp（动态 port）**：mock 一个 AdsPower / MultiLogin 风格的 fake server，端口非 9222（如 51789），`/json/version` 返回标准 shape。用 `browser-daemon url --backend rdp --port 51789` 调用，断言 stdout ws URL 正确。覆盖"port 不是 9222 默认值 + 厂商 UA 字符串"组合。
- **云端 wss + URL token 透传**：mock `wss://example.com/cdp?api_key=secret` 风格的 fake upstream。设 `BD_CDP_WS=wss://example.com/cdp?api_key=secret`，断言：
  - Mode A：`browser-daemon url` stdout 输出**一字不差**的原始 URL（query 不被剥、不被改）；
  - Mode B：daemon 自连 upstream 时 URL 整段 passthrough（截 ws 握手 SNI / Host header 看是否含原始 query）。
- **`BU_*` compat alias**：设 `BU_CDP_WS=ws://...`（无 BD_CDP_WS），断言 daemon 读到并 doctor 输出 deprecation hint。
- **`launch-chrome` 端到端**：mock Chrome binary 路径，断言 spawn detached + 轮询 DevToolsActivePort + 输出 ws URL + 写 pid 文件，整链路通过。

---

## 10. 开放问题

已经被 skill-architect 拍板的不再列。剩余真未决：

- **config schema 校验**：pydantic vs 手写？倾向手写（避免重依赖、保 cold-start 速度）。MVP 决：手写 + 失败时友好 error。
- **多 Chromium 浏览器（Edge / Brave / Arc / Dia）的 doctor 友好提示范围**。实测都遵循 DevToolsActivePort 协议，autoconnect backend 自动支持；doctor 文案是否专门为它们写？MVP 决：不专门写，doctor 显示 "Chrome-family browser detected at `<path>`" 即可。
- **doctor 的 i18n**。MVP 决：punt，纯英文（除部分 ux_warning 用户面字串，由 Skill 层翻译）。
- **`autoconnect` profile auto-select 策略**。多个 profile 都有 `DevToolsActivePort` 时取 mtime 最新——但 mtime 最新不等于用户当前 active profile（Chrome 重启 inactive profile 也更新该文件）。是否要加 active probe（HTTP GET 验证 ws 真能 OPEN）？probe 本身**不**触发握手（HTTP discovery 阶段无副作用，§2 实测），可以做。**决**：v0.1 加这一层 active probe，doctor 输出标 `detail` 哪个 profile 是"真活的"。
- **`launch-chrome` Chrome flag 透传**。当前设计明确**不**透传用户自定义 flag（§5.5 "约束"）。如果 v0.1 用户反馈一定要 `--lang=zh-CN` / `--proxy-server=...` 等怎么办？MVP 决：先不做，等真有人反映；如果加，**白名单**式（不接受 `--no-sandbox` 之类危险 flag）。
- **upstream `Browser.getVersion` 心跳频率**。§6 keepalive 提到 30s 心跳；过频浪费 CDP，过疏检测 Chrome 死亡慢。**决**：30s（v0.2 写死），如果 Skill 反馈太快慢再 config 化。
- **Mode B upstream OPEN 之前的 client 队列长度**。client 在 daemon CONNECTING 状态下连上来，是 buffer 命令等 upstream OPEN，还是直接 reject？**决**：buffer，但有上限 100 条；超出抛标准 CDP error 给 client。

未来才考虑（不在 v0.1-v0.3 范围）：

- 多 backend 同时活跃（同 daemon 实例同时持 rdp + autoconnect 上游）——是 cloud 形态，暂不考虑。
- daemon-as-a-service（远程 daemon，跨机器服务）——v0.5+ 才有意义。
- 自动化 profile 加密 / cookie 隔离 / fingerprint randomization——不在 daemon 范围。

---

## 11. 命名 / 边界

为什么叫 `browser-daemon` 而 v0.1 MVP 不是 daemon？

- 名字预留给 Mode B 真长驻进程。
- v0.1 单独看像 `git config --get`——一次性 resolver。
- Skill 端 / 文档已经用 daemon 这个词，保留命名一致。

**关键边界**（再次强调）：

| Daemon 做 | Daemon 不做 |
|---|---|
| 输出 CDP ws URL（Mode A） | CDP 命令的应用层（截图、点击、DOM、网络拦截）|
| 代理 CDP 流量 + 管理 upstream lifecycle（Mode B） | 自动重连 chrome / 自动 reattach 失效 session |
| 主动断 upstream（Skill 喊 disconnect 时） | 主动重连 upstream（Skill 自己来）|
| backend 发现 / fallback 调度 | LLM 调度 / planning / tool routing（agent / Skill 的事）|
| 单 client connection (v0.2) / multi-client mux (v0.3 含事件路由 + 单 attacher) | 跨 Chrome 实例负载均衡（每个 daemon 服务一个 Chrome）|
| Chrome 138+ default-profile 404 fallback | 启动 Chrome（独立 profile 启动器 `launch-chrome` v0.3+ 例外） |

---

## 附录 A — 参考项目挖矿

四个相关项目在 `/Users/metajs/gitRepos/labs/browser/`。下面是对 daemon 设计有直接影响的代码点，按 "借鉴 / 拒绝" 分类。

### A.1 browser-harness（参考价值最高）

**借鉴**：

- `src/browser_harness/daemon.py:104-160` `get_ws_url()` 是 fallback chain 的事实参考实现。Chrome 147+ default-profile 404 fallback（`:142-147`）必须移植。
- `src/browser_harness/_ipc.py:161-186` AF_UNIX bind 前 `umask(0o077)` 模式——0600 socket file 权限的标准做法。Mode B 直接照搬。
- `_ipc.py:105-123` ping handshake 防 port reuse 后 stale socket file 假阳性。Mode B daemon 启动检测必须这么做。
- `_ipc.py:38-50` `BD_NAME` path-traversal 校验（regex `[A-Za-z0-9_-]{1,64}`）+ 多实例文件命名。Mode B 直接照搬。
- `daemon.py:36-65` 平台 profile 列表全表。autoconnect backend 直接使用。
- `daemon.py:344-356` stale session 自动 re-attach 模式。**不**默认开（Skill 在标准 CDP 协议里没有 daemon 主动 reattach 的预期），但 v0.4 可以以 `?auto_reattach=1` query 字段 opt-in。

**拒绝**：

- browser-harness 的 IPC 不是标准 CDP wire format（自家 `{meta, ...}` JSON 协议）。**browser-daemon Mode B 必须走标准 CDP**——这是 Skill 能用 cdp-use 等任意客户端的前提，也是相比 browser-harness 的核心架构优势。
- browser-harness 的 daemon 内嵌应用层 helper（截图 base64、`drain_events` 累积等）。Mode B daemon **不**做这些；Skill 自己拼。

### A.2 playwriter

**借鉴**：

- `playwriter/src/cdp-relay.ts:71-90` relay 启动端口 19988——extension backend 选 19989 跟它并排避冲突，端口段保持邻近便于识别同类工具。
- `playwriter/src/chrome-discovery.ts:55-89` `probePortStatus` 三态 (`live`/`blocked`/`dead`) + `appendSessionToWsUrl` Chrome 136+ default-profile 锁定 fallback。Mode B 实装直接套用。
- `playwriter/src/relay-state.ts` + `docs/plan-centralize-relay-state.md` 集中状态 + 纯转换 + subscribe 副作用模式。**Mode B 实现推荐这个 pattern**——状态可单测，副作用集中，事件流的"先 setState 再 sendToPlaywright"顺序自然落地。
- `cdp-relay.ts:39-64` restricted-target 过滤（chrome://、devtools://、edge:// 黑名单 + 扩展 URL 按 id 白名单）。`BrowserDaemon.getActiveTab` 复用同套过滤。

**拒绝**：

- `cdp-relay.ts:103-157` stableKey + 多扩展 reconnect 路由。是 multi-extension 场景的优化，v0.3 daemon 不需要（单 Chrome 单 backend）。是 cdp-relay.ts 1846 行复杂度的主要来源之一，**必须避免被这个模式诱惑**。

### A.3 browser-cli

整个 `src/browser.ts` 通过 `@browserbasehq/stagehand` 把 CDP 连接抽象掉了，没有自己的 daemon / discovery 模块。**无直接借鉴**。

**反向证据**：Stagehand SDK 接受 `cdpUrl` 作为唯一入参（`browser.ts:89, 115`），证明 "daemon 输出一个 ws URL，下游全部以此为锚" 是行业惯例。Mode A 方向被独立证实。

### A.4 OpenCLI

**借鉴**：

- `OpenCLI/src/daemon.ts:86-114` 多 profile 路由模型（`extensionProfiles: Map<contextId, ...>`，单 profile auto-select，多 profile 要求显式 `--profile`）。extension backend v0.4 形状参考。
- `OpenCLI/src/daemon.ts:194-240` daemon HTTP+WS server 的 anti-CSRF / verifyClient 模式（拒非空 `Origin` header 阻止恶意网页 drive-by）。**Mode B 必抄**：浏览器恶意网页可以发 `ws://127.0.0.1:port` 请求（CORS 不管 ws upgrade）。
- `OpenCLI/extension/src/cdp.ts:96-150` chrome.debugger 3-retry + "Another debugger is already attached" 处理（与 1Password、Playwright Bridge 等冲突）。extension backend v0.4 必看。

**拒绝**：

- OpenCLI 的 daemon 完全绑死扩展模型——标准 `--remote-debugging-port` / autoconnect 路径它不做。browser-daemon 必须 backend-pluggable，不能让 daemon 内部对 "extension is special" 有任何特殊路径渗透出去——那种渗透是 OpenCLI daemon 不可拆的关键原因。

---

## 附录 B — 拒绝清单（明确不做的事）

为了保持范围克制，下面是任何后续 PR 都应该被拒绝的设计扩张：

- ❌ daemon 内嵌任何 CDP 应用层（截图、点击、DOM、网络拦截、cookie 管理）。
- ❌ daemon 启动 Chrome（`launch-chrome` v0.3+ 子命令只是 helper 不是 daemon 内置行为）。
- ❌ daemon 主动重连 upstream（除非 Skill 显式触发）。
- ❌ daemon 自动 re-attach stale session（除非客户端 `?auto_reattach=1` opt-in）。
- ❌ daemon retry framework / session manager / 任何"管"上游 chrome 状态的抽象。
- ❌ daemon LLM 调度 / planning / tool routing。
- ❌ 跨 Chrome 实例负载均衡 / daemon pool。一台机器一个 BD_NAME 一个 Chrome 一个 daemon。
- ❌ 任何不通过标准 CDP wire format 的 Mode B 协议扩展（除 `BrowserDaemon.*` 命名空间外）。
- ❌ doctor 默认握手 ws（永远 opt-in）。
- ❌ daemon 持久化用户偏好（user 偏好住 Skill 的 global memory，daemon 每次启动从 CLI/env 读）。
- ❌ daemon HTTP API（只有 WebSocket socket，不开 HTTP REST）。

这些拒绝是 §1 "为什么 daemon 存在" 那句话的直接产物——任何超越"提供 CDP ws"的行为都是 scope creep。

---

## 协作 / 接口反馈

- `browser-skill/design.md` §D（Daemon ↔ Skill 边界）已 finalize v1（1620 行）。本文件 §3 用 cross-ref scheme (B)：H 是本地编号，"来自" 列反查 D.2.x。
- 实装阶段每个 PR 必须通过 §9 doctor JSON schema 测试 + §9 反测试（doctor 不开 ws / daemon 不自动重连 / chrome 退出不重启）。

## US1-4 端到端 self-check

每个 US 通过下面这条路径可跑通，daemon 责任清单：

| US | Skill 调用 | Daemon 提供 | 跑通验证 |
|---|---|---|---|
| US1 当前页 one-shot | `browser-daemon active-tab --json` (v0.1) / `BrowserDaemon.getActiveTab` (v0.2) | CLI 子命令 / RPC，accuracy=heuristic | 用户点 form tab → CDP `Target.activateTarget` → daemon last-activated 表更新 → Skill 拿到正确 targetId |
| US2 in-flight wider events | 标准 `Target.setAutoAttach` + `Console.enable` / `Network.enable` | 透明 CDP proxy（v0.2 Mode B），零特殊处理 | Skill 在 attached session 上发 setAutoAttach → 新 target 自动 attach → 事件流到 Skill |
| US3 propose_solidify | （无 daemon 路径） | 零 | Skill 进程层完全自洽，daemon 不参与 |
| US4 backend from memory | Skill 启动时读 `~/.browser-skill/global.md` frontmatter `daemon.preferred_backend` → `browser-daemon url --backend <name>` | `--backend` flag / `BD_BACKEND` env 接受字符串值（H10） | install wizard → user 选 isolated → write memory → next launch 自动用 rdp + isolated profile |

四条路径都在 §5/§6/§8 落点上有实质 spec。design-v2 v1 端到端 self-check ✅。
