# Browser Connection Notes: AutoConnect, Remote Debugging Port, and CDP WebSocket

> **历史说明（2026-05）**：本文写于 `autoconnect` backend 还在 daemon 内时。如今 `autoconnect` 已经从 `browser-daemon` 移除（Chrome 144+ popup-accumulation hazard 没法靠"开发者自律"防住）。要驱动用户日常 Chrome，请用 **`extension` backend**：`browser-daemon serve --backend extension` + 装配套未打包扩展，靠 `chrome.debugger` 中继 CDP，零 popup、零横幅。下文中对 AutoConnect 的描述属于 Chrome 自身这条能力的 field notes，仍然准确，但 daemon 不再走这条路。

本文总结 Chrome 浏览器自动化/调试里几个容易混淆的概念：

- Chrome DevTools MCP / AutoConnect
- `--remote-debugging-port`
- `DevToolsActivePort`
- CDP WebSocket
- Browser-level CDP endpoint
- Page-level / Target-level CDP endpoint
- 通过浏览器插件实现类 Remote Debugging Port 的 CDP relay

重点结论：**AutoConnect 和传统 Remote Debugging Port 在拿到 CDP WebSocket 之后，后续 CDP 流程基本一样；区别主要在前置的发现、授权和 profile 使用方式。**

实测补充：在当前 Chrome 测试中，`/json/version`、`/json` 这类 HTTP discovery 阶段没有触发授权框；**打开 browser-level CDP WebSocket 的握手阶段就触发了 Chrome 的 “Allow remote debugging?” 弹窗**，即连接：

```text
ws://127.0.0.1:9222/devtools/browser/<browser-id>
```

时弹出，而不是等到 `Target.attachToTarget` 或 page-level 操作阶段才弹出。

另一个实测/实现结论：**通过浏览器插件实现的类 CDP relay 不走 Chrome 原生 remote debugging WebSocket，因此不会触发 AutoConnect 那套 “Allow remote debugging?” 弹窗。** 例如 Playwriter 的方式是：Playwright 连接插件工具自己暴露的本地 CDP relay，relay 再通过 Chrome Extension 的 `chrome.debugger` API 控制 tab。

---

## 1. 核心概念速览

### CDP

CDP 是 Chrome DevTools Protocol，用于调试和控制 Chrome。

常见能力包括：

- 列出/创建/关闭 tab
- attach 到页面 target
- 执行 JavaScript
- 截图
- 监听网络请求
- 模拟输入
- 读取 DOM / Accessibility tree

CDP 通常通过 WebSocket 通信。

---

### Remote Debugging Port

传统方式是启动 Chrome 时传：

```bash
chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-debug-profile
```

这里的 `9222` 不是 CDP WebSocket URL，而是 Chrome DevTools discovery HTTP 服务的端口。

通常先访问：

```bash
curl http://127.0.0.1:9222/json/version
```

得到 browser-level WebSocket：

```json
{
  "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc..."
}
```

或者访问：

```bash
curl http://127.0.0.1:9222/json
```

得到 page target 列表，每个页面可能有：

```text
ws://127.0.0.1:9222/devtools/page/page-id...
```

所以关系是：

```text
--remote-debugging-port=9222
    ↓
http://127.0.0.1:9222/json/version
    ↓
ws://127.0.0.1:9222/devtools/browser/<browser-id>
```

---

### DevToolsActivePort

`DevToolsActivePort` 是 Chrome 写到 user data dir / profile 目录里的一个发现文件。

它通常包含两行：

```text
<port>
<websocket path>
```

例如概念上类似：

```text
49623
/devtools/browser/abc123
```

工具可以用它拼出：

```text
ws://127.0.0.1:49623/devtools/browser/abc123
```

或者访问：

```text
http://127.0.0.1:49623/json/version
```

再读取 `webSocketDebuggerUrl`。

注意：

