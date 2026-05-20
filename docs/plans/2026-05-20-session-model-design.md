# Session 模型 — 设计（2026-05-20）

> **状态**:已与 owner 在 brainstorming 中逐节对齐,待实现。
> **原则**:代码是唯一事实来源,所有引用均为 `path:line`。
> **取代**:本文是 `docs/session-model.md` 的演进版(短自增 id、create/attach 两轴、
> 创建显式 / 使用透明、决策记忆层)。session-model.md 保留作问题分析背景。

## 0. 目标准则

本工具(`browser-skill` + `browser-daemon`)**仅由 code agent 调用**,owner 不手动使用;
owner 会在**同一 worktree 同时跑多个 code agent**。两条北极星:

- **P1** 多个 code agent 并发用浏览器、互不影响。
- **P3** 通用抽象层吸收不同 CDP 暴露方式(extension / 新开 Chrome 的 remote debug
  port / cloud),**使用时**对下游透明。

关键澄清:**P3 的"透明"是使用时属性,不是创建时属性。** session 一旦建好,所有操作
(`new_page` / `attach_active` / 点击 / 截图 ……)与后端无关;但**创建时**必须由 agent
显式选择后端/方式("用谁的浏览器")。

---

## 1. 核心:统一 Session 模型

**一个 Session = 一个 code agent 的浏览器工作空间**,是 P1 的隔离主键。

### 1.1 句柄与生命周期

- `browser-skill session new [--name <label>]` → 返回**短自增 id**(从 `1` 起,省 token)。
  id 是**不透明句柄**;`name` 可选,仅作 group 的**显示标签**(extension 侧),与句柄是两回事。
- agent 之后**每次**调用带 `--session <id>`(heredoc 是新进程,必须每次带;不能用 cwd
  推断——一个 worktree 多 agent)。
- **取消 default session**:不带 id → 响亮报错 `"no session; run browser-skill
  session new first"`,无静默回退。直接干掉 `mode_b_client.py:39` 的 `_DEFAULT_NAME
  = os.environ.get("BD_NAME", "default")` 这个 import 期冻结的事故源。
- `browser-skill session end --session <id>` → 按 §1.3 ownership 规则清理。

### 1.2 两个正交层级

| 层级 | 动作 | 语义 |
|---|---|---|
| 浏览器/工作区(建 session 时,**显式**) | `create` / `attach` | create=我们拉起浏览器并**拥有**;attach=绑到已有浏览器、**借用** |
| 页面(session 内,**两端一致**) | `new_page` / `attach_active` | 在本工作区开新页 / 把当前聚焦页拉进本工作区 |

三后端归一:

| 后端 | 工作区物化 | 浏览器层级 |
|---|---|---|
| Extension | 一个 tab group | 永远 attach 日常 Chrome(从不杀);group 由我们 create(end 时清理) |
| RDP-隔离 Chrome | 一个独立浏览器 | create(`launch-chrome`),end 时关 |
| RDP-指纹浏览器 | 那个浏览器实例 | create(按 memory 配方启动)或 attach(用户已起好) |

### 1.3 ownership / end 规则

由 create/attach 直接决定,不再单独定"杀不杀":

- **create 出来的** → 本 session 拥有 → `session end` 时关掉浏览器。
- **attach 上去的** → 借用 → `session end` **不关**,但**输出一条提醒**给 agent:
  "浏览器 X 仍在运行(attach、非本 session 拥有),如不再需要请自行关闭"。关不关由
  agent/用户决定;我们只尽**提醒义务**。
- Extension:group 是 create 的 → end 时**关掉 agent 开的 tab、把 `attach_active`
  拉进来的用户 tab 移出 group(ungroup 不关)**;Chrome 本身 attach → 永不杀。

---

## 2. daemon 拓扑 + session 注册表(Option A)

### 2.1 拓扑

| 后端 | daemon | 隔离方式 |
|---|---|---|
| Extension | **一个共享 daemon**(已有 `serve --backend extension`,常驻) | 按 group 分桶:`extension_upstream.py:119` 的 `_sessions`、relay 侧 tab 归属都改成 **per-session-id**(今天是全局共享 dict → P1 的洞) |
| RDP | **每 session 一个 daemon**(session ↔ 浏览器 ↔ daemon 1:1) | 天然隔离:复用今天"一个 daemon = 一个 upstream Chrome"(`_UpstreamHolder` 单 upstream),daemon 几乎不改 |

唯一新增的 daemon 侧工作量集中在 **extension 的 per-session 分桶**;RDP 侧 `session new`
按需 launch 浏览器 + 起 daemon。(对比被否决的 Option B:单一全能 daemon 多路复用所有
upstream——要把 `_UpstreamHolder` 重写成多 upstream,且一个进程崩了全挂,爆炸半径大。)

### 2.2 注册表(ledger)

skill 只拿到 `--session <id>` 时,靠注册表解析到后端/daemon/工作区。

- 位置:`~/.browser-skill/sessions/`,**带文件锁**。
- `session new`:原子分配自增 id(计数器存表内,文件锁防并发撞号),写一条
  `{id, backend, daemon_endpoint, workspace(group_id 或 browser_ws), owner: create|attach,
  name, created_at, last_seen}`。
