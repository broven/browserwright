# Phase B: persistent per-session executor — live page/context/state across heredoc calls

## Goal

照抄 playwriter 的 `Map<sessionId, executor>`:让一个**常驻、按 sessionId 隔离的 per-session executor** 持有 live Playwright `page`/`context`/`browser` + 持久 `state` dict + 长活的 facade 连接。`browserwright <<'PY' … PY` heredoc CLI 退化成**瘦客户端**:把 code 发给本 session 的 executor 执行,回 stdout/return/error。

这样 `page` 真的是同一个 live 对象**跨 heredoc call 存活**(不再每次重连 facade + 靠 ledger `current_target_id` 重绑),`state` 按引用注入即天然持久,顺带省掉每 heredoc 的 connect 开销。这是 phase A(facade)/phase C(agent 面)之后的收官位移。

## 背景/前提(已落地)

- **Phase A**:`chromium.connect_over_cdp(ws://daemon-facade)` 可驱动 rdp + extension 两后端(含高层 API)。契约 `.trellis/spec/backend/playwright-cdp-facade.md`。facade 是 daemon 内一条**并行 TCP 传输**,不碰 DaemonState/Router,session-less 看到全部 target。
- **Phase C**:agent 面只剩 Playwright `page`/`context`/`snapshot` + 非浏览器助手。heredoc = **独立短命进程 + in-process `exec`**(`repl/inline.py`),命名空间 `repl/_namespace.py:build_globals()` 注入 lazy `page`/`context`(`repl/playwright_handle.py`):首次访问才经 facade `connect_over_cdp`,靠 ledger `current_target_id` 重绑 session 当前 tab,heredoc 末 `handle.close()` 断开。**进程一死 live 对象全没** → 跨 call 只能靠 ledger 重连重绑模拟持久,`state` 故意未注入(怕空 dict footgun)。
- **daemon 架构**:单全局 daemon,asyncio。`daemon/server/listener.py:run_serve()` 主循环;`Daemon` 类持 `shared_context`(extension/env/cloud) + `contexts: dict[session_id, UpstreamContext]`(rdp 懒建)。CLI↔daemon 走 unix socket(`mode_b_client.py`),CDP JSON-RPC + `BrowserwrightDaemon.*` 自答动词(`getActiveTab`/`endSession`/`recoverSession`/`openBackgroundTab`/...)。
- **session 模型**:`BD_SESSION`/`--session` 选 id,ledger(`~/.browserwright/sessions/ledger.json`)存 `backend`(不可变)/`runtime.current_target_id`。`session_runtime.py` 负责 persist/读 target。
- **P3 教训**:旧的常驻 Skill REPL daemon 因把 `BD_NAME`/backend 冻进**共享单例** + 转发 heredoc 不带其 env → cross-talk 事故,被删。Phase B **复活常驻 sandbox 但按 sessionId 隔离**,正是规避该坑的正确做法。

## Decision (ADR-lite)

**已定(本次 brainstorm 收敛)**:

- **D1 — executor = 独立 per-session 子进程(不是 daemon 进程内线程)。** 每 session 一个常驻 executor 子进程,跑 **sync** Playwright,持有 live `page`/`context`/`browser`/`state`,并保持一条长活 facade `connect_over_cdp` 连接。daemon 保持纯 async 代理,**绝不在特权 daemon 进程内跑 agent 任意代码**。
  - *为什么不放 daemon 进程内*:daemon 是 asyncio,sync Playwright 不能在 event loop 线程跑(需线程亲和 worker);更致命的是 agent 任意代码(死循环/segfault)会带走**管着用户真浏览器**的 daemon。blast radius 不可接受。
  - *为什么子进程对*:崩溃只炸自己 session 的 executor,不波及 daemon、不污染别 session;sync Playwright 独占自己的进程无 async 冲突;直接对齐 playwriter `Map<sessionId,executor>` 模型 + P3"别用全局单例、按 session key"的教训。
- **D2 — 完整 phase B 范围(一个 task 内多 PR 做完)**:常驻 executor + 持久 `state` + `page`/`context` 跨 call 存活 + `reset()` + 完整生命周期(懒启 / idle 回收 / `endSession` 杀 / daemon 重启后恢复)。

## 分叉已收敛(research/phase-b-forks.md,2026-05-25)

核心洞见:**daemon 本来就是 per-session 子进程管理器**——rdp-Chrome 路径(懒启/idempotent/idle 回收/endSession 杀/crash-drop/重启 orphan-sweep)就是 phase B 生命周期的现成孪生。executor = "rdp Chrome v2",照抄该监管契约。

