# browser-daemon — Design Review

伴随 `design.md` 的 architecture-review。逐节 critique，只写有补充/异议的部分，正文文档已经扎实的地方不复述。

---

## Executive Summary

**扎实**：§2 的 Chrome 144+ 实测表是整个项目的事实基础，结论（autoconnect 必须单长连、横幅 X dismiss per-WS 持久、所以 idle close 仅服务隐私）推得干净。Mode A / Mode B 二分以及 fallback chain 默认不含 autoconnect/extension，方向都对。

**最大 gap**：**Mode B 的 wire 契约缺失**。文档把 Mode B 说成"socket + 标准 CDP 协议（WebSocket text frames + JSON）"就停下了，但接口 surface 至少还差五件事：(a) endpoint 发现协议（socket path / TCP port / token 怎么传），(b) 客户端身份握手（status 输出里那个 label 从哪来），(c) **"用户当前活跃 tab"如何暴露**（design.md §14 列了"多 client 同 attach"开放问题但完全跳过了 active-tab 语义；这是 Skill REPL 唯一一个 daemon 必须帮忙的事），(d) 多 client 隔离策略（命令交错、event fanout），(e) upstream Chrome 死掉时对各 client 的关闭礼仪。这五项不定义，skill-architect 没法照着实现；定义了，v0.2 就有了真正的 backbone。

**单条最高杠杆建议**：把 Mode B 的协议升级为 "**标准 CDP browser-level + `BrowserDaemon.*` 非标准命名空间**" 双层结构。标准 CDP 部分让 Layer 2 可以拿任何现成 CDP 客户端（cdp-use / pyppeteer）连上去；`BrowserDaemon.*` 暴露 daemon-only 概念（active tab、setLabel、upstream lifecycle），不污染 CDP 语义。下面 §Layer 1↔2 接口契约 是给 skill-architect 直接照抄的版本。

---

## §1 目标 / 非目标

补一条非目标：**不做跨 Chrome 实例的负载/多路调度**。如果同一台机器上同时跑两个隔离的自动化 profile，应该让用户起两个 `browser-daemon`（按 `BD_NAME` 隔离 socket/log/pid），而不是一个 daemon 管两套 upstream。这条不写明，未来很容易被需求方推着做"daemon pool"。参考 browser-harness 的做法：`BU_NAME` 一个进程一组文件，互不重叠（`src/browser_harness/daemon.py:31`、`_ipc.py:38-50`）。

---

## §2 Chrome 144+ UX 实测约束

实测表本身保持不动。补两条**实测表没覆盖**但实现时会撞到的事：

### 2.6 Chrome 136+ / 147+ 的 default-profile 锁

实测 `cdp-popup-memory-test.mjs` 跑的是 autoconnect 路径（Allow popup 触发 ws 握手）。**`--remote-debugging-port` 路径** Chrome 136 之后对 *默认* user-data-dir 越来越严格，到 147+ 时 `/json/version` 直接 404（HTTP discovery 失效），但 `DevToolsActivePort` 文件里的第二行 ws path 还能用。browser-harness `daemon.py:142-147` 和 playwriter `chrome-discovery.ts:55-89` 都内嵌了这条 fallback：

> HTTP discovery 失败但状态码不是 ECONNREFUSED → 用 DevToolsActivePort 的 path 直接拼 ws URL；这条路径不触发 Allow 弹窗（因为是 rdp，不是 autoconnect），但需要绕过 HTTP probe。

design.md §9 `rdp` 那段只描述了乐观路径（`/json/version` 成功）。**MVP 必须实现 404 fallback**，否则用户拿默认 profile + `--remote-debugging-port` 启动时 backend 会假阳性"unavailable"。

### 2.7 横幅与 chrome.debugger 路径

§2.5 表格把 `extension` 列为"无弹窗 / 有横幅"。这是 *chrome.debugger* API 走的 extension 权限模型，没有"Allow remote debugging"对话框 —— 但**有自己的授权流程**：用户安装时点 "Allow this extension to debug pages"。这条不是 daemon 要管的（产品决定），但 doctor 输出对 `extension` backend 应该提示一次：

> extension backend 是否可用，取决于用户是否已经在 Chrome 里安装 + 启用了对应扩展，且扩展是否打开了和 daemon 的 relay 连接。daemon 测不到"未安装"，只测得到"relay 不可达"。

doctor 写文案时不要混淆"扩展未装"和"扩展装了但 relay 没起来"。

---

## §3 两种模式

§3 决策矩阵正确。但 §15 v0.1 路线说 "autoconnect 在 mode A 下作为权宜"，跟 §3 矩阵 "autoconnect: B（必需）" 直接矛盾。**修订**：