- `DevToolsActivePort` 不是启动参数。
- 它是 Chrome 写出的“当前活跃 DevTools 端口 + WebSocket path”的发现文件。
- 如果启动时指定了 `--remote-debugging-port=9222`，文件里的 port 可能就是 `9222`。
- 如果是 AutoConnect / 当前 Chrome 实例授权式连接，port 可能是 Chrome 自动分配的随机端口。

---

## 2. 传统 Remote Debugging Port 流程

传统方式要求在启动 Chrome 时显式开放调试端口。

```text
用户/脚本启动 Chrome
        │
        ▼
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/profile
        │
        ▼
Chrome 监听 http://127.0.0.1:9222
        │
        ▼
客户端访问 /json/version 或 /json
        │
        ▼
拿到 CDP WebSocket URL
        │
        ▼
建立 CDP WebSocket
        │
        ▼
Target.getTargets / Target.attachToTarget / Runtime.evaluate / Page.navigate ...
```

特点：

| 项 | 说明 |
|---|---|
| 是否需要重启 Chrome | 通常需要用参数启动 |
| 端口来源 | 用户显式指定，例如 `9222` |
| profile | Chrome 新版本通常要求非默认 `--user-data-dir` |
| 用户授权弹窗 | 通常没有每次 attach 的 Allow 弹窗 |
| 适合场景 | 无人值守、服务器、指纹浏览器暴露 CDP port、独立自动化 profile |

Chrome 新版本对默认 profile 的 `--remote-debugging-port` 越来越严格。实际自动化里通常要使用非默认 user data dir：

```bash
--user-data-dir=/some/non-default/profile
```

---

## 3. AutoConnect / 当前 Chrome 实例连接流程

Chrome DevTools MCP 最近的 `--autoConnect`，以及 Browser Harness 的 Way 1，本质都是：

> 对正在运行的真实 Chrome profile，请求一个远程调试会话。Chrome 让用户授权后，客户端获得 CDP 入口。

用户侧通常需要先在 Chrome 中打开：

```text
chrome://inspect/#remote-debugging
```

勾选：

```text
Allow remote debugging for this browser instance
```

Chrome 144+ 之后，首次 attach 或某些 attach 场景下，还可能弹出：

```text
Allow remote debugging?
```

用户需要点击 Allow。

AutoConnect 概念流程：

```text
Chrome 已经在运行
        │
        ▼
用户在 chrome://inspect/#remote-debugging 启用 remote debugging
        │
        ▼
客户端 / MCP server / harness 请求连接当前 Chrome
        │
        ▼
Chrome 弹 Allow remote debugging 授权框
        │
        ▼
用户点击 Allow
        │
        ▼
Chrome 暴露/写出 DevToolsActivePort
        │
        ▼
客户端发现实际 port + WebSocket path
        │
        ▼
建立 CDP WebSocket
        │
        ▼
后续 CDP 流程
```

Browser Harness 的实现路径大致是：

```text
browser-harness CLI
        │
        ▼
ensure_daemon()
        │
        ▼
启动 browser_harness.daemon
        │
        ▼
daemon.get_ws_url()
        │
        ├─ 如果 BU_CDP_WS 存在，直接使用
        ├─ 如果 BU_CDP_URL 存在，访问 /json/version 得到 WS
        ├─ 否则扫描 Chrome profile 目录里的 DevToolsActivePort
        └─ 最后 fallback 探测 9222 / 9223
        │
        ▼
CDPClient(ws_url).start()
        │
        ▼
Chrome 可能弹 Allow remote debugging
        │
        ▼
Target.getTargets
        │
        ▼
Target.attachToTarget
        │
        ▼
Page / DOM / Runtime / Network / Input 命令
```

---

## 4. AutoConnect 和 Remote Debugging Port 的关系

### 实测：授权框在 browser-level WebSocket 握手阶段出现

用分阶段脚本测试当前 Chrome 行为时，结果是：