- 每次 skill 调用:用 id 查表 → daemon socket + workspace → 连上去。**后端差异在此被吸收**
  (P3 使用时透明)。
- `session end`:清理后删条目。
- id **单调、ledger 存续期间不复用**(避免 stale 句柄歧义);无活跃 session 时可重置计数。

---

## 3. 创建显式、使用透明、记忆驱动决策

### 3.1 `session new` 创建面(显式三模式,未来可加 cloud)

- `session new --backend extension` → attach 日常 Chrome + 建 group。
- `session new --backend rdp --create` → `launch-chrome` 起隔离 Chrome(create+拥有)。
- `session new --backend rdp --attach <recipe|port>` → 挂到已有浏览器(指纹浏览器;
  attach+借用,end 提醒)。

### 3.2 Skill Memory 决策层(新)

- skill 维护一块 **session 决策记忆**,随使用积累 case:"什么情况用什么方式起 session"
  (含指纹浏览器的启动配方/端口)。
- 交互模式:**命中自动、未命中问并记** —— agent 先查记忆,命中就按记忆自动起;没命中就
  问用户(用哪个浏览器/怎么起),再把决策写回记忆,下次自动复用。

### 3.3 内部抽象范围(实现细节,非大决策)

- 新增一个小 **Backend 能力接口**,只覆盖 session 真正用到的动作:`workspace_create/attach`、
  `page_new` / `page_attach_active`、`caps()`(如 `supports_browser_context`、`owns_browser`)。
  extension / rdp 各实现一份。
- **顺手修 3 个阻塞性 bug/泄漏**:
  1. `proxy.py:676-681` `getBackendInfo` 的 `kind` 硬编码 `UPSTREAM_WS` → 按真实后端返回
     (`whoami` 依赖)。
  2. `backends/extension.py` 的 `resolve()` 恒抛 `Unavailable`,而 `active_tab.py` 直接调它
     → extension 下 `active-tab` 必失败 → 改走 relay 路径。
  3. `extension_upstream.py:258-262`:extension 不支持的 `Target.createTarget` 等应**快速失败
     并指路** `new_page`,别再返回误导的 `-32601 "requires a sessionId"`。
- **不做**:把 server 层 6+ 处 backend 字符串分支(`listener.py:106/411/461`、`proxy.py`)
  彻底多态化。YAGNI,留后续。

---

## 4. 回收 / 自省 / 砍 REPL daemon

- **回收僵尸 session**(agent 没 `session end` 就死):ledger 每条带 `last_seen`,每次调用刷新;
  **兜底 reaper** 按空闲超时回收(超时给宽,如数小时,避免误杀正常停顿的 agent)。显式
  `session end` 仍是主路径。
- **手动管理**:`session list`(读 ledger)、`session prune`(清僵尸)。
- **自省**:`whoami --session <id>` 输出 `{id, backend, 物理 Chrome 身份, group/browser,
  归属 tab 数, 样本 URL}`,让 agent 随时确认"连对没"。
- **砍掉全局 REPL daemon**:移除跨进程 REPL daemon 及——
  - `repl/inline.py:36` 的"命中即静默转发"分支;
  - `repl/client.py:59` 的"只发代码不发 env";
  - `repl/server.py:174` 的 `globals_` 单次冻结;
  - `install.py:578/581/602/626` 对 `repl start` 的推荐。
  进程内执行路径(单个 heredoc 内)保留。session id 提供**浏览器状态**(tab/group)的连续性,
  不再保活 Python 变量(agent 工作流不需要后者)。

---

## 5. 实现触点(file:line 地图)

**browser-skill**
- `mode_b_client.py:39` — 删 `_DEFAULT_NAME` import 期冻结;引入 `--session`/`BD_SESSION`。
- `mode_b_client.py:218/445/450`、`errors.py` — 身份/backend 校验扩展到所有路径(含原 REPL 路径)。
- `mode_b_client.py:315`(`open_background` 的 `group=`)— 绑定为 session 的 workspace。
- `repl/inline.py:36`、`repl/client.py:59`、`repl/server.py:174`、`cli.py` — 移除全局 REPL daemon。
- `cli.py` — 新增 `session new|end|list|prune`、`whoami` 子命令。
- 新增 session 注册表读写 + 文件锁 + 自增计数器;session 决策记忆读写。
- `install.py:578/581/602/626` — 删 `repl start` 推荐。

**browser-daemon**
- `server/extension_upstream.py:119`(`_sessions` → per-session-id 分桶)、`:258-262`(createTarget 报错)。
- `server/relay.py:310`(`create_background_tab` 的 `group_name`)— 按 session 跟踪 tab/group;
  `attach_active_tab` 拉进来的 owner tab 标记为"借用,end 时 ungroup 不关"。
- `server/proxy.py:676-681`(getBackendInfo kind)。
- `backends/extension.py`、`active_tab.py`(resolve 死路 → relay 路径)。
- 新增小 Backend 能力接口(workspace/page/caps),extension 与 rdp 各实现。

---

## 6. 不在本期范围

- daemon 内 `state.py` pre-open 缓冲无背压(`PRE_OPEN_BUFFER_LIMIT`)。
- server 层全部 backend 字符串分支的彻底多态化(只修 §3.3 那 3 处)。
- cloud 后端的创建模式(本期先 extension + rdp 两类)。