- v0.1 MVP **可以** 用 mode A + autoconnect，但 doctor 必须打 ux_warning 而且 default fallback chain 不含 autoconnect（§6 已经做了，对的）。
- 用户显式 `--backend autoconnect` 走 mode A：daemon 每次解析就要触发一次 Allow 弹窗，这是已知摩擦，**不是 bug**，doctor 输出里写清楚。
- v0.2 mode B 上线后，doctor 的"下一步建议"对 autoconnect 改为 "推荐 mode B，否则每次调用必弹"。

把这条写进 README 的 backend 表会更清楚。

---

## §4 架构

`backends/` 注册表 + `proxy/` 单独子包是合理的拆法。但**模块布局有一个隐含假设需要明文化**：

> `extension` backend 的 daemon 行为**不同于** `env`/`rdp`/`autoconnect`。后三者的 daemon 只是 "拿到 ws URL → 自己开 ws 长连"；前者的 daemon 是 "**自己当 relay**，转发到浏览器扩展（也可能是另一个本地 relay 进程，如 playwriter 风格 `ws://127.0.0.1:19988`）"。

也就是说 `Backend.resolve()` 返回 `str | None` 这个签名 **不够**：对 extension backend 来说，"返回的 URL" 不是 Chrome 的 browser-level ws，而是 daemon 自己代理后透出的虚拟 ws（或第三方 relay 的虚拟 ws）。建议 `Backend` 接口加一个分类标签：

```python
class BackendKind(Enum):
    UPSTREAM_WS = "upstream_ws"       # env / rdp / autoconnect 都是
    LOCAL_RELAY = "local_relay"        # extension（playwriter / 自家 extension）
    # 留扩展：CLOUD_RELAY 给 v0.4 Browser Use 等远程
```

resolver / proxy 调度时按 kind 走不同分支：UPSTREAM_WS 拿 URL → 自己开 ws；LOCAL_RELAY 拿一个 forwarder handle（或 URL，daemon 自己当中间人，但 idle 策略和事件 fanout 都换算法）。

---

## §5 Backend Protocol

`recommended_mode` 是字符串 `"A" | "B"`，应该改成 Enum 避免字面量散落。次要。

更重要：`resolve()` 同时承担"拿 URL"+"判定可用"两职。doctor 想要的是 cheap probe（不弹窗、不开 ws），resolver 要的是真的开 URL 准备 ws 握手。对 `autoconnect` 这两件事差异很大 —— probe DevToolsActivePort 是文件系统操作，但真握手会弹窗。建议拆：

```python
class Backend(Protocol):
    name: str
    kind: BackendKind
    recommended_mode: Mode

    async def probe(self) -> DoctorResult: ...    # cheap, side-effect-free
    async def resolve(self, timeout: float) -> ResolveResult: ...  # may trigger UI
```

`ResolveResult` 包含 `ws_url` 和（对 LOCAL_RELAY）必要的辅助信息（relay 进程的 pid / 启动命令 / 扩展 id）。`doctor` 子命令只调 `probe`；`url` 子命令调 `resolve`。这样 doctor 永远不会"为了诊断而弹窗"。

---

## §6 Mode A Resolver 逻辑

逻辑没问题。补一条 fallback **顺序内的优先级**：当多个 backend 都可用时，目前文档说 "第一个返回 URL 就用它"。

补：**`rdp` 命中独立 profile**（非默认 user-data-dir）应该比命中默认 profile 优先级高，因为前者完全无横幅干扰。这要求 rdp backend 自报 "I'm on isolated profile" or not。实现：通过 `/json/version` 返回里的 `Browser` 字段 + 调 `/json` 看 target 数量+title 推断，或者更直接 —— 看 `--user-data-dir` 是否等于平台默认。MVP 可以先不做（rdp 默认就是用户配的端口），但接口预留 ResolveResult.extras["isolated_profile"]: bool 给 doctor 用。

聚合错误 `AllBackendsUnavailable` 的 `last_errors` 格式建议写死成 `list[tuple[str, str]]`（name, reason）。CLI 输出时直接逐行打。

---

## §7 Mode B 状态机 + Idle Policy

这是 design.md 最薄的一节。逐子节补：

### 7.1 状态机

漏掉的状态/边：

| Missing 边 | 行为 |
|---|---|
| `CONNECTING` 超时（用户没点 Allow） | 默认 60s 后 → `DISCONNECTED`，告知所有 pending client `{"code": -32000, "message": "user did not authorize"}` |
| `CONNECTED` 时 upstream send 失败 | → `DEGRADED` 状态尝试一次重连（仅一次！autoconnect 路径下重连必弹），失败 → `DISCONNECTED` |
| `CONNECTED` 时收到 upstream 主动 `Inspector.detached` | 立即 → `DISCONNECTED`（用户从 chrome://inspect 关掉了 attach） |
| `CLOSING` 时新 client 连入 | 拒绝 ws upgrade，HTTP 503，client 应该 retry |

建议加 `DEGRADED` 中间态显式表达"upstream 半死，daemon 仍持有 client 连接"——避免 client 在 DISCONNECTED 瞬间被强制 close，给一次自愈机会。

### 7.2 Idle 监控