- **Fork 1 — 生命周期 → (a) daemon 监管子进程。** `docs/refactor-single-daemon.md:27` 明写"daemon 自己拉起并拥有 per-session Chrome";"单全局 daemon"是指**单 socket/单身份/删 BD_NAME**,不是禁子进程。executor registry 按 `session_id` keying(对齐 `Daemon.contexts`,daemon.py:87),挂 `Daemon` 上而非 `_UpstreamHolder`(extension 多 session 共享一个 holder)。复用 rdp 的 spawn(`Popen(start_new_session=True)`)/track(pid 上 holder)/idle-watchdog/endSession-kill/orphan-sweep。
- **Fork 2 — 传输 → 混合:控制面走 daemon,数据面走 executor 自有 socket。** 客户端先经既有 mode_b socket 发 `BrowserwrightDaemon.ensureExecutor {session}`(daemon 懒启 executor + 回 socket 路径/写 per-session 发现文件 `bw-exec-<shortid>.sock`),再**直连** executor 的 per-session unix socket 发 `{code, timeout}`。理由:把任意 code + streaming stdout + 多 MB 截图塞进特权 daemon 的 CDP-JSON-RPC event loop 正是 D1 要避开的 head-of-line 耦合;executor 自有 socket 走我们自定义的简单 length-framed 协议,sync↔sync 无桥。
- **Fork 3 — 并发 → executor 内串行队列。** 单专用线程拥有 thread-affine 的 sync-Playwright 对象,accept loop 入队、worker FIFO 跑。默认排队(非 reject-busy),有界队列 + per-call timeout 防 wedge。
- **Fork 4 — daemon 重启 → executor 自杀冷启,不 live-reconnect。** facade 是 daemon 内部 server,重启后 ws 端口变、发现文件被 unlink,executor 的长活 `connect_over_cdp` 断。executor 检测到传输死 → 进程退出 → daemon reap → 下个 heredoc `ensureExecutor` 冷启新 executor,经 ledger fast-path(`session_runtime.ensure_session_target`)重绑 session 当前 tab。对齐 rdp 的"upstream 死就 drop-context 不重连"哲学。**`state` 在此路径丢失——诚实记录(同 `reset()`)**。
- **Fork 5 — `state` 注入 → 同一 dict 按引用每 call 注入。** executor 进程内复用 `_namespace.build_globals()`,但把 lazy `_LazyHandleProxy` 的 `page`/`context` 换成 executor 持有的 **live 对象**,加持久 `state` dict,`snapshot=make_snapshot(handle)` 重绑 executor live page。phase C 不注 `state` 是怕"看着持久其实不持久"的 footgun;phase B 注入安全**正因为它真在常驻 executor 里跨 call 存活**。
- **Fork 6 — `reset()` → executor 命名空间内注入的可调用。** 重跑冷启 bind(`_ensure_connected`+`_bind_current_page` 等价物)+ `state.clear()`。必须是注入 callable 而非 daemon 动词,因为它要操作 executor 的 live 对象。
- **Fork 7 — phase C lazy-connect → 共存,移动边界。** `inline.py` 保形:纯 `memory()`/site-skill/`http_get` heredoc 仍 in-process `exec`,**不拉起也不接触 executor**。判定靠 `inline.py` 里一个**便宜的静态预检**(`compile()` 后查 `co_names` 是否引用 `{page,context,snapshot,state,reset}`):不引用→走今天的轻量路径;引用→**整段 body 发给 executor 跑**(live 跨进程 Page 没法塞回本地 exec)。`playwright_handle.py` 的 connect/bind/「extension facade 下绝不用 Playwright CDP session」铁律**整体迁入 executor**,只在冷启/恢复时跑;per-heredoc 复用从"ledger 重解析"变成"同一 live 对象"(phase B 的核心收益)。

## 建议 PR 切片(实现时可微调)

- **PR1 — executor 进程骨架 + 数据面(MVP 核心)**:新 `browserwright._executor` 模块(sync,持 `connect_over_cdp`+cold-start bind+live page/context/state+串行队列+per-session unix socket+execute 协议)。`ensureExecutor` daemon 动词 + `_ipc` 发现文件。`inline.py` 静态预检 + 瘦客户端分流。验收:跨 heredoc `state`/`page` 存活、单 executor/单 tab,两后端 e2e。
- **PR2 — 生命周期监管**:daemon executor registry(按 session keying)+ 懒启 single-flight 锁 + idle-watchdog 回收 + endSession 杀 + crash-reap + 重启 orphan-sweep。验收:endSession/idle 后进程退出无残留、daemon 重启冷启恢复、并发两 session 不串台。
- **PR3 — `reset()` + 输出协议补全 + 文档**:注入 `reset()`、console/return/warnings/screenshots/truncation/timeout 输出块补齐、`--print-skill`/`skill_runtime.md` 改写(`state` 用法 + executor 心智 + `reset` 纪律 + 翻 phase C "每 heredoc 重连"叙述)。

## 实现期新增风险(research 已记)

