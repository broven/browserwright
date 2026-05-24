# Tab handle model for code agents — 照抄 playwriter(分期)

## Goal

根治 code agent "狂开 tab":根因是暴露的是 browser-level CDP、能无限建页,却没给 agent "拿到/持有 page 句柄、后续操作都指向它"的心智模型。决定**照抄 playwriter 的暴露模型**——把(真)Playwright 交给 agent:单 `execute(code)` + 注入 `page/context/state` + 持久 `state` 当句柄 + snapshot 行即 locator + prompt 约定(首调复用 about:blank、同 call goto、之后只用 state.page、never close)。

## Decision (ADR-lite)

**Context**:agent 拿不到也记不住 page 句柄 → 退化成反复 `open()`(一次会话 9 次 open = 9 tab)。参考 playwriter / agent-browser,用户决定照抄 playwriter(直接暴露 Playwright)。

**Decision**:
- **引擎 = 真 Playwright**,经 daemon 的 **CDP facade** 用 `chromium.connect_over_cdp(ws://daemon)` 接入。已验证可行:playwriter 也是扩展后端 + Playwright,靠 relay 模拟浏览器级 CDP 握手(`Target.*`/`Browser.*` 从 `connectedTargets` 合成)、page 域经 `chrome.debugger` 转发。**我们 `extension_upstream.py` 已在用同一技术,差距是补丁级**(见 research)。
- **P3 不算回退**:playwriter 用 `Map<sessionId, executor>` 按 session 隔离,正好避开 P3 的"全局单例 cross-talk"坑——session 隔离是 browserwright 已有原语。
- **分期落地**,MVP = 阶段 A。

**Consequences**:引入 Playwright 依赖;daemon 加对外 CDP 端点;agent 接口与 `skill_runtime.md` "不暴露 Playwright" 的旧立场反转(需改文档)。rdp 后端更易先证;extension 后端(用户主场景)是最终目标。

## Scope:分期

### 阶段 A(本任务 MVP)— CDP facade + Playwright 跨通
让 `chromium.connect_over_cdp(daemon_ws)` 能驱动我们的后端。
- A1 对外 Playwright-facing 裸 CDP ws 端点 + `/json/version` 发现路由(包在既有 relay/proxy 之上)。
- A2 `Target.setAutoAttach`/`setDiscoverTargets` 从"只 ack"升级为"ack + 合成 `Target.attachedToTarget`/`targetCreated` 事件流"(Playwright 发现 tab 的命门)。
- A3 `Target.createTarget` → 映射 `openBackgroundTab`;`getTargets` scope 策略(放开 or 保持,实测定)。
- A4 `Runtime.enable` 执行上下文屏障(防竞态,健壮性)。
- 先在 rdp 后端证通(最便宜),再在 extension 后端(日常 Chrome)证通。

### 阶段 C(后续)— agent 接口
`execute(code)` 工具 + 注入 `page/context/state` + snapshot 行即 locator + 移植 skill.md prompt 约定。句柄跨 heredoc 先靠现有 daemon ledger 重绑(不依赖持久 state)。

### 阶段 B(后续)— 持久 per-session executor
常驻 sandbox(按 sessionId 隔离),CLI/heredoc POST code 给它执行,让 `state`/live `page` 跨 call 存活,真·1:1 playwriter。

## Requirements (阶段 A)

- daemon 暴露一个 Playwright `connect_over_cdp` 能连的 CDP ws 端点 + HTTP 发现路由。
- Playwright 连上后能 `Target.getTargets` 看到后端 tab、`attachToTarget` 拿 session、对 page 执行 `goto/click/evaluate`。
- `setAutoAttach` 后能收到所有现存 target 的 `attachedToTarget`,`context.pages()` 枚举正确。
- rdp 与 extension 两后端均跑通同一条 Playwright 驱动路径。

## Acceptance Criteria (阶段 A)

- [ ] e2e:`chromium.connect_over_cdp(daemon_ws)` → 打开页 → `page.goto` → `page.title()`/`page.click` 成功(rdp 后端)。
- [ ] e2e:同上,extension 后端(日常 Chrome / Chrome-for-Testing harness)。
- [ ] `context.pages()` 能枚举后端已有 tab;新开 tab 触发 `targetCreated`/`attachedToTarget`。
- [ ] 既有 `browserwright`/`BrowserwrightDaemon.*` 客户端路径不回归(facade 是新增端点,不动旧的)。

## Definition of Done

- Playwright 加为依赖(按 [uv 偏好] 管理)。
- 单元覆盖 facade 的 CDP 方法模拟 + target 事件合成;extension e2e harness(tests/daemon/e2e)跑通跨通用例。
- lint/typecheck/CI 绿。
- `skill_runtime.md`/`--print-skill` 暂注明"Playwright facade 实验中"(C 阶段再改 agent 接口文档)。
- memory 决策更新(照抄 playwriter / 分期)。

## Out of Scope(阶段 A)

- 阶段 C:`execute(code)` agent 接口、snapshot ref 改造、skill.md prompt 移植。
- 阶段 B:持久 per-session executor / live `state` 跨 call。
- OOPIF/iframe 子 session 透传(iframe 才需,nice-to-have,A 不做)。
- `Browser.crash/close` 等改用户浏览器状态的破坏性方法(永不支持 extension 后端)。

## Research References

- [`research/playwriter-exposure.md`](research/playwriter-exposure.md) — playwriter 给 agent 的完整暴露面(2 工具/sandbox/snapshot/state/skill.md)。
- [`research/playwright-over-extension-bridge.md`](research/playwright-over-extension-bridge.md) — Playwright×扩展桥接逐方法对照 + 我们的 delta(A 阶段工作量依据)。

## Technical Notes

- 关键文件:`daemon/server/{relay,extension_upstream,proxy,listener}.py`(facade 落点)、`daemon/backends/{extension,rdp}.py`、`primitives/page.py`、`api.py`(EXPORTS)、`skill_doc.py`/`skill_runtime.md`、`repl/inline.py`(C/B 阶段)。
- 现有模拟:`extension_upstream.py:334-407` 已拦截 `Target.setAutoAttach/getTargets/attachToTarget`、`Browser.getVersion`;`relay.py:597-694` 扩展协议 dispatch;扩展 background.js 用 `chrome.debugger.attach/sendCommand/onEvent`。
- 约束:heredoc 现为独立进程、无跨 call in-process 状态(B 阶段才改);daemon ledger 是当前唯一跨 call 持久层。
- 依赖策略:Python 包用 [uv]。