设计文档把"client 集合为空"作为 idle 判定的唯一信号，正确。但 **upstream 半死检测被混淆了**：

- daemon → client 的 WS keepalive（ping/pong / TCP keepalive）：保护半死 client 占着 client slot。
- daemon → upstream Chrome 的 WS keepalive：保护 Chrome 已经退出但 OS 还没回收的连接。

这两个独立。文档第 322-338 行把 ping 配置放在 `[proxy]` 段，没说是哪条链路。建议显式拆：

```toml
[proxy.client_keepalive]
ping_interval = "20s"
ping_timeout = "60s"

[proxy.upstream_keepalive]
probe_interval = "30s"     # 主动发 Browser.getVersion 当心跳
probe_timeout = "10s"
```

upstream 用 `Browser.getVersion` 作为心跳的好处：是个零副作用的 browser-level CDP，对 autoconnect Allow popup **没有任何影响**（一旦上 OPEN 之后，CDP 命令不再触发新弹窗 —— §2.4 已经证明）。

### 7.3 按 backend 配置 idle policy

§7.3 关于 "autoconnect 默认 never" 的论证（断开后重连必弹 Allow，而横幅可由 X dismiss 一劳永逸）逻辑正确。补一条：

> 即便 `idle_close_after = "never"`，**daemon 进程退出**仍然会让 ws 关闭、横幅瞬时消失。所以"显式 `browser-daemon stop`"必须是用户可达且 documented 的操作 —— 它是 autoconnect 用户找回隐私的唯一一键开关（替代了主动 idle close 的角色）。这条要写进 README。

### 7.4 SessionId 多路复用 —— 重点补全

design.md 这小节四个 bullet 没说清楚的事：

**问题陈述**：daemon 暴露给 Layer 2 的 ws 应该让多个 client 看起来都连到了同一个 browser-level CDP，但底层共用一根上游 ws。两个 client 都发 `Target.attachToTarget(targetId=X)` 怎么办？两个 client 的 `Runtime.evaluate` 应该回到正确的客户端怎么办？

**v0.2 简化策略（推荐）**：

1. **每 client 独立 sessionId 名空间**。daemon 维护一张表：`(client_id, client_local_session) ↔ upstream_session`。客户端发的 `sessionId` 是 client-local；daemon 翻译到 upstream-session 再转发。返回路径反向翻译。
2. **同一 upstream targetId 只允许被 1 个 client attach**。第二次 attach 同 targetId 返回标准 CDP error：
   ```json
   {"id": <reqId>, "error": {"code": -32602, "message": "target already attached by another client", "data": {"holder": "<label>"}}}
   ```
   这是 MVP 显著简化 —— 不必处理事件双发、命令并发交错。
3. **Browser-level events**（无 sessionId 的 CDP event）广播给所有 client。
4. **Session-scoped events** 按 upstream-session 反查 owner client，路由过去。
5. `Target.getTargets` 返回完整 target 列表（不隐藏其他 client attach 的 target），但 attach 时会拒。Layer 2 可据此决定切换 / 报错给用户。

**为什么不学 playwriter 的 stableKey 复杂度**：playwriter 要做的事更难（多扩展、断连重连、跨 profile）。daemon 在 v0.2 只服务单台 Chrome 实例 + 同进程内自己的 Layer 2 client。stableKey / rebindClientsToExtension 那套（cdp-relay.ts:103-157）现阶段是过度设计，留到 v0.4 再考虑。

**实现参考**：browser-harness `daemon.py:191-206`（`attach_first_page`）和 `:305-339`（`set_session` 处理 stale + parallel enable）演示了 sessionId 重 attach 的正确顺序：旧 session 显式 `Network.disable`，新 session `gather(Page/DOM/Runtime/Network).enable`，整段在 client IPC 5s 超时内完成。Mode B 多 client 实现应该把这个 pattern 套到每个 attach 操作上。

---

## §8 CLI 设计

补 v0.1 缺的两条命令（v0.2 才有 mode B 但 mode A 也需要）：

```
browser-daemon endpoint           # mode A 也实现 —— 输出 BD_CDP_WS 等价的 endpoint URL
browser-daemon doctor --backend autoconnect   # 单 backend 诊断，避免连带触发其它 probe
```

`status` 的输出格式（§8 86-105 行）有一个问题：`clients` 列表里的 label 比如 `(browser-skill-repl)` 怎么来？文档没说。两个选择：

- (a) 客户端连接时通过 query string `?client=<label>` 上报；
- (b) 连上之后通过 `BrowserDaemon.setLabel` RPC 上报。

推荐**两者都支持**，query string 用作"连接时初始 label"，RPC 用于运行时改名（Layer 2 REPL 当一个 skill 进入"长任务模式"时可以重命名为 `task:damai-check`）。这两个都属于 §Layer 1↔2 接口契约 的内容，下面 finalize。

