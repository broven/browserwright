# Multi-Agent Session Model — 问题说明与目标设计

> **状态**:设计已与 owner 对齐,待实现(由后续 Claude Code 会话执行)。
> **原则**:代码是唯一事实来源。本文所有引用均为 `path:line`。其余 `design*.md` 已删除(见 §4),不要再参考。

## 0. 范围与基线准则

- 本工具(`browser-skill` + `browser-daemon`)**仅由 code agent 调用**,owner 不手动使用。
- owner 会在**同一个 worktree 同时跑多个 code agent**。因此身份不能从 cwd 派生,必须显式创建。
- 基线准则:
  - **P1** 多个 code agent 并发用浏览器、互不影响。
  - **P2** 经 Chrome extension 暴露的 CDP 连接 owner 的真实浏览器。
  - **P3** 通用抽象层吸收不同 CDP 暴露方式(extension / 新开 Chrome 的 remote debug port / cloud),下游透明。

---

## 1. 问题说明(现状)

### 1.1 P1 在三层"按机器/用户全局、而非按会话隔离"的可变状态上被违反

| 层 | 全局状态 | 是否按身份隔离 | 证据 |
|---|---|---|---|
| Skill REPL daemon | `$BS_HOME/repl.sock` + `repl.pid` + 共享 `globals_`/`Session` 单例 | ❌ 整机单例 | `browser-skill/src/browser_skill/repl/_proto.py:16-23`、`repl/inline.py:36`、`repl/server.py:174` |
| 默认 daemon 名 | `BD_NAME` 缺省 `"default"`,**import 时定死** | ❌ 两个没设 `BD_NAME` 的会话共用一个 daemon / 同一个 Chrome | `browser-skill/src/browser_skill/mode_b_client.py:39` |
| extension relay 端口 | `DEFAULT_RELAY_PORT = 19989` | ❌ 两个 extension daemon 撞端口 | `browser-daemon/src/browser_daemon/server/relay.py` |
| (daemon 内)extension session 表 | `ExtensionUpstream._sessions` 单一共享 dict | ❌ A 铸的 sessionId 对 B 可见/可 detach | `browser-daemon/src/browser_daemon/server/extension_upstream.py:119` |

### 1.2 已确认的事故(本设计的动机案例)

agent H 想经 extension 操作 owner 日常 Chrome,显式带了 `BD_BACKEND=rdp`。但此前另一个会话(register-machine,`BD_NAME=nst`,env backend 驱动指纹浏览器)跑过 `browser-skill repl start`(`install.py` 还推荐这么做)。机制:

1. REPL daemon 启动时把 `BD_NAME=nst`/backend **冻结**进它的 `Session` 单例(`mode_b_client.py:39` 的 `_DEFAULT_NAME` 在 import 时取值;`repl/server.py:174` 的 `globals_` 只建一次)。
2. agent H 的 heredoc 命中 `is_repl_running()`(`repl/inline.py:36`)被无条件转发;`send_exec` **只发代码、不发 env**(`repl/client.py:59`)。
3. agent H 的 `BD_BACKEND=rdp` 整段被丢弃,代码在 REPL daemon 的冻结命名空间里执行 → 实际驱动了 nst 指纹浏览器。
4. 守卫 `assert_backend_matches`(`mode_b_client.py:218`)**只在进程内路径调用**(`:445`、`:450`),REPL 路径完全绕开 → 无任何报错。

### 1.3 P3 抽象泄漏

- backend 名在 server 层被**字符串比较 6+ 次**:`server/listener.py:106/411/461/564-568`、`server/proxy.py:714-789/837-853/931-935`。
- `BrowserDaemon.getBackendInfo` 硬编码 `"kind":"UPSTREAM_WS"`,extension 下是错的:`server/proxy.py:676-681`。
- `backends/extension.py` 的 `resolve()` 永远抛 `Unavailable`,而 `active_tab.py` 直接调 `resolve()` → extension 下 `active-tab` 必失败(真 bug)。

### 1.4 缺自省 + 误导报错

- **没有任何命令/原语**能回答"我现在连在哪个 backend / 哪个 Chrome / 哪些标签"。`repl status` 只打印 pid+socket(`repl/server.py:223`)。
- extension 上 `Target.createTarget`/`new_tab` 不快速失败,而是返回**误导性的 `-32601 "requires a sessionId"`**(`server/extension_upstream.py:258-262`),把"未实现"和"缺 session"混为一谈。

---

## 2. 目标设计:Session 模型

**核心**:把"隔离单位"从 *daemon/BD_NAME* 挪到 **session**。一个 **Session = 一个 code agent 的浏览器工作空间**,在 extension 场景下物化为同一个 Chrome 内的一个 **tab group**。

### 2.1 生命周期与传播 〔决策 1、3〕

- `browser-skill session new` → 返回 `session_id`(短 token)。
- agent 之后**每次** `browser-skill` 调用都带 `BD_SESSION=<id>`(每个 heredoc 是新进程,必须每次带;不能用 cwd 推断,因为一个 worktree 多 agent)。
- `browser-skill session end` → 关闭该 session 的 group/标签、释放 daemon 侧状态。
- **回收**:显式 `session end` 为主;另加**兜底 reaper**清理僵尸 session(agent 进程跨子进程难探测存活,需基于空闲时间或心跳)。

### 2.2 两种"页面去向"模式 〔决策 2〕

- **group 模式(默认)**:agent 用 `open_background(url)` 开的页面**全部进该 session 专属 tab group**。group **懒创建**(第一次开页面时才建;只用 attach/`http_get` 的 session 不留空 group)。
- **attach 模式(仅当 owner 明确要求"操作我当前页面")**:`attach_active()` 驱动 owner 当前聚焦标签,**并把该当前标签拉进本 session 的 group**。
  - **注意**:attach 拉进来的是 owner 的标签,`session end` 时应**移出 group(ungroup)而非关闭**,区别于 agent 自己开的页面(end 时关闭)。