```text
PHASE 1: GET /json/version
PHASE 2: GET /json
PHASE 3: Open BROWSER-LEVEL WebSocket
```

在 Phase 3 执行：

```text
Opening browser-level: ws://127.0.0.1:9222/devtools/browser/b4530e92-93ab-4f0d-87e1-b3ef4c2db4db
browser-level: WebSocket OPEN
```

Chrome 弹出了 “Allow remote debugging?” 授权框。

因此对这次测试环境来说：

```text
HTTP discovery (/json/version, /json)
        │
        │ 未触发授权框
        ▼
打开 browser-level CDP WebSocket
        │
        │ 触发 Chrome Allow remote debugging 弹窗
        ▼
WebSocket OPEN
        │
        ▼
后续 Browser.getVersion / Target.getTargets / Target.attachToTarget
```

也就是说，授权不是等到 `Target.attachToTarget` 或具体页面命令才发生，而是在 browser-level CDP WebSocket 连接/握手时发生。

### 实测：授权弹窗的记忆策略

Chrome 144+ 对 "Allow remote debugging?" 弹窗**没有任何记忆**。每一次 browser-level WebSocket 握手都会重新触发授权框，包括：

- 立即重连（关 ws 后立刻再连）
- 短间隔重连（关 ws 5s 后再连）
- 并行连接（已有 ws 活着时再开第二个 ws）
- 同进程串行重连
- Chrome 进程重启后重连

→ 含义：连续多次连接同一 Chrome 实例，每次都需要用户手动点 Allow。
→ 自动化客户端在 autoconnect 路径下必须**单 WebSocket 长连**，避免反复弹窗。

### 实测：横幅（"Chrome is being controlled by automated test software"）的生命周期

| 触发 | 横幅 |
|---|---|
| 第一次 browser-level WS open | 立即出现 |
| WS idle（15s / 60s 不发命令） | 保持（与流量无关） |
| 关掉唯一的 WS | 立即消失（零延迟） |
| 关掉重连 | 立即重新出现 |
| 并行第二个 WS（已有一个） | 无变化（boolean，不是 counter） |
| 关其中一个 WS，另一个还在 | 保持 |
| 关最后一个 WS | 立即消失 |

→ 横幅严格绑"是否有任意 browser-level WS 活着"，零延迟、无 grace period。
→ 横幅与具体连接协议**无关**：autoconnect、`--remote-debugging-port`、浏览器扩展走 `chrome.debugger` API——只要有外部 CDP 客户端在用 Chrome，横幅都会出现。
→ 横幅出现位置只取决于"**连接的是不是用户日常的 Chrome 实例**"：

| Backend × Profile | 弹窗 | 横幅是否在用户视线里 |
|---|---|---|
| `--remote-debugging-port` + **独立自动化 profile**（后台 Chrome） | 无 | **否**（用户看不见那个窗口） |
| `--remote-debugging-port` + 用户日常 profile | 无 | 是 |
| 浏览器扩展 relay（用户日常 Chrome） | 无 | 是 |
| AutoConnect（用户日常 Chrome） | **每次连** | 是 |

唯一"无打扰长连"路径是 RDP + 独立 profile。所有其它路径在日常 Chrome 上都会让用户看到横幅。

### 实测：弹窗与横幅的时序

| 时刻 | popup | 横幅 |
|---|---|---|
| 客户端发起 WS 握手 | 显示 | ❌ 不出现 |
| popup 显示中（用户未点 Allow） | 显示 | ❌ 不出现 |
| 用户点击 Allow | 消失 | — |
| 紧接着 WS 握手完成 OPEN | — | ✅ 立即出现 |

→ 横幅严格门控于 **WS 握手完成**，不在 popup 显示时就出现。
→ "用户点 Allow → WS open" 的延迟接近 0（毫秒级）。
→ 推论：客户端**不可能**"静默预热"上游 ws——只要 ws OPEN，横幅就必然显示。