Exit codes 表完整。补一条：**6 = upstream Chrome 不可达**（Mode B 操作过程中 upstream 死亡），跟"backend 不可用"(2) 区分 —— 一个是启动时拿不到 URL，一个是运行时连接断开。`status` 也应该用这个码反映 DEGRADED/DISCONNECTED。

---

## §9 各 Backend 细节

### env

补：除了 `BD_CDP_WS`、`BD_CDP_URL`，**也读 `BU_CDP_WS`、`BU_CDP_URL`**（browser-harness 的命名）。这条不是为了取悦旧用户，是为了让 browser-daemon 能 *无缝替换* browser-harness 的 daemon 模块（用户切过去不用改环境）。`BD_*` 命名空间是首选，`BU_*` 是 deprecation alias，doctor 输出建议迁移。

### rdp

§9 没提：**rdp backend 也需要 DevToolsActivePort 404 fallback**（§2.6 提过的 Chrome 147+ 行为）。具体实现参考 `browser-harness/src/browser_harness/daemon.py:115-125`：HTTP GET 失败但状态码=404 时，到 `PROFILES` 列表里找 DevToolsActivePort 匹配 port 拼 ws URL。

profile path 列表建议 *搬* browser-harness 的（`daemon.py:36-65`，覆盖 Chrome / Canary / Edge × stable/beta/dev / Brave / Arc / Dia / Chromium / Comet × macOS/Linux/Flatpak/Windows），别自己再列一遍 —— 那个表是吃过 bug 才完整的。MVP 文档可以只说"支持 Chrome stable"但实现就把全表带上，没成本。

### autoconnect

实现细节欠了几条：

1. **多个 profile 都有 DevToolsActivePort 时**（doc 提到"取最新 mtime"）—— 注意 *最新 mtime* 不一定是用户当前 active profile。Chrome 重启 inactive profile 时也会更新该文件。更稳的策略：HTTP GET `/json/version` 验证 ws URL **真的能 OPEN**（即用户在那 profile 启用了 chrome://inspect）；不能 OPEN 的就跳过。这是文件存在性之上多一层 active probe。
2. **Mode B 模式下首次连接前**，daemon 必须告知 client "**即将弹 Allow 弹窗，请等待用户操作**"。建议在 ws upgrade 完成后**立刻**发一条 `BrowserDaemon.upstreamConnecting` event；upstream OPEN 后再发 `BrowserDaemon.upstreamReady`。Layer 2 REPL 据此可以在 UI 上提示用户。如果不发，第一条 CDP 命令可能 hang 60s（用户去吃饭了），client 完全不知道发生了什么。

### extension

v0.1 占位 OK，但**协议形状**现在就应该定下来，否则 v0.2 mode B 上线时回头改架构会痛。具体设计建议：

- 默认 endpoint：`ws://127.0.0.1:19989/relay`（比 playwriter 的 19988 高一位，默认就跟它共存不冲突；路径也换掉避免误装成 playwriter）。
- 扩展握手：扩展连过来时发 `{"type":"hello","installId":"...","browser":"chrome","email":"...","version":"..."}`。
- "用户主动 attach 一个 tab" 模型：扩展不自动 attach 所有 tab，用户点扩展图标 → 扩展通过 chrome.debugger.attach 后向 daemon 发 `{"type":"tabAvailable","tabId":...,"url":...,"title":...}`。
- daemon 把扩展 attach 过的 tab 当成 "ghost target"，在 `Target.getTargets` 里以 `type:"page"` 形式返回给 Layer 2 client。Layer 2 client 发 `Target.attachToTarget` 时，daemon 通过扩展的 chrome.debugger.sendCommand 实现。
- v0.2 "browser-level CDP" 在 extension backend 上是模拟的（chrome.debugger 不是 browser-level，每 tab 一个）。`Target.createTarget` 需要 daemon 调 `chrome.tabs.create`。
- 不支持的命令：`Browser.crash`、`Storage.setStorageItems` 之类 browser-level 状态命令在 extension 路径下返回 `{"code":-32601,"message":"method not implemented in extension backend"}`。doctor 提示这条限制。

OpenCLI 的 daemon (`OpenCLI/src/daemon.ts:390-455`) 和扩展 cdp.ts 已经实现了这套大部分；可以借鉴它的 "contextId per profile" 多 profile 路由（`daemon.ts:86-114`），即便我们 v0.2 不上多 profile，接口预留一致更好。

---

## §10 错误模型

补：

```python
class UpstreamUnavailable(BrowserDaemonError):
    exit_code = 6      # mode B running, upstream Chrome died

class AuthorizationDenied(BrowserDaemonError):
    exit_code = 7      # autoconnect: user clicked "Deny" or did not click in time
```

CLI 顶层 catch 别把 UpstreamUnavailable 当 BackendUnavailable —— 排查路径完全不同。

---

## §11 测试策略

补一条 mode B 关键 case，design.md 漏了：