### 2.3 隔离主键 = `session_id`;`BD_NAME` 退为物理选择 〔决策 4〕

- `session_id` 成为 **skill↔daemon 协议的隔离主键**。`open_background` 现有的 `group=` 参数(`mode_b_client.py:315`)绑定为 `session_id`,daemon 侧**按 session 跟踪标签归属**。
- `ExtensionUpstream._sessions`(`extension_upstream.py:119`)改为**按 session_id 分桶**,跨 session 不可见/不可互相 detach。
- `BD_NAME` 仅表示"哪个物理浏览器/daemon"。**extension 场景:单 daemon 多 session**——一个装了扩展的 Chrome、一个 daemon、N 个 group/session 复用之。

### 2.4 backend 差异 〔决策 5〕

- **extension**:单 daemon 多 session(如上)。
- **rdp / env(含指纹浏览器)**:一个浏览器只服务一个 code agent → **session 与 daemon 1:1**。session 层仍存在(API 统一),但隔离退化为 daemon 本身;无需 tab group 多路复用。

### 2.5 砍掉全局 REPL daemon

- REPL daemon 对 agent 工作流近乎零收益:贵的那条到 Chrome 的 ws 由 Layer-1 `browser-daemon` 持有,REPL 只省 Python import + 跨调用变量保活,而 agent 的 heredoc 都是自包含的。
- **移除**全局 REPL daemon 及 `install.py:578/581/602/626` 对它的推荐。`repl/inline.py` 保留进程内执行路径(`:53`)。
- 若将来确需"保温",必须**按 session_id 命名空间**重做,且 inline 复用前**校验身份、不符则响亮报错**——绝不能再静默转发。

### 2.6 加 `whoami` 自省

- `browser-skill whoami` 输出:当前 `session_id`、backend、物理 Chrome 身份(install_id)、group、归属标签数、一个样本 URL。让 agent 任何时候能确认"我连对了没"。

### 2.7 顺带修(仅限阻塞本设计的抽象泄漏)

- `extension_upstream.py:258-262`:extension 上不支持的 `Target.createTarget` 等应**快速失败并指路**(用 `open_background` 进 session group),不要返回误导的 `-32601 "requires a sessionId"`。
- `proxy.py:676-681`:`getBackendInfo` 的 `kind` 按真实 backend 返回(`whoami` 依赖它)。
- `backends/extension.py` / `active_tab.py`:`active-tab` 在 extension 下走 relay 路径,不要调用恒抛的 `resolve()`。

---

## 3. 实现触点(file:line 地图)

**browser-skill**
- `repl/inline.py:36` — 删除"命中全局 REPL daemon 即转发"的分支(或改为按 session 校验)。
- `repl/_proto.py:16-23`、`repl/server.py`、`repl/client.py`、`cli.py:86` — 移除全局 REPL daemon(或按 session 命名空间)。
- `mode_b_client.py:39` — `BD_NAME` 不在 import 时定死;引入 `BD_SESSION` 读取。
- `mode_b_client.py:218/445/450`、`errors.py:58` — 身份/backend 校验扩展到所有路径。
- `mode_b_client.py:315`(`open_background` 的 `group`)— 绑定为 `session_id`。
- `session.py`、`multitask.py` — session 概念落地;跨进程身份靠 `BD_SESSION`。
- `cli.py` — 新增 `session new|end`、`whoami` 子命令。
- `install.py:578/581/602/626` — 删除 `repl start` 推荐。

**browser-daemon**
- `server/extension_upstream.py:119`(`_sessions`)、`:71-75`(sessionId 铸造)、`:258-262`(createTarget 报错)、`:44-51`/`:241`(未实现列表)。
- `server/relay.py`(`DEFAULT_RELAY_PORT`、`ext.tabs`、`attach_tab :255-296`、`attach_active_tab :214-253`、`_pick_active_extension :395-399`)— 按 session 跟踪标签/group;relay 端口按 daemon 隔离。
- `server/proxy.py:676-681`(getBackendInfo kind)、`:721-965`(attach/open/close handlers — 加 session 维度)。
- `server/listener.py:106/411/461/564-568`、`server/state.py`(per-client/per-session 状态)。
- `backends/extension.py`、`active_tab.py`(resolve 死路)。
- Chrome 扩展:`chrome-extension/background.js:39`(relay ws)、`:413-424`(sendCommand)、`:50-64`(install_ids)、`:91-96`(重连重报标签)、`:229/:252/:307`(attach/createTab)、tab group API 接线。

---

## 4. 删除清单(AI-slop / 已被代码取代,删)

- `browser-daemon/design.md`
- `browser-daemon/design-v2.md`
- `browser-daemon/design-review.md`
- `browser-skill/design.md`
- `docs/plans/` 下全部(均为已实现功能的 AI 计划/交接稿)

**保留(load-bearing,勿删)**:`skill/SKILL.md`、`skill/memory.md`、`browser-skill/SKILL.md`、`browser-skill/ONBOARDING.md`、`src/.../site_skills_starter/*/SKILL.md|memory.md`、各 `README.md`、`browser-connection.md`(Chrome CDP 现场笔记,有价值,后续可 trim)。

---

## 5. 不在本期范围

- daemon 内 `state.py` 100 帧 pre-open 缓冲无背压、超限静默丢帧(`PRE_OPEN_BUFFER_LIMIT`)。
- rdp/env 的多 session 复用(本期 1:1 即可)。
- server 层全部 6+ 处 backend 字符串分支的彻底抽象化(本期只修阻塞 session 模型的那几处)。