### 实测：横幅 X 按钮（关闭）的语义

横幅右侧有 X 按钮。实测：

- **不影响 CDP 连接**：WS 仍 OPEN，所有 CDP 命令照常工作。
- **多 WS 时只是 hide UI**：开两个 WS 并行运行，点 X 后**两个**都不会被关。
- **per-WS 持久**：dismiss 后在同一 WS 上做下列操作都**不会**让横幅重新出现：
  - `Target.createTarget`（CDP 打开新 tab）
  - `Page.navigate`（CDP 导航）
  - `Target.attachToTarget`（attach 新 target）
  - `Runtime.evaluate`（任意 CDP 命令）
  - 用户手动 Cmd+T 开新 tab
  - 用户手动地址栏导航
- **仅在 WS close + 新 WS 时重置**：dismiss 状态绑当前 WS 实例。WS 关掉再重新连接（新握手），横幅会重新出现。

→ X 是**纯 UI dismiss**，CDP 协议层完全无感。客户端不需要监听、不需要响应、也无法预测。

### 实测结论的实用含义

把上面四块合在一起：

- AutoConnect 路径下，**一次 Allow + 一次 X dismiss = 整个工作 session 零干扰**（前提：客户端必须保持单一长连接的 WS）。
- 任何"短连 + 重连"模式都会被反复弹窗淹没——这是 Chrome 144+ AutoConnect 的硬约束。
- 想完全无打扰：用 `--remote-debugging-port` 启动一个独立的自动化 Chrome（非默认 user-data-dir），让横幅出现在用户看不见的窗口里。
- 浏览器扩展 relay 没有弹窗（走扩展权限模型），但仍然有横幅。

---

## 5. AutoConnect 和 Remote Debugging Port 的关系

可以分成两个阶段看。

### 阶段 A：拿到 CDP WebSocket 之前

这一段不同。

Remote Debugging Port：

```text
启动时指定 --remote-debugging-port=9222
客户端直接知道 http://127.0.0.1:9222
```

AutoConnect：

```text
Chrome 已经在运行
用户启用 chrome://inspect/#remote-debugging
客户端请求连接
Chrome 弹窗授权
客户端从 DevToolsActivePort / discovery 机制发现实际端口和 WS path
```

AutoConnect 多了：

- 当前 profile 级别的 remote debugging 开关
- 用户授权弹窗
- 通过 profile 中的 `DevToolsActivePort` 自动发现端口
- 端口可能不是固定 `9222`
- 可以连接用户正在使用的真实 Chrome profile

### 阶段 B：拿到 CDP WebSocket 之后

这一段基本一样。

一旦拿到：

```text
ws://127.0.0.1:<port>/devtools/browser/<browser-id>
```

后续都是标准 CDP：

```text
CDP WebSocket
        │
        ▼
Target.getTargets
        │
        ▼
Target.attachToTarget
        │
        ▼
获得 sessionId
        │
        ▼
Runtime.evaluate / Page.navigate / Page.captureScreenshot / Input.dispatchMouseEvent ...
```

所以准确说法是：

> AutoConnect 和 `--remote-debugging-port` 在拿到 CDP WebSocket 之后，后续流程基本一样；AutoConnect 的差异主要在前置阶段：它面向正在运行的真实 Chrome profile，通过用户授权和 DevToolsActivePort 发现实际 CDP 入口。

---

## 6. 通过浏览器插件实现类 Remote Debugging Port

除了 Chrome 原生的 remote debugging port / AutoConnect 之外，还可以通过浏览器插件实现一个“看起来像 CDP endpoint”的本地 relay。

典型例子是 Playwriter。

它不是让客户端连接 Chrome 原生的：

```text
ws://127.0.0.1:<chrome-port>/devtools/browser/<browser-id>
```

而是自己启动一个本地 relay，例如：

```text
ws://127.0.0.1:19988/cdp/<client-id>
```