- **upstream Chrome 中途退出**：mock CDP server 在第二条命令后关闭 ws。断言 daemon 对每个活 client 发 `Target.detachedFromTarget`（每个 session 一条）+ `BrowserDaemon.upstreamClosed` + 主动 close client ws with code 1011。Layer 2 测试将依赖这个行为重连，没单测的话 v0.2 必坏。

- **autoconnect Allow 超时**：mock backend `resolve()` sleep > timeout，断言 daemon 进入 CONNECTING 而非 DEGRADED，超时后回到 DISCONNECTED，告知 pending client 标准错误码。

- **多 client 同 targetId attach**：mock 两 client 都 `Target.attachToTarget(X)`。断言第二个收到 `error.code=-32602`，第一个仍然正常工作。

---

## §12 命名与边界

不补。

---

## §13 与上层 Layer 2 的接口 —— 完整重写

> **这一节是给 skill-architect 直接照着实现的契约。** 上面所有改动收敛到此。原 §13 那段示例代码（`subprocess.check_output(["browser-daemon", "url"])` + `websockets.connect("ws+unix:///tmp/browser-daemon.sock")`）作为 API 层面是对的，但**契约**层面欠太多。下面是完整版。

### 13.1 Mode A 契约（v0.1）

**调用**

```
browser-daemon url [--backend NAME] [--timeout SEC] [--json] [--config PATH]
```

**stdout 行为**

- 非 `--json`：**精确一行**输出 ws URL，以 `\n` 结尾，URL 以 `ws://` 或 `wss://` 开头，无前后空白。除此之外不输出任何东西到 stdout。
- `--json`：单行 JSON `{"ws_url":"ws://...","backend":"rdp","extras":{}}\n`。`extras` 至少含 `isolated_profile: bool`（仅 rdp）、`profile_path: str | null`（仅 rdp/autoconnect）。

**stderr 行为**

- 不带 `-v`：仅在失败时输出 1-3 行人类可读 reason。
- 带 `-v` / `--verbose`：每个 backend 一次尝试的诊断（"trying env... not set"、"trying rdp... HTTP 200 ok"）。

**exit codes** — 见 §10 + 原 §8 表。

**环境变量**

| 优先级 | 变量 | 含义 |
|---|---|---|
| 1 | `BD_BACKEND` | 等价 `--backend`，命令行优先 |
| 2 | `BD_CDP_WS` | env backend 直接读 |
| 2 | `BD_CDP_URL` | env backend 走 `/json/version` |
| 3 | `BU_CDP_WS` | compat alias，等价 `BD_CDP_WS`，doctor 提示迁移 |
| 3 | `BU_CDP_URL` | compat alias |
| - | `BD_CONFIG` | config 文件路径 |
| - | `BD_TIMEOUT` | 单 backend 超时秒数，默认 5 |
| - | `BD_NAME` | Mode B 多实例名（Mode A 忽略），默认 `default` |

**Layer 2 标准用法**

```bash
# 启动 skill 进程时一次拿到 URL，导出供子流程使用
export BD_CDP_WS="$(browser-daemon url)"
exec skill-repl ...
```

或 Python：

```python
import os, subprocess
if "BD_CDP_WS" not in os.environ:
    os.environ["BD_CDP_WS"] = subprocess.check_output(
        ["browser-daemon", "url"], text=True
    ).strip()
```

**Layer 2 反义务**：Skill 不调 `browser-daemon url` 多次。一次结果缓存到 env 后整个 skill 生命周期内复用。重连失败 → 重新调一次。

### 13.2 Mode B 契约（v0.2）

#### 13.2.1 Endpoint 发现

```
browser-daemon endpoint
```

输出（POSIX 默认）：

```
ws+unix://${XDG_RUNTIME_DIR:-/tmp}/browser-daemon-${BD_NAME:-default}.sock
```

输出（Windows）：

```
ws://127.0.0.1:<port>?token=<hex>
```

其中 port/token 写在 `%TEMP%/browser-daemon-${BD_NAME}.port` 的 JSON：`{"port":N,"token":"<hex>"}`。Layer 2 优先调 `browser-daemon endpoint` 而不是自己读 port 文件 —— 文件结构允许后续演进。

`--json` 形式：

```json
{"endpoint":"ws+unix:///.../browser-daemon-default.sock","transport":"unix","name":"default"}
{"endpoint":"ws://127.0.0.1:8541?token=...","transport":"tcp","name":"default","port":8541}
```

#### 13.2.2 鉴权

- **POSIX**：socket file 权限 0600。daemon 启动时 `umask(0o077)` 包住 bind，避免 TOCTOU。参考 browser-harness `_ipc.py:166-170`。无 token。任意能 stat socket 的 uid 都可以连，不过 0600 已经把 uid 边界压到当前用户。
- **Windows**：TCP loopback 127.0.0.1:N，必须在 ws upgrade 的 query string 带 `token=<hex>`，daemon 端比对。token 64-char 十六进制由 `secrets.token_hex(32)` 生成，daemon 关闭时擦除 port 文件。