- **spawn race**:同 session 并发首 heredoc 双重 spawn → 需 per-session single-flight 锁(对齐 rdp `_open_lock`+pid 检查)。
- **Windows 传输**:executor socket 在 Windows 无 AF_UNIX,需 mode_b 同款 TCP+token fallback(`_ipc.py:327-339`)——额外面,未设计,实现时标注。
- **AF_UNIX 104 字节预算**:per-session socket 名必须短(`bw-exec-<shortid>.sock`),macOS `_runtime_dir()` 是 `/tmp` 正因如此。
- **静态预检逃逸**:`g=globals(); g['page']` 这类间接引用会绕过 `co_names` 检测——可接受,因为 fallback(发 executor)永远正确,只是不够轻量。

## Requirements (evolving)

- 同一 session 跨多个 heredoc call,`page` / `context` 是**同一 live 对象**(不重连、不重绑)。
- 注入一个持久 `state` dict,跨 call 按引用存活(`state.foo = ...` 下个 heredoc 可见)。
- executor 懒启(首个需浏览器的 heredoc 才拉起),按 sessionId 隔离,互不串台。
- `reset()` 可重建连接 + 清空 `state`(连接坏/页关时用)。
- daemon 重启 / executor 崩溃后能优雅恢复(下个 heredoc 冷启新 executor 重绑 session 当前 tab)。
- `endSession` 杀掉对应 executor;idle executor 超时自动回收,不泄漏进程。
- 纯 memory()/site-skill heredoc 不应被迫拉起 executor(保持轻量)。
- extension + rdp 两后端均可用。

## Acceptance Criteria (evolving)

- [ ] heredoc A 里 `state.x = 1` / `page.goto(url)`,heredoc B 里 `state.x` == 1 且 `page` 是同一对象(`page.url` 仍是 url,无重连日志)。
- [ ] 连续 N 个 heredoc 操作只产生 1 个 tab、1 个 executor、1 条 facade 连接。
- [ ] `reset()` 后 `state` 清空、连接重建,后续 heredoc 正常。
- [ ] `endSession` 后 executor 进程退出(无残留);idle 超时同理。
- [ ] daemon 重启后下个 heredoc 冷启新 executor 并重绑到 session 原 tab。
- [ ] 两 session 并发各自 heredoc,`state`/`page` 不串台。
- [ ] extension + rdp 两后端均通过上述。

## Definition of Done

- 单测 + e2e(复用 phase A/C 的 CfT harness,两后端)覆盖:跨 call `state`/`page` 存活、单 executor/单 tab、`reset`、`endSession`/idle 回收、daemon 重启恢复、并发隔离。
- `--print-skill` / `skill_runtime.md` 更新:`state` 用法 + executor 心智模型 + `reset()` 纪律(替换 phase C 里"每 heredoc 重连"的描述)。
- lint/typecheck/CI(fast gate)绿;memory 决策更新([[copy-playwriter-model]] 补 phase B 段)。

## Out of Scope

- 合并 phase A/C 分支(另行处理)。
- 非 Playwright 的 agent 面变更(memory/site-skill/http_get 等保持 phase C 现状)。
- 远程/cloud 后端的 executor 特化(若 rdp+extension 已覆盖,cloud 复用同机制即可)。

## Research References

- `../05-24-tab-handle-model-for-code-agents/research/playwriter-exposure.md` — playwriter `ExecutorManager: Map<sessionId,executor>` 常驻进程模型 + `state` 跨 call 持久机制 + `reset()` + skill.md tab 纪律。**phase B 的头号蓝本**。
- `.trellis/spec/backend/playwright-cdp-facade.md` — facade 契约(executor 经它连 Playwright)。
- `.trellis/spec/backend/agent-playwright-surface.md` — phase C agent 面契约(executor 进程内复用其注入模型)。

## Technical Notes

- 关键文件:`daemon/server/{listener,daemon}.py`(executor registry / 监管 / 新 RPC)、`mode_b_client.py`(传输)、`repl/{inline,_namespace,playwright_handle}.py`(瘦客户端化 + 注入迁移)、`session_runtime.py`/`session_registry.py`(ledger 恢复字段)、`daemon/_ipc.py`(若走 per-session 发现文件)。
- **sync/async 边界**:daemon asyncio,executor 子进程 sync Playwright——**两进程隔离即天然解耦**,不需 sync-in-async 桥(这正是 D1 选子进程的核心收益)。
- **并发**:executor 内单线程串行执行队列;同 session 重入排队。
- **恢复**:executor 冷启时复用 phase C 的 `current_page()` agent 路径重绑 session 当前 tab(`current_target_id` ledger fast-path),映射 Page→targetId **绝不用 Playwright CDP session**(extension facade 下 fatal,见 `playwright_handle.py:_agent_page_targets`)。
- **输出协议**:参考 playwriter——console 输出 + `[return value]` + `[WARNING]` + 截断;`execute` 带 timeout。