Playwright / MCP client 连接这个 relay，relay 再和浏览器插件通信。浏览器插件在 Chrome 内部使用：

```ts
chrome.debugger.attach({ tabId }, '1.3')
chrome.debugger.sendCommand({ tabId }, method, params)
```

控制真实 tab。

整体结构：

```text
Playwright / Agent
        │
        ▼
插件工具暴露的本地 CDP relay
ws://127.0.0.1:19988/cdp/<client-id>
        │
        ▼
Relay 模拟 browser-level CDP 行为
Target.getTargets / Target.attachToTarget / events ...
        │
        ▼
WebSocket / native messaging / localhost channel
        │
        ▼
Chrome Extension
        │
        ▼
chrome.debugger.attach(tabId)
chrome.debugger.sendCommand(...)
        │
        ▼
Chrome tab
```

### 为什么这种方式不会触发 AutoConnect 的 Allow 弹窗？

Chrome 的 “Allow remote debugging?” 弹窗管的是外部进程连接 Chrome 原生 remote debugging WebSocket 的路径，例如：

```text
ws://127.0.0.1:<chrome-port>/devtools/browser/<browser-id>
```

而插件 relay 方式没有连接这个 Chrome 原生 endpoint。外部客户端连接的是插件工具自己开的 relay：

```text
ws://127.0.0.1:19988/cdp/<client-id>
```

从 Chrome 的角度看，真正控制页面的是已经安装并获得权限的扩展调用：

```ts
chrome.debugger.attach(...)
chrome.debugger.sendCommand(...)
```

它走的是 Chrome Extension 权限模型，而不是 AutoConnect / remote debugging port 权限模型。因此不会弹出 AutoConnect 那个 “Allow remote debugging?” 对话框。

授权边界变成了：

```text
安装扩展时授予 debugger 权限
        +
扩展产品自己的用户确认方式
例如点击扩展图标连接某个 tab
```

### `chrome.debugger` 和 page-level CDP 的关系

对一个已经 attach 的普通网页 tab，`chrome.debugger.sendCommand()` 在很多页面级命令上非常接近 page-level CDP WebSocket：

```text
Runtime.evaluate
Page.navigate
Page.captureScreenshot
DOM.getDocument
Network.enable
Input.dispatchMouseEvent
Emulation.*
Accessibility.*
```

但它不是完整等价于 browser-level CDP WebSocket。

差异包括：

- 它只能在 Chrome 扩展环境里使用。
- 扩展需要声明 `debugger` 权限。
- 通常以具体 `tabId` / target 为中心，而不是天然连接整个 browser process。
- 不能随便 attach `chrome://`、`devtools://`、其他扩展页面等受保护页面。
- 一个 tab 同时只能有一个 debugger attach，可能和 DevTools 或其他扩展冲突。
- 事件通过 `chrome.debugger.onEvent` 返回，需要 relay 重新包装成 CDP event。
- Browser-level / Target-level 能力常需要 relay 自己模拟或用 `chrome.tabs`、`chrome.windows` 等扩展 API 补齐。

因此插件工具通常会做一层 compatibility layer：

```text
Page-level 命令
  → 转发给 chrome.debugger.sendCommand

Browser/Target-level 命令
  → relay 自己维护 connectedTargets
  → 或用 chrome.tabs / chrome.windows 实现

Chrome debugger events
  → extension 转发给 relay
  → relay 包装成 CDP events 发给 Playwright
```

### 能否连接已经打开的页面？

插件形态理论上可以列出当前 profile 的 tabs：

```ts
const tabs = await chrome.tabs.query({})
```

也可以对普通网页 tab 执行：

```ts
await chrome.debugger.attach({ tabId }, '1.3')
```

所以技术上可以实现：列出已打开页面、选择某个页面、attach 后暴露给 Playwright。