#### 13.2.3 协议

WebSocket text frames，每帧是一个 JSON 对象。**外观与直连 Chrome browser-level WS 完全一致**（JSON-RPC：`{id, method, params}` request；`{id, result | error}` response；`{method, params, sessionId?}` event）。

Layer 2 应当可以用 **任何标准 CDP 客户端库** 指向这个 endpoint 工作 —— cdp-use、pyppeteer、puppeteer-core、chrome-devtools-protocol-python。

#### 13.2.4 连接握手 query string

ws upgrade URL 支持以下 query：

| key | 必需 | 含义 |
|---|---|---|
| `token` | Windows 必需 | 鉴权 |
| `client` | 否 | 初始 label，出现在 `status` clients 列表，默认 `pid:<pid>` |
| `intent` | 否 | `repl` / `task` / `oneshot`，daemon 内部用于选择 idle policy（task 结束允许关 upstream，repl 维持） |

例：`ws://127.0.0.1:8541?token=...&client=skill-repl&intent=repl`。

#### 13.2.5 标准 CDP 透传

绝大多数 CDP 命令直接转发到 upstream，事件路由按 §7.4 v0.2 简化策略：

- `Target.attachToTarget` / `Target.detachFromTarget` 走 sessionId 翻译表，并对**重复 attach 同 targetId** 返回 `error.code=-32602`。
- `Target.createTarget` 直接转发，新 target 出现在所有 client 的 `Target.targetCreated` 事件里。
- `Target.closeTarget` 转发，但如果 target 当前被另一个 client attach，返回 `error.code=-32602`。
- `Target.getTargets` 返回完整列表（包含其它 client attach 的）。filter 由 Layer 2 决定。
- `Browser.*` 命令直接转发。
- 有 sessionId 的命令翻译 sessionId 后转发。
- 无 sessionId 的 event 广播给所有 client。
- 有 sessionId 的 event 按反向表路由到 owner client。

#### 13.2.6 `BrowserDaemon.*` 非标准命名空间

daemon 暴露的私有 RPC，方法名都以 `BrowserDaemon.` 开头，事件同 prefix。**Layer 2 是这些方法/事件的唯一消费方**。

**方法**

| method | params | result | 说明 |
|---|---|---|---|
| `BrowserDaemon.getActiveTab` | `{}` | `{targetId, url, title}` 或 `null` | Chrome UI 当前最前台的 real page（过滤 chrome://、devtools://、chrome-extension://、about:）。实现：daemon 订阅 `Target.targetInfoChanged` + 维护"最近被 `Target.activateTarget` 触达"的 targetId，回落到 first non-internal page。**关键**：CDP target 顺序不等于 Chrome tab strip 视觉顺序（cf. browser-harness gotchas），daemon 不能直接取 `Target.getTargets()[0]`。 |
| `BrowserDaemon.setLabel` | `{label: str}` | `{ok: true}` | 运行时改 client 的 status 标签。 |
| `BrowserDaemon.listClients` | `{}` | `[{label, pid, intent, attached_session_ids: [...]}]` | `status` 的等价 RPC，供同进程内查询。 |
| `BrowserDaemon.getBackendInfo` | `{}` | `{name, kind, ux_warnings: [...], upstream_state}` | Layer 2 据此调整行为 —— 例如 extension backend 时 banner 显示 "请点扩展图标授权 tab" 等 UI 提示。 |

**事件**

| method | params | 触发时机 |
|---|---|---|
| `BrowserDaemon.upstreamConnecting` | `{backend, hint?: str}` | autoconnect Allow 弹窗弹出的瞬间 / rdp 首次 HTTP probe / extension 等待扩展连入。`hint` 给 Layer 2 UI 提示用。 |
| `BrowserDaemon.upstreamReady` | `{ws_url, isolated_profile?: bool}` | upstream WS OPEN 之后。 |
| `BrowserDaemon.activeTabChanged` | `{targetId, url, title, reason: "activated"|"navigated"|"closed"}` | 仅订阅了 `BrowserDaemon.subscribeFocus` 的 client 收到。 |
| `BrowserDaemon.upstreamClosed` | `{reason: "chrome_exit"|"idle_close"|"backend_lost"|"daemon_shutdown"}` | upstream 被关闭。daemon 此后立刻关掉该 client 的 ws with code 1011。 |
| `BrowserDaemon.modeAdvisory` | `{level: "warn"|"info", message: str}` | daemon 发现 client 行为不合规（短连重连过多触发弹窗、attach 同 target 多次等）时的劝告。Layer 2 可以原样打到日志。 |

**订阅方法**

| method | 说明 |
|---|---|
| `BrowserDaemon.subscribeFocus` | 之后收到 `BrowserDaemon.activeTabChanged` |
| `BrowserDaemon.unsubscribeFocus` | 停止 |

#### 13.2.7 upstream 关闭礼仪

daemon 发现 upstream 关闭（Chrome 退出 / `Inspector.detached` / idle 触发 / `disconnect` 子命令）时，对每个连接的 client：

