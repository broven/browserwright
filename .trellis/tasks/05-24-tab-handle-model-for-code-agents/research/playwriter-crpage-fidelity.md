# playwriter CRPage init 保真 —— 移植清单(配合 crpage-fidelity-gap.md 我们侧 trace)

来源:playwriter fork `@xmorse/playwright-core` 的 `crPage.ts:_initialize()` 契约 + `cdp-relay.ts`/`background.ts` 实现。关键:playwriter 用的 fork,但 **CRPage `_initialize` 序列与上游基本一致**(fork 改动是 additive 的 frame-level CDP 暴露,非 init 放松)。所以下面就是 stock Playwright CRPage 的真实契约。我们 dev dep pin 的是 playwright 1.60.0。

## 关闭机制(两侧吻合)

`CRBrowserContext.newPage` → `Target.createTarget` → 等 `page.waitForInitializedOrError()`。若 `FrameSession._initialize()` 的 `Promise.all`(crPage.ts:536)reject/超时 → page 解析为 error → Playwright `Target.closeTarget` 拆掉刚建的 target。我们 trace 实测:close(id 17)在 `Page.getFrameTree`(id 7)resolve 同一 microtask、ids 9-16 返回前触发 → 是 init body rejection,不是命令错/assert/detach。

## CRPage `_initialize` 在 page session 上的批量(crPage.ts:432-536)

`Page.enable` / `Page.getFrameTree`(→`_handleFrameTree`,需有效树)/ `Page.createIsolatedWorld`(`_sendMayFail`,失败不致命)/ `Log.enable` / `Page.setLifecycleEventsEnabled` / `Runtime.enable` / `Page.addScriptToEvaluateOnNewDocument` / `Network.enable` / page-session `Target.setAutoAttach{autoAttach,waitForDebuggerOnStart:true,flatten:true}` / 主帧 `Emulation.*`/`Security.*`/`Browser.getWindowForTarget`/`Page.setInterceptFileChooserDialog` / `Runtime.runIfWaitingForDebugger` / `_firstNonInitialNavigationCommittedPromise`。

## 修复清单(按触发 close 的可能性排序)

1. **【高】`Target.attachedToTarget`/`getTargetInfo` 必须带 `browserContextId`**。`crBrowser.ts:166` `assert(targetInfo.browserContextId)` 在建 CRPage 前执行,缺则 attach handler throw → target 被弃。给任意稳定非空 id(默认 context 的 id)。还需 `type:'page'`、真实非空 `url`、`waitingForDebugger:false`。
2. **【高】`Runtime.enable` 门控**(playwriter 称最关键):transport 层做 `Runtime.disable → sleep(~50ms) → Runtime.enable`(强制 Chrome 对后到 client 重发 `executionContextCreated`),**并扣住 `Runtime.enable` 响应直到观察到该 sessionId 的 `executionContextCreated{auxData.isDefault:true}`**(~3s 超时)。我们现状只 50ms sleep,无事件门 —— 升级成事件门。code 参考:relay `cdp-relay.ts:792-829`、ext `background.ts:980-1004`。
3. **【高】`waitingForDebugger:false`**(我们已对,保持)。原理:chrome.debugger 接的是已运行 tab,从未真 pause,所以 `runIfWaitingForDebugger` 是无害 no-op,页面正常渲染。**绝不能报 true**(否则渲染器卡在 pause,init 超时→关)。
4. **【中】别把 url 改写成 `about:blank`**:新建 target Chrome 报 `':'`,Playwright `crPage.ts:479` 靠 `mainFrame().url()===':'` 判 initial-empty-page。透传 Chrome 原值;并从 `Page.frameNavigated`(top frame)持续刷新 `targetInfo.url`,别把 Playwright 困在 about:blank。
5. **【中】page-session `Target.setAutoAttach`(带 sessionId)要转发**给扩展(我们现 silent-ack)。整套 init 命令(`Page.getFrameTree/createIsolatedWorld/addScriptToEvaluateOnNewDocument/setLifecycleEventsEnabled`、`Log.enable`、`Network.enable`、`Emulation.*`、`Security.*`、`Browser.getWindowForTarget`、`Page.setInterceptFileChooserDialog`、`Runtime.runIfWaitingForDebugger`)全透传,不得致命报错。
6. **【低,iframe 才需】child/OOPIF session 路由**:child sessionId 透传(≠ tab root session 时);OOPIF `attachedToTarget` 用 `frameId→sessionId` map(由 `Page.frameAttached/frameNavigated` 喂)re-parent 到属主 page session,fallback 用 incoming sessionId 而非 root(错挂 root 会被 Playwright detach→挂起)。

## 验收

extension harness 上 `chromium.connect_over_cdp` → `ctx.new_page()` → `page.goto()` → `page.title()`/`page.content()` **高层 API** 跑通(非 CDP 级)。rdp e2e + 既有 ext CDP 级 e2e 不回归。