但出于安全和隐私，很多工具不会默认 attach 所有 tabs，而是要求用户显式授权。例如 Playwriter 的设计是用户点击扩展图标后，该 tab 才进入 connected 状态。这个限制更多是产品/安全策略，而不是 Chrome 插件技术上完全做不到。

---

## 7. CDP WebSocket 的两类入口

CDP WebSocket 常见有两种层级：

1. Browser-level endpoint
2. Page-level / Target-level endpoint

它们都是 CDP，但连接对象不同。

---

## 8. Browser-level CDP WebSocket

典型 URL：

```text
ws://127.0.0.1:9222/devtools/browser/<browser-id>
```

它连接的是整个 Chrome browser process。

常见能力：

```text
Browser.getVersion
Target.getTargets
Target.createTarget
Target.closeTarget
Target.attachToTarget
Target.setDiscoverTargets
Target.setAutoAttach
```

Browser-level 通常流程：

```text
connect ws://.../devtools/browser/<browser-id>
        │
        ▼
Target.getTargets
        │
        ▼
找到 page targetId
        │
        ▼
Target.attachToTarget
        │
        ▼
获得 sessionId
        │
        ▼
后续页面命令带 sessionId
```

示例 CDP 消息：

```json
{
  "id": 1,
  "method": "Target.getTargets"
}
```

attach 到页面：

```json
{
  "id": 2,
  "method": "Target.attachToTarget",
  "params": {
    "targetId": "page-123",
    "flatten": true
  }
}
```

返回：

```json
{
  "id": 2,
  "result": {
    "sessionId": "session-456"
  }
}
```

之后对页面执行 JS：

```json
{
  "id": 3,
  "sessionId": "session-456",
  "method": "Runtime.evaluate",
  "params": {
    "expression": "document.title",
    "returnByValue": true
  }
}
```

Browser Harness、Playwright、Puppeteer 这类完整自动化框架通常偏向 browser-level，因为它们需要管理多个 target、tab、iframe、worker 等。

---

## 9. Page-level / Target-level CDP WebSocket

典型 URL：

```text
ws://127.0.0.1:9222/devtools/page/<target-id>
```

它直接连接到某一个 page target。

可以直接发送页面相关命令：

```text
Page.navigate
Page.captureScreenshot
Runtime.evaluate
DOM.getDocument
Network.enable
Input.dispatchMouseEvent
```

Page-level 通常流程：

```text
connect ws://.../devtools/page/<target-id>
        │
        ▼
Page.enable / Runtime.enable / DOM.enable / Network.enable
        │
        ▼
Runtime.evaluate / Page.navigate / Screenshot ...
```

不需要先 `Target.attachToTarget`，也通常不需要带 `sessionId`，因为 WebSocket 本身已经绑定到这个页面 target。

示例：

```json
{
  "id": 1,
  "method": "Runtime.evaluate",
  "params": {
    "expression": "document.title",
    "returnByValue": true
  }
}
```

---

## 10. Browser-level vs Page-level 对比

| 对比项 | Browser-level | Page-level / Target-level |
|---|---|---|
| URL 形态 | `/devtools/browser/<id>` | `/devtools/page/<targetId>` |
| 连接对象 | 整个 Chrome browser process | 单个 page/tab target |
| 获取方式 | `/json/version` 常返回这个 | `/json` / `/json/list` 返回页面列表 |
| 是否能列出所有 target | 可以，`Target.getTargets` | 不适合 |
| 是否能创建新 tab | 可以，`Target.createTarget` | 不适合 |
| 是否能切换/管理多个 tab | 可以 | 很有限 |
| 页面操作 | attach 后带 `sessionId` | 直接发页面命令 |
| 适合场景 | 自动化框架、harness、多页面控制 | 简单调试单页 |

---

## 11. `/json/version` 和 `/json` 的区别

假设 Chrome discovery 服务在：

```text
http://127.0.0.1:9222
```

### `/json/version`

```bash
curl http://127.0.0.1:9222/json/version
```

通常返回 browser-level endpoint：