1. 对该 client 持有的每个 sessionId 发 `Target.detachedFromTarget` event。
2. 发 `BrowserDaemon.upstreamClosed` event with reason。
3. 用 WebSocket close code 1011（server error）关闭 ws。

Layer 2 据此决定重连还是终止。

#### 13.2.8 Backend 透明性义务

- Layer 2 **不要** import `browser_daemon` 任何 python 模块。
- Layer 2 **不要** case on `BrowserDaemon.getBackendInfo` 返回的 `name`（除了 UI 文案）。所有 backend-specific 行为（如 extension backend 的 user gesture 需求）通过标准 CDP 事件（`Target.targetCreated` 出现新可用 target）暴露。
- daemon 升级（v0.2 → v0.3）不改 13.2 协议；新方法走 capability negotiation：Layer 2 可以发 `BrowserDaemon.getBackendInfo` 看 daemon version + supported caps。

#### 13.2.9 Layer 2 推荐用法

```python
# 启动期
import os, json, subprocess, websockets

endpoint = subprocess.check_output(
    ["browser-daemon", "endpoint", "--json"], text=True
)
url = json.loads(endpoint)["endpoint"]
url += "?client=skill-repl&intent=repl"

async with websockets.connect(url) as ws:
    # 标准 CDP，跟连 Chrome 一样
    await ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
    # 同时 daemon 私有
    await ws.send(json.dumps({"id": 2, "method": "BrowserDaemon.getActiveTab"}))
```

如果 client lib 不支持 `ws+unix://` scheme，Layer 2 可以读 `transport` 字段先 fallback 到 `transport=tcp` 启动模式：daemon 接受 `serve --transport=tcp` 切到 TCP loopback。

---

## §14 开放问题 —— 收敛

按 design.md §14 原序：

- [x] **config schema 校验**：手写。引入 pydantic = 多 50ms 冷启动 + 一个非平凡依赖，对一个最多 30 行 toml 不值得。
- [x] **Windows / 多 Chromium 兼容**：实现层把 browser-harness `daemon.py:36-65` 的 PROFILES 表整张搬过来，文档层先只承诺 Chrome stable。Edge/Brave/Chromium/Arc 实际能用但不进 doctor 友好提示。
- [x] **doctor i18n**：punt。
- [x] **多 client 同 attach 同 target**：v0.2 拒绝（§7.4 规则 2）。v0.3 可能引入 "shared read" mode（多 client 都能收 event，但只第一个 attacher 能发命令），现在不做。
- [x] **upstream ws 断了重连后 sessionId 失效**：广播 fake `Target.detachedFromTarget` + `BrowserDaemon.upstreamClosed` + close client ws with 1011（§13.2.7）。**不** 让 client 继续发命令撞已失效 session。
- [x] **`launch-chrome` 子命令**：v0.3 范围。复用 browser-harness 隐式做过的事：`--user-data-dir=$(mktemp -d)` + `--remote-debugging-port=0`（让 OS 选）+ 读 DevToolsActivePort 拿到真实端口。Chrome binary 查找走 `which google-chrome` → 平台默认路径列表。

---

## §15 路线图修订

design.md 的 v0.1/v0.2 大致正确，但 **v0.2 工作量被低估**了。修订：

| Version | 范围 |
|---|---|
| **v0.1** | Mode A + `env`/`rdp`/`autoconnect` backend。autoconnect 在 mode A 下 documented as "expect 1 popup per invocation"。`extension` 占位 doctor 显示 not implemented。Mode A subprocess 契约（§13.1）完整。 |
| **v0.2** | Mode B server，**单 client only**（拒第二个 ws upgrade with HTTP 503 ）。状态机、idle policy、`BrowserDaemon.*` 命名空间、upstream 关闭礼仪。autoconnect/rdp/env backend 在 mode B 下走通。 |
| **v0.3** | Mode B 多 client + sessionId 翻译表 + 单 target 单 attacher 规则。第二个 client 也能 attach 但拿到正确错误。 |
| **v0.4** | `launch-chrome` 子命令。extension backend 真实实现（含 playwriter 风格 relay 兼容路径）。 |
| **v0.5** | `cloud` backend（Browser Use 等）。observability、metrics。 |

把 v0.2 多 client 推迟到 v0.3 是关键决定：Mode B 单 client 的实现量已经不小（state machine + idle + upstream lifecycle + BrowserDaemon namespace），加上多 client 路由完整一次性出，QA 面积爆炸。Layer 2 v0.2 阶段只有 REPL 一个 client，足够吃完一整版。

---

## 附录 A — 参考项目挖矿摘要

### browser-harness/

