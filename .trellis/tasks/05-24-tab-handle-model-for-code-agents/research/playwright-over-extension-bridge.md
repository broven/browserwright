# Playwright × extension 后端怎么桥接(playwriter vs 我们)

## playwriter 的拓扑(`cdp-relay.ts` 2192 行 + `extension/background.ts`)

```
Playwright(connectOverCDP) ──ws──► Relay(Hono ws, :19988, /cdp/:clientId) ◄──ws── 扩展(MV3 SW, chrome.debugger)
        说真 CDP                      relay 模拟浏览器级 CDP                    chrome.debugger.sendCommand per tab
```

relay 还实现 `/json/version`(返回 `webSocketDebuggerUrl: ws://host/cdp`)让 CDP client 能 bootstrap。

## relay 自己模拟(不转发给扩展)的方法 —— `routeCdpCommand` switch

| 方法 | playwriter relay 行为 | 我们 `extension_upstream.py` 现状 |
|---|---|---|
| `Browser.getVersion` | 硬编码合成返回 | ✅ daemon 戳合成(line 391) |
| `Target.setAutoAttach`(root) | 返回 `{}` **并合成 `Target.attachedToTarget` for 每个 target** | 🟡 只 silent ack(line 335),**不推 attach 事件** |
| `Target.setDiscoverTargets` | 返回 `{}` **并合成 `Target.targetCreated`** | 🟡 silent ack,**不推 created 事件** |
| `Target.attachToTarget` | 从 `connectedTargets` 合成,返回已存在 sessionId + 合成 attachedToTarget | ✅ 已实现,伪造 `ext-sid-{tab}-{rand}`(line 357) |
| `Target.getTargets` / `getTargetInfo` | 从 relay 的 `connectedTargets` map 答 | ✅ 已伪造 targetInfos(line 340);⚠️ scoped 到 session tab group |
| `Target.createTarget` | 转发(扩展 `chrome.tabs.create`) | 🟡 现 -32601,但有 `openBackgroundTab` 可映射 |
| `Target.closeTarget` | 转发(`chrome.tabs.remove`) | ✅ 有 `closeTab` |
| `Runtime.enable`(带 sessionId) | 转发,**但 await `executionContextCreated` 屏障**(3s)防竞态 | 🟡 直接转发,无屏障 |
| 其余 `Page.*/Network.*/DOM.*/Input.*/Runtime.evaluate` | 默认转发给扩展 | ✅ 经 sessionId→tab 透传 |

## 扩展侧(只有它碰真 CDP)

`chrome.debugger.attach({tabId},'1.3')` → 把 Playwright 来的命令 1:1 映射成 `chrome.debugger.sendCommand({tabId, sessionId?}, method, params)`(background.ts:1046)。MV3 给不了的浏览器级方法**永远到不了扩展**,全由 relay 答。合成 sessionId `pw-tab-{scope}-{n}` 只是稳定 map key,非真 Chrome sessionId。**我们扩展(background.js)用的就是同一套 `chrome.debugger.attach`/`sendCommand`/`onEvent`。**

## 多 tab

每个启用的 tab = 一个独立 CDP target + 合成 sessionId,存 relay 的 `connectedTargets`。Playwright 发 `setAutoAttach` 时 relay 重放所有 target 的 `attachedToTarget` → Playwright `context.pages()` 就能看到每个 tab。

## 与微软 playwright-mcp 关系

无显式署名,但**架构就是 playwright-mcp extension 模式同款**(setAutoAttach 作 bootstrap、attachedToTarget 合成、getTargets 查 map、flat-session 路由 + 扩展 chrome.debugger 当真 CDP 传输)。playwriter 是大幅演化的独立实现。

## 结论:照抄到我们 daemon 的真实 delta

我们 `extension_upstream.py` **已经在用同一种模拟+转发技术**(Target/Browser 模拟、page 域经 chrome.debugger 透传),所以不是"能不能",是补这几块:

1. **【主要】对外加一个 Playwright-facing 的裸 CDP ws 端点 + `/json/version` 发现路由**。现 client 走 unix socket + `BrowserwrightDaemon.*` RPC,没有 Playwright 能连的 `ws://host:port/cdp`。这是最大一块,但是在既有 relay 机制上包一层 facade。
2. **【中】合成 `Target.attachedToTarget`/`targetCreated` 事件流**(`setAutoAttach`/`setDiscoverTargets` 从"只 ack"升级到"ack + 重放所有 target 的 attach 事件")。这是 Playwright 发现 tab 的关键。
3. **【小】`Target.createTarget` 映射到 `openBackgroundTab`**;`getTargets` scope 策略(给 Playwright 连接放开或保持 scoped,需试)。
4. **【小/健壮性】`Runtime.enable` 执行上下文屏障**防竞态。
5. 合成 sessionId:✅ 我们已有 `ext-sid-{tab}-{rand}`。
6. OOPIF/child session 透传:iframe 才需,nice-to-have。

**判定:真 Playwright 跑在 extension 后端(用户日常 Chrome)技术上确定可行,工作量是"加 CDP facade 端点 + 补 target 事件合成",不是换引擎重写。** rdp 后端(daemon 自管 Chrome)更简单,可直接转发真 `Target.*`。