```json
{
  "Browser": "Chrome/...",
  "Protocol-Version": "1.3",
  "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"
}
```

### `/json` 或 `/json/list`

```bash
curl http://127.0.0.1:9222/json
```

通常返回 page targets：

```json
[
  {
    "id": "page-123",
    "type": "page",
    "title": "Example",
    "url": "https://example.com",
    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-123"
  }
]
```

---

## 12. 总结图

```text
传统 Remote Debugging Port
──────────────────────────

chrome --remote-debugging-port=9222 --user-data-dir=/tmp/profile
        │
        ▼
http://127.0.0.1:9222/json/version
        │
        ▼
ws://127.0.0.1:9222/devtools/browser/<browser-id>
        │
        ▼
Browser-level CDP
        │
        ▼
Target.getTargets
        │
        ▼
Target.attachToTarget
        │
        ▼
Page sessionId
        │
        ▼
Page / Runtime / DOM / Network / Input commands
```

```text
AutoConnect / 当前 Chrome profile
────────────────────────────────

Chrome 已经在运行
        │
        ▼
chrome://inspect/#remote-debugging 启用
        │
        ▼
客户端请求连接
        │
        ▼
Chrome 弹 Allow remote debugging
        │
        ▼
用户点击 Allow
        │
        ▼
profile/DevToolsActivePort
        │
        ▼
port + /devtools/browser/<browser-id>
        │
        ▼
ws://127.0.0.1:<port>/devtools/browser/<browser-id>
        │
        ▼
Browser-level CDP
        │
        ▼
Target.getTargets
        │
        ▼
Target.attachToTarget
        │
        ▼
Page sessionId
        │
        ▼
Page / Runtime / DOM / Network / Input commands
```

```text
浏览器插件类 CDP relay
─────────────────────

Playwright / Agent
        │
        ▼
ws://127.0.0.1:19988/cdp/<client-id>
        │
        ▼
插件工具自己的 relay
        │
        ▼
Chrome Extension
        │
        ▼
chrome.debugger.attach(tabId)
chrome.debugger.sendCommand(...)
        │
        ▼
Chrome tab

不连接 Chrome 原生 remote debugging WebSocket，
因此不会触发 AutoConnect 的 “Allow remote debugging?” 弹窗。
```

---

## 13. 最终结论

1. **Remote Debugging Port 不是 CDP WebSocket URL。**  
   它是 discovery HTTP 服务端口。通过 `/json/version` 或 `/json` 才能拿到 WebSocket URL。

2. **DevToolsActivePort 是 Chrome 写出的发现文件。**  
   它记录当前活跃 DevTools 端口和 WebSocket path，常用于 AutoConnect / 当前 Chrome 实例发现。

3. **AutoConnect 和 Remote Debugging Port 后续 CDP 流程基本一样。**  
   区别主要在拿到 WebSocket 前：AutoConnect 支持连接正在运行的真实 profile，并引入用户授权流程。当前实测中，授权框在打开 browser-level CDP WebSocket 时出现，而不是在 HTTP discovery 或 `Target.attachToTarget` 时才出现。

4. **CDP WebSocket 有 Browser-level 和 Page-level。**  
   Browser-level 连接整个浏览器，适合自动化框架；Page-level 直接连接单个页面，适合简单单页调试。

5. **完整自动化通常选择 Browser-level。**  
   因为它能 `Target.getTargets`、`Target.createTarget`、`Target.attachToTarget`，更适合管理多个 tab、iframe、worker 和 session。

6. **浏览器插件可以实现一个类 remote debugging port 的 CDP relay。**  
   这种方式不是连接 Chrome 原生 `/devtools/browser/...` WebSocket，而是让外部客户端连接工具自己的 relay，再由扩展通过 `chrome.debugger` API 控制 tab。它走扩展权限模型，因此不会触发 AutoConnect 的 “Allow remote debugging?” 弹窗。
