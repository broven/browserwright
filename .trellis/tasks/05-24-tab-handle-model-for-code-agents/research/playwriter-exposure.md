# playwriter — agent-exposure model (copy spec)

来源:remorses/playwriter `src/{mcp,executor,aria-snapshot,cdp-relay,cli}.ts` + `src/skill.md`。

## 暴露表面 = 2 个 MCP 工具

- **`execute({ code: string, timeout?: number=10000 })`** —— 跑任意 Playwright JS。`code` 描述原文:*"js playwright code, has {page, state, context} in scope. Should be one line, using ; ... you MUST call execute multiple times instead of writing complex scripts."* 工具的 `description` **整段就是编译后的 skill.md**(agent 怎么用全靠它)。返回:一个 text 块(console 输出 + `[return value]` + `[WARNING]` + 每张截图的 path/a11y snapshot,截断 10000 字)+ 每张图一个 image 块。出错附 hint 让 agent 调 `reset`。
- **`reset()`** —— 重建 CDP 连接、重置 browser/page/context、**清空 `state`**。连接坏了/页关了才用。
- 另有 3 个 doc resource(debugger/editor/styles api),非工具。

## sandbox 注入的名字(`vm.createContext`)

- `page: Page`(`getCurrentPage()`,复用不重建)、`context: BrowserContext`、`browser: Browser`、`state: Record<string,any>`(**同一对象按引用每次注入 → 持久机制**)、`console`(写进数组非 stdout)。
- `require`(白名单模块,fs 换成限域 ScopedFS)、`import()`、一堆 useful globals(fetch/URL/Buffer/crypto…)。
- ~25 个 helper:`snapshot()`/`refToLocator()`/`getCleanHTML()`/`getPageMarkdown()`/`getLocatorStringForElement()`/`getLatestLogs()`/`waitForPageLoad()`/`getCDPSession()`/`screenshotWithAccessibilityLabels()`/`resizeImageForAgent()`/`ghostCursor`/`recording.*` 等。

## snapshot:行即 locator

`snapshot({page?,frame?,locator?,search?,showDiffSinceLastCall?,interactiveOnly?})`。每行 `- role "name" <locator>`:稳定属性→`[data-testid="x"]`/`[id="x"]`,否则 `role=button[name="Submit"]`;重复加 `>> nth=N`。短 ref `e1/e2`,截图叠 `eN` 标签,`refToLocator({ref:'e3'})` 还原。**element handle 从不序列化给 agent,只给 locator 字符串/ref**。默认 diff 模式省 token。

## 防 tab 爆炸 = 纯靠 prompt 约定(skill.md 原文)

- *"Initialize state.page first"*:首调就 `state.page = context.pages().find(p=>p.url()==='about:blank') ?? await context.newPage()`,**同一 call 内立刻 goto**(防别的 agent 抢 about:blank)。
- 之后所有操作用 `state.page`;`if (!state.page || state.page.isClosed())` 才重建。
- *"Never close"* browser/context;*"No bringToFront"*;*"Snapshot before screenshot"*;*"Snapshot replaces page.evaluate() for inspection"*。
- observe→act→observe 循环,禁止盲目连续动作。
- 多 tab:`context.pages()` URL 过滤选页;popup 自动成 tab,以 `[WARNING] ... index N` 通知,`context.pages()[N]` 抓取。**无 page-ID 协议,agent 持的是 live JS 对象引用。**

## 关键:state 为什么能跨 call 存活(架构)

`ExecutorManager: Map<sessionId, PlaywrightExecutor>` 活在**常驻 relay 进程**里。每 session 一个 executor,各自的 `userState` + 各自的 CDP 连接 → **state 按 session 隔离、pages 跨 session 共享**。
**CLI 是瘦 HTTP 客户端**:`playwriter -s <id> -e <code>` POST `/cli/execute` 到 relay,relay 在**持久 executor** 里跑 code 再回结果。MCP 与 CLI 共用同一 executor 模型。

> 即:state 的持久 = 服务端常驻 per-session sandbox。P3 的坑(BD_NAME/backend 冻进**共享单例**)playwriter 用 **sessionId keying** 规避——这正是 browserwright 已有的 session 隔离原语。**P3 教训不是"别持久",是"别用全局单例,按 session key"。**

## 照抄到 browserwright = 两个架构位移

1. **持久 per-session executor**(复活 P3 删掉的常驻 sandbox,但按 sessionId 隔离规避 cross-talk);CLI/heredoc 改成把 code POST 给 daemon 内的持久 executor 执行。
2. **引擎换/加 Playwright**:当前是裸 CDP(`cdp-use`),无 Playwright 依赖,且 `skill_runtime.md:60` 明写"不暴露 Playwright 对象"。要暴露真 `page`/`context` 得 `connect_over_cdp(ws://daemon)`。

## ⚠️ 头号风险:Playwright × extension 后端(用户日常 Chrome)

extension 后端经 MV3 `chrome.debugger` relay 转发**受限 CDP 子集**;Playwright `connect_over_cdp` 要浏览器级 CDP(`Target.setAutoAttach`/`Browser.*`)。**Playwright 很可能驱不动 extension relay**(用户的默认场景),rdp 后端(daemon 自管 Chrome)大概率能成。落地前必须 spike 验证。