- `src/browser_harness/daemon.py:104-160` — `get_ws_url()` 是 MVP fallback chain 的事实参考实现。值得借鉴：DevToolsActivePort 404 fallback、profile 列表完整性、`/json/version` HTTP timeout=1s。
- `src/browser_harness/_ipc.py:166-198` — AF_UNIX 0600 umask 模式 + Windows token + ping handshake 防 port 复用。**Mode B 实现应当原样移植 ipc 层**。
- `src/browser_harness/daemon.py:191-206` — `attach_first_page` 处理 omnibox-popup → 创建 about:blank。daemon 在 active-tab 解析 fallback 时复用这条 fix。
- `src/browser_harness/daemon.py:344-356` — stale-session 自动 re-attach 模式（"Session with given id not found" → attach_first_page → retry）。Mode B 实现可以借这个 pattern，但**不要默认开**：Layer 2 在标准 CDP 协议里没有"daemon 自作主张重 attach"的预期。可以以 `?auto_reattach=1` query 开启。

**应当明确拒绝的**：browser-harness 的 IPC 不是标准 CDP（是自家 JSON+`meta` 字段）。browser-daemon Mode B 走标准 CDP wire format —— Layer 2 客户端因此可以用任意 CDP 库，是这个项目相比 browser-harness 的核心优势。

### playwriter/

- `playwriter/src/cdp-relay.ts:71-90` — relay 启动端口 19988（约定俗成的扩展 relay 端口），可以借用以提示用户冲突意识。
- `playwriter/src/chrome-discovery.ts:55-89` — `probePortStatus` `'live' | 'blocked' | 'dead'` 三态判别 + `appendSessionToWsUrl` 走 default-profile 锁定 fallback。Chrome 136+ 路径关键。
- `playwriter/src/relay-state.ts` 全文 + `docs/plan-centralize-relay-state.md` — Zustand-style 集中状态 + 纯转换 + subscribe 副作用模式。**Mode B proxy server 内部状态管理推荐这个 pattern**：状态可单测，副作用集中。Python 等价物是手写 `dataclass(frozen=True)` + 一个 `applyTransition` 集中点 + observer。
- `cdp-relay.ts:39-64` — restricted-target 过滤策略（chrome://、devtools://、edge:// 黑名单，extension URL 按 id 允许）。daemon 的 `BrowserDaemon.getActiveTab` 复用这套过滤。

**应当明确拒绝的**：playwriter 的 stableKey + 多扩展 fallback（cdp-relay.ts:103-157）是 multi-extension reconnect 场景的优化，daemon 早期不需要。它也是 1846 行 cdp-relay.ts 主要复杂度来源，必须避免被这个模式吸引。

### browser-cli/

`src/browser.ts` 整个文件靠 `@browserbasehq/stagehand` 把 CDP 连接抽象掉了，没有自己的 daemon / discovery 模块。对 browser-daemon 没有可直接借鉴的代码。可以做的反向参考：Stagehand SDK 接受 `cdpUrl` 作为唯一入参（browser.ts:89, 115），证明"daemon 输出一个 ws URL，下游全部以此为锚"的契约 *是行业惯例*，design.md Mode A 的方向对得上 Stagehand-like 客户端。

### OpenCLI/

- `OpenCLI/src/daemon.ts:86-114` — 多 profile 路由模型（`extensionProfiles: Map<contextId, ...>`，单 profile 时自动选，多 profile 要求显式 `--profile`）。日后 extension backend 真做时直接参考这套接口。
- `OpenCLI/src/daemon.ts:194-240` — daemon HTTP+WS server 的 anti-CSRF / verifyClient 模式（拒绝带 Origin 的 ws upgrade 请求避免恶意网页 drive-by）。**Mode B 必须实现**：浏览器上的恶意网页可以发 ws://127.0.0.1:port 的请求（CORS 不管 ws upgrade）。daemon `verifyClient` 拒掉非空 `Origin` header。
- `OpenCLI/extension/src/cdp.ts:96-150` — chrome.debugger 3-retry + "Another debugger is already attached" 处理（与 1Password、Playwright Bridge 等冲突）。extension backend v0.4 实现必看。

**应当明确拒绝的**：OpenCLI 的 daemon 完全绑死扩展模型，标准 `--remote-debugging-port` / autoconnect 路径它都不做。browser-daemon 必须保持 backend-pluggable，不能让 daemon 内部对 "extension is special" 有任何特殊路径渗透出去 —— 那种渗透是 OpenCLI daemon 1000+ 行不可拆的原因之一。

---

## 给 skill-architect 的总结

- 13.1 / 13.2 是给你直接照着写代码的契约，不要重新协商基础形状（exit codes、stdout 格式、CDP wire format、`BrowserDaemon.*` namespace 已经定了）。
- 现在仍然在迭代的：(a) Mode B 是否允许 multi-client（v0.2 默认拒，等你回信），(b) `BrowserDaemon.getActiveTab` 是否对 Skill 真的有用，(c) `intent=repl|task|oneshot` 这个字段你那边有没有用。
- 其它一切（backend 选型、Chrome 弹窗摩擦、横幅生命周期）对 Skill 完全透明 —— 你不用 case。
