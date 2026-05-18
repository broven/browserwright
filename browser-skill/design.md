# browser-skill — Design

本文是 Layer 2 (Skill) 的开发者视角设计文档。三层架构里：

- **Layer 1 (Daemon)** — 按 Layer 2 提出的需求，提供 CDP WebSocket + 一组浏览器进程级原语。Skill 是它的客户，**Skill 的体验是 daemon 的 KPI**。daemon 实现细节见 `../browser-daemon/design.md` / `design-v2.md`。
- **Layer 2 (Skill, 本文档)** — 教 AI Agent 怎么用浏览器：REPL + 站点固化层 + 记忆。**设计主导方**：先定 Skill 体验，反推 Daemon 需求。
- **Layer 3 (Tasks)** — 跨站点 / 定时 / 编排，构建在 Skill 之上，不在本文档范围。

**设计姿态（重要）**：本文档不被"现有 daemon 设计"约束。Skill 先想清楚自己想长什么样、想给 agent 什么体感，**然后**列出"为此 daemon 需要提供什么"作为契约清单 (§D)。任何"现有 daemon 不易做"的需求，daemon 自己改。

---

## Executive Summary

Skill 层最关键的 3 个设计决定：

1. **双形态 REPL：inline heredoc + long-lived repl daemon。** Heredoc 沿用 browser-harness 的"零仪式" UX，agent 直接 `browser-skill <<'PY' ... PY`；同一进程里也可以 `browser-skill repl start` 起一个持久守护，把 CDP ws 长连 hold 住，让多次 heredoc 调用共享同一个 ws session。这是 §F 给出的"Daemon Mode A 每次新 ws 都触发 Allow popup"硬约束下唯一无打扰的姿势。

2. **站点固化层 = 纯 Python 模块 + 同目录 markdown memory，目录就是合约。** 每个站点一个目录 `site-skills/<host-stem>/`，里面 `tasks/*.py` 是可调用脚本（带 `ARGS` / `OUTPUT` / `selftest()` 元数据），`SKILL.md` 是站点地图，`memory.md` 是站点级累积记忆。不发明 DSL、不用 YAML——直接复用 REPL 验证过的 Python 代码，把"probe 出来的"变成"固化的"成本接近零。

3. **三个 memory store 分层，由读取时机决定写入时机。** 全局 `~/.browser-skill/global.md`（用户偏好、跨站点别名）→ 进程启动加载一次；站点级 `site-skills/<site>/memory.md` → `goto_url(host)` 命中后加载；REPL 临时上下文 → 仅当前进程。Agent **不**自动写入——只在 (a) 用户明确说"记住这个" / (b) 命中"非显然发现"（私有 API、稳定 selector、URL 陷阱）这两种触发时调 `remember()`，写入 append-only，rewrite 需用户确认。

**反推到 Daemon 的需求**（§D 详述）：
- subprocess url resolver + 零副作用 doctor + 稳定 JSON shape（v0.1 硬需求）
- 标准 CDP 透传 + sessionId 多路 + 单 target 单 attacher 规则（v0.2 硬需求）
- **`BrowserDaemon.getActiveTab` / `subscribeFocus` 非标准命名空间**——REPL 跟 user 共用 Chrome 的核心 ergonomics（v0.2 硬需求，无替代）
- **`browser-daemon launch-chrome` 子命令**前置到 v0.1——install 流程没它没法引导用户走"隔离 profile 无打扰"路径
- daemon 不做应用层（截图缓存 / 重试 / cookies / attach 决策）

最大的 unknown：

- **probe → 固化的精确触发与质量门控**。我们想做"agent 跑完 REPL 后说 'save'，然后 scaffolder 把工作代码提取成 `tasks/<name>.py`"，但 REPL 的实际操作里夹杂了大量探查（截图、试错、scroll、错误的 click），如何从 N 步里挑出真正"对结果有贡献"的 M 步是开放问题。v0.1 退化为：agent **手写**脚本框架，scaffolder 只提供模板；v0.2 才尝试自动提炼。

其它已知未解但**不**致命的问题列在 §11 开放问题。

---

## 0. User Stories（验收标准）

下面 4 个 user story 是 Skill v0.1 必须**完整、自然**承载的场景。每个 story 后面列出它对设计的影响 + 落在哪个章节。修订后整篇 design 反复 self-check 这 4 条。

### US1 — 当前页面 one-shot

> 用户对 agent 说"帮我填一下现在这个表单" / "把这页评论给我读一下"。Agent 调 Skill → Skill 拿到**用户视觉前台的 tab** → 直接操作，**不**开新 tab，**不**问用户在哪个 tab。

**对设计的影响**：
- §A.2 新增 `current_page()` 原语：返回 **user 视觉前台 tab** 的 attach handle（不是 CDP 最后 activated）。
- §D.2.8 `BrowserDaemon.getActiveTab` 是 daemon 必须提供的能力（已在硬需求清单；本轮 patch 加 `accuracy` 字段：v0.1 = `heuristic-recent-activate`，stale 时 Skill 降级 + warn agent）。
- §A.1 inline heredoc 在 autoconnect+ModeA 下会反复弹窗——US1 这种"帮我处理这页"是高频小任务，被弹窗砸非常痛。**已有缓解**：install wizard 检测 autoconnect → 主动建议 `browser-skill repl start`，之后 inline heredoc 复用同 ws，零摩擦。Mode B v0.2 上线后自动消解。**不**前推 Mode B 到 v0.1（与 daemon-architect 协商一致）。

### US2 — 新 tab one-shot + 执行中创建站点 memory

> 用户对 agent 说"看看 Product Hunt 今日有什么新产品"。Skill 开新 tab → ProductHunt → 提取信息。过程中发现 "今日榜单的 voted 数字在 hover 时才显示" 这种与预期不一样的事实 → **当场**写进 `site-skills/producthunt/memory.md`，下次任何 agent 跑这站点都能读到。

**对设计的影响**：
- §C.3 写入时机表明确加一行 "**执行流任意时刻**，agent 命中非显然事实 → `remember(site, '...')`"。这是 in-flight write，不是 post-task 沉淀。
- §C.3 新加规则：**站点目录 lazy-create**——`remember(site, ...)` 若 `site-skills/<site>/` 不存在，自动 scaffold 空 `memory.md`（含 frontmatter）+ stub `SKILL.md`。Agent 不需要先 `bootstrap_site()`。
- §A.2 新增 `bootstrap_site(host)` 显式原语（给 agent 在 explore-only 时主动建目录用）；`remember()` 内部也调它。
- §C.3 redaction 规则在 in-flight 仍然成立——`remember()` 是 append-only + 高熵 / Bearer / token 正则检查，**不**因为 "执行中" 放宽。
- v0.1 "wider event stream"（console / network failure 跨 tab）**不**承诺；US2 v0.1 范围 = 当前 attached tab 的事件。Mode B v0.2 上线后通过标准 CDP `Target.setAutoAttach` 拿全量 events（daemon-architect 已确认 daemon 侧零变更）。

### US3 — One-shot 完成后的固化询问

> One-shot 跑完，Skill 判断 "这看起来是个可复用任务"（参数化清晰、无一次性副作用、有重复可能）→ 通过 agent **向用户 surface 一个确认**："要不要把这个保存成 `<site>/<name>` 任务？" 用户同意 → Skill 用 REPL history 填模板 → user review → commit。

**对设计的影响**：
- §B 新增 §B.4.0 "Solidify-readiness 启发式"：明确判定清单（参数化程度、副作用类型、auth 依赖、可重现性）。
- §B 新增 §B.4.1 "Solidify 询问协议"：明确 surface 路径 = agent 在自己的对话流里问用户（Skill **不**自己跟用户对话；Skill 只通过 `propose_solidify()` 原语返回结构化建议给 agent，agent 决定话术）。
- §B.4 Stage 2 scaffolder 提前到 v0.1 必须有最小可用版本——不能再退到 v0.2。**实现策略**：scaffolder 从 REPL daemon 取 success-only 代码片段（按 exec 成功 + 无异常过滤），填进 `tasks/<name>.py` 模板的 `run()` 函数体，**agent 自动整理**（裁剪试错 step + 加 `selftest()`），用户最终 review/approve。不是用户手填代码。
- §11 开放问题第一条 "probe → 固化精度" **前移到 v0.1**：必须给个最小可用提炼策略。粗糙允许，但不能没有。

### US4 — Daemon backend 偏好写入 global skill memory

> 用户对 agent 说"用我的浏览器插件连接 Chrome" / "改用 autoconnect"。Agent 解析意图 → 询问用户确认 → 写入 `~/.browser-skill/global.md` 的 `daemon` 块。下次任何 Skill 进程启动 → 读 global → 自动 `browser-daemon url --backend <preferred>`。

**对设计的影响**：
- §C.2 global.md schema 扩展：frontmatter 加 `daemon:` 块（`preferred_backend` / `notes` / `set_by_user_at`，**plus v0.5 cloud_* keys** — see §C.2 schema）。<sub>v0.5 REVIEW.md F-8: `fallback_chain` 从 schema 里 retract — daemon-impl-2 F-5 已确认 daemon 端不再尊重它（fallback 现在走显式 BD_BACKEND env / 配置）。原始设计意图请见 §11 开放问题历史条目。</sub>
- §C.3 新增写入触发："**用户直接指令** Skill 行为偏好"——区别于 US2 的 "agent 命中非显然事实"。这种写入**必须** user confirm 一次（不能 agent 自决），confirm 后写入。
- §C.3 新增读取触发："Skill 进程启动时读 `daemon` 块 → 传 `--backend` 给 `browser-daemon url` subprocess (Mode A) 或调对应 Mode B endpoint"。
- §C.3 冲突解决规则：CLI env > CLI `--backend` flag > global.md preference > daemon `recommended`。冲突时 prompt agent "用户当次显式指定的优先；要不要更新 global preference？"。
- §C.3 redaction 注意：`daemon.notes` 段不要写 Chrome user-data-dir 绝对路径（可能含用户名）；只写相对位置 / 描述（"我的工作 profile"）。
- §E 末尾加一行 "用户问 '我之前是怎么连的浏览器？' → agent 读 `global.md` 的 `daemon` 块回答"。
- §D 加一行 "Skill 把 backend 偏好通过 `--backend` flag 传给 daemon；daemon 侧**零新增**（flag 已支持）" —— daemon-architect 确认 US4 daemon 工作量为零。

### US3R — Persistent REPL session（post-v0.1 折叠回 §0）

> 用户对 agent 说"先开个浏览器我接下来给你一连串任务" / 长流多轮调试。Agent 启动 `browser-skill repl start` → 长驻 Skill daemon hold 住 CDP ws → 后续每条 inline heredoc 复用同 ws session，零 popup 累积。

**对设计的影响**：
- §A.1 三形态表的第二行（`repl start`）就是 US3R 的 happy path。**REVIEW.md F-5c**: 这条 story v0.1 design 没显式列，是 popup-cost 防御（spec H1 / P0 #75）的承载场景，应在 §0 落座，让任何后续 agent 直接看到。
- v0.3 inline heredoc abort gate（spec §A.1 footnote）依赖 US3R 作为"安全替代路径"：abort error 引导 user 跑 `repl start`。
- US3R 没有新原语 — REPL daemon 是 §A.1 已有的 `browser-skill repl {start,stop,status,exec}` 子命令组合。

### US5 — Cloud / remote-browser backend（v0.5）

> 用户对 agent 说"用 Browser Use / Browserless 跑这个任务"（云端 Chrome 服务）。Agent 走 install wizard option 5 → 选 provider + auth_kind → daemon 持有 AuthProvider（Bearer / Basic / mTLS）→ Skill 完全透明使用。

**对设计的影响**：
- §A.1 backend 表第 5 行（v0.5）就是 US5 的落地。
- §C.2 global.md `daemon:` 块扩 cloud_provider_hint / cloud_endpoint / cloud_auth_kind / cloud_token_env / cloud_username_env / cloud_password_env / cloud_cert_file / cloud_key_file —— 全部是 credential **引用** 不是 secret 本身。
- daemon-side cloud backend (`browser-daemon 0.5.0`) 持有 `AuthProvider` 抽象；Skill 只调 `install` wizard + 写 `~/.config/browser-daemon/config.toml` `[backends.cloud]` + `[backends.cloud.auth.<kind>]` 两段。
- spec H3 doctor-as-contract 保证 cloud backend 的 availability 检测零 ws 副作用；install wizard option 5 的 live/coming label 完全 doctor-driven。

### 6 条 US 横向映射

| US | 主要章节 | 新增原语 | Daemon 侧需求 |
|---|---|---|---|
| US1 | §A.1 / §A.2 / §D.2.8 | `current_page()` | `getActiveTab` (硬, v0.1 heuristic) |
| US2 | §A.2 / §C.3 | `bootstrap_site(host)` | 零（Mode B 标准 CDP 在 v0.2 自然解锁 wider events） |
| US3 | §B.4 / §B.4.0 / §B.4.1 | `propose_solidify()` | 零 |
| US3R | §A.1 三形态表 | （现有 `repl start/stop/exec`） | 零 |
| US4 | §C.2 / §C.3 | （走现有 `remember_global`） | 零（`--backend` 已支持） |
| US5 | §A.1 / §C.2 | （install wizard option 5） | cloud backend + AuthProvider (v0.5) |

---

## 1. 目标 / 非目标

### 目标

1. 给 AI Agent 一个最小的浏览器原语集合，能完成 one-shot 任务（"帮我刷一下今天的新评论"）。
2. 把 agent 在 one-shot 里探出来的工作姿势**就地固化**成可复用脚本，下次同类请求秒级命中。
3. 通过站点目录 + 索引 + memory 的组合，让"自然语言请求 → 正确脚本"的查找是 deterministic + LLM-fuzzy 双路。
4. 与 Layer 1 (Daemon) **进程级解耦**——Skill 不 import daemon 包，只走 subprocess (Mode A) 或 socket (Mode B)。
5. 内核稳定、外围 hackable——core primitives 守护，interaction-skills/ + site-skills/ 由 agent 边用边写。
6. **REPL 与 user 共用 Chrome 时不打扰对方**——agent 知道用户当前在看哪个 tab、不抢占、不clob 用户工作。这条目标直接驱动 §D 对 daemon `BrowserDaemon.getActiveTab` 的硬需求。

### 非目标

- **不实现 Layer 1**。如何拿到 CDP ws、如何启动 Chrome、如何处理弹窗、是 daemon 的事。Skill 通过 §D 的契约**要求**daemon 提供具体能力，至于 daemon 怎么实现不管。
- **不做 LLM 调度 / planning / tool-use 框架**。Agent 自己是 Claude/GPT/Gemini，Skill 给它工具不给它脑子。
- **不做定时调度**。`cron`、并发、跨站点编排是 Layer 3 的事。Skill 暴露被调用的接口，不主动管时间。
- **不做无浏览器 fallback**。仅 `http_get()` 一条裸 HTTP 出口；想要 headless / API-only 的请走别的栈。
- **不做账号管理**。Skill 不存密码、不替用户登录、不替用户填验证码。命中 auth wall / CAPTCHA → 抛异常给 agent，由 agent 决定是问用户还是放弃。
- **不做截图理解 / OCR / element-AI**。Skill 把截图交给 agent，agent 自己看像素出坐标。

### 设计顺序

写本文档时严格按以下顺序展开，保证 Skill 体验先行、daemon 跟随：

1. 先定 REPL 体感 (§A) → 决定了"长连 vs 短连"、"哪些原语必须有"、"错误怎么呈现"。
2. 再定固化层工作流 (§B) → 决定了"task 怎么调用 daemon"、"selftest 需要哪些查询"、"task 间并发模型"。
3. 再定 memory (§C) → 不依赖 daemon。
4. **由 §A/§B/§C 反推**出 daemon 必须提供的能力清单 (§D)，分硬/软分级，每条绑到上面具体场景。
5. 可发现性 (§E) 和实测约束总结 (§F) 是辅助。

---

## 2. 实测约束 / Daemon 反向影响（决定一切）

Skill 的几乎所有架构选择都来自 §2 of `../browser-daemon/design.md` 的实测。摘要：

- **autoconnect 路径下，每次 browser-level WS 握手都触发 Chrome "Allow remote debugging?"弹窗**。Chrome 144+ 完全无记忆。
- **横幅与 ws 严格同步**：ws 一旦 OPEN，"Chrome is being controlled..." 横幅立即出现；最后一个 ws close 后立即消失。横幅的 X dismiss 是 per-WS 持久。
- **唯一无打扰路径**是 `rdp + 独立 user-data-dir`——这是后台 Chrome，用户看不到横幅。所有"连用户日常 Chrome"的路径都至少有横幅、autoconnect 还多一次弹窗。

这些约束**直接逼出**了 Skill 层四个硬性反模式：

| 反模式 | 为什么不能 | Skill 怎么处理 |
|---|---|---|
| **高频短连（每次操作起新 ws）** | autoconnect 每次弹窗；其它 backend 反复显示/隐藏横幅 | REPL 默认 long-lived 模式；heredoc 调用走 daemon Mode B socket 复用上游 ws |
| **多 client 同时 attach 同 browser** | daemon Mode B v0.2 明确禁止；浪费横幅"是否可见"的隐私 budget | Skill 全局单例 ws；并行 sub-agents 用 Browser Use cloud（独立 BU_NAME） |
| **轮询式 `js("document.readyState")` 每秒 5 次** | 不致命但烧 CDP 带宽，会让 Network.* 事件队列拥塞 | `wait_for_load` 默认 0.3s 间隔；网络空闲检查走 drain_events 累积 |
| **opportunistic probe ws**（"先试试通不通"） | 多余一次握手 = 多一次弹窗 + 一次横幅闪 | doctor / install 流程**不**默认 probe ws；显式 `--probe-ws` 才连 |

**Daemon Mode A vs Mode B 在 Skill 层的体感差异**：

| 维度 | Mode A (subprocess + Skill 自管 ws) | Mode B (Skill 连 daemon socket) |
|---|---|---|
| heredoc 一次调用 | 每次 fork ws → 每次弹窗（autoconnect 致命） | daemon 复用上游 ws，零摩擦 |
| `repl start` 长驻 | 一次性弹窗 + 横幅，之后无打扰 | 一样 |
| Skill 进程崩溃恢复 | 重新弹窗 | daemon 兜底，Skill 重连 socket 无感 |
| 多 task 并发（v0.3 Layer 3） | Skill 自己得做 multiplex，难 | daemon 已经替你做了 sessionId fanout |

→ Skill v0.1 **默认假设 Mode A + repl 长驻**，inline heredoc 在 Mode A 下文档明确警告"会反复弹窗，请改用 `repl start`"。v0.2 跟 Daemon Mode B 上线后，inline heredoc 变成首选。

---

## 3. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  Agent (Claude Code / Codex / 自家 SDK)                       │
│  发自然语言指令；调 browser-skill CLI；读 SKILL.md 学姿势       │
└────────────┬────────────────────────────────────────────────┘
             │
             │   CLI subprocess  ◀── 三个入口：
             │                       browser-skill <<'PY' ... PY  (inline)
             │                       browser-skill repl start       (long-lived)
             │                       browser-skill task <site>/<name> --args=...
             ▼
┌─────────────────────────────────────────────────────────────┐
│  browser-skill 进程                                            │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ exec layer                                              │  │
│  │  - inline: 直接 exec(stdin) 在带 helpers 的 namespace 里 │  │
│  │  - repl: unix socket server,接收 {code} JSON 消息       │  │
│  │  - task: 加载 site-skills/<site>/tasks/<name>.py 调 run│  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ core primitives (browser_skill/core/)                   │  │
│  │   nav / input / visual / eval / wait / events / http    │  │
│  │   memory_read / remember / remember_global              │  │
│  └────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ daemon client (browser_skill/daemon_client.py)          │  │
│  │   - ModeAClient: subprocess("browser-daemon url") + ws  │  │
│  │   - ModeBClient: connect ws+unix:///tmp/...sock         │  │
│  │   - 切换由 BS_DAEMON_MODE / config 决定                  │  │
│  └────────────┬────────────────────────────────────────────┘  │
│  ┌──────────▼─────────────────────────────────────────────┐  │
│  │ cdp client (cdp-use 或自卷)                              │  │
│  │  - 单根 ws，sessionId multiplex                          │  │
│  │  - event ring buffer → drain_events()                   │  │
│  └────────────────────────────────────────────────────────┘  │
└─────┬────────────────────────────────────────┬──────────────┘
      │ ws / unix socket                       │ filesystem
      ▼                                        ▼
┌─────────────────┐               ┌───────────────────────────┐
│  browser-daemon │               │  $BS_HOME (default ~/.browser-skill) │
│  Mode A or B    │               │   global.md                          │
└─────────────────┘               │   site-skills/<site>/                │
                                  │     SKILL.md / memory.md             │
                                  │     tasks/*.py                       │
                                  │   interaction-skills/<topic>.md      │
                                  │   index.json                         │
                                  └──────────────────────────────────────┘
```

**包布局**：

```
browser-skill/
├── pyproject.toml
├── README.md            # 给 user 看
├── SKILL.md             # 给 Agent 看（@-import 进 ~/.claude/CLAUDE.md）
├── design.md            # 本文档
└── src/browser_skill/
    ├── __init__.py
    ├── cli.py            # argparse + 子命令 dispatch
    ├── exec.py           # inline / repl / task 三个入口共用的 namespace 装配
    ├── repl_server.py    # repl start 的 unix socket server
    ├── daemon_client.py  # Mode A / B 客户端
    ├── cdp.py            # CDP client（基于 cdp-use）
    ├── errors.py         # BrowserSkillError 体系
    ├── memory.py         # global + site memory R/W
    ├── discovery.py      # site 匹配、index.json 维护
    ├── scaffold.py       # save-task / new-site 模板生成
    ├── doctor.py         # browser-skill doctor 子命令
    └── core/
        ├── __init__.py
        ├── nav.py        # goto_url / new_tab / switch_tab / ...
        ├── input.py      # click_at_xy / fill_input / press_key / ...
        ├── visual.py     # capture_screenshot / page_info
        ├── eval.py       # cdp / js
        ├── wait.py       # wait_for_load / wait_for_element / wait_for_network_idle
        ├── events.py     # drain_events
        ├── http.py       # http_get
        ├── dialog.py     # handle_dialog / page_dialog 等
        └── iframe.py     # iframe_target
└── interaction-skills/    # markdown，跟 browser-harness 一致
└── site-skills/           # markdown + Python，bundled 起步集；用户/agent 扩展
└── tests/
```

---

## A. REPL 设计

### A.1 三个调用形态

| 形态 | 命令 | 生命周期 | 何时用 |
|---|---|---|---|
| **inline heredoc** | `browser-skill <<'PY' ... PY` | 一次性，进程退出 | Agent 一发就走的 ad-hoc 操作；one-shot |
| **repl start** | `browser-skill repl start` → 后台 unix socket | 长驻 daemon | Agent 在一次会话里需要连发多条命令；Mode A 下唯一无打扰姿势 |
| **task 运行** | `browser-skill task <site>/<name> --arg=...` | 一次性 | Agent 知道目标站点和已固化任务名时；最快 |

三者**共享同一个 namespace 装配**（`exec.py`）——core primitives + agent_helpers + 自动加载的 site-skills 模块，区别只是输入源 (stdin vs socket vs file)。

**为什么不用 stdin/stdout JSON-RPC？** 考虑过：

```jsonl
{"id":1,"method":"goto_url","params":{"url":"..."}}
{"id":1,"result":{"frameId":"..."}}
```

但 Agent 通常想发的不是"调一个 method"，而是"跑一段 Python 表达 N 步业务逻辑"：截图 → 看像素 → click → 等 → 提取。把它拆成 N 个 RPC 让 Agent 自己拼装，等于把 Python 解释器从 Skill 进程移到 Agent 进程——agent 进程没有 Python 解释器，只能把 Python 字符串作为 string 发回来，最终还是 `exec()`。我们直接接受这件事，省一层封装。

**inline heredoc 的契约**：

- stdin 是任意 Python 代码片段，在带预导入的 globals 里 `exec()`。
- stdout = 用户 print 的内容（透传给 Agent）。
- stderr = Skill 自己写的诊断（`-v` 时透传）。
- exit code: 0=ok, 1=user error (语法 / args), 2=browser unavailable (daemon 起不来), 3=script raised, 4=auth_wall, 5=captcha。
- 进程结束自动 `drain_events` flush 一次到 stderr（debug 用）。

**repl start 的协议**（unix socket on `/tmp/browser-skill.sock`）：

```
client → server: {"id": 1, "code": "print(page_info())"}
server → client: {"id": 1, "stdout": "...", "stderr": "", "exception": null}
                 {"id": 1, "stdout": "...", "stderr": "", "exception": {"type":"AuthWall","msg":"..."}}
```

`browser-skill exec '<code>'` 是 socket client 的便利封装；Agent 也可以直接连 socket。`browser-skill repl stop` 关守护。

**Auto-suggest `repl start`（v0.1，应对 US1）**：

Inline heredoc 在 autoconnect+ModeA 组合下每次开新 ws → 每次 Allow popup（§F 反模式表第一行）。这对 US1 "帮我填这个表单" 这种高频小任务极其痛。Skill 在 inline 入口启动时做一次检查：

```python
# inline 入口启动时（exec stdin 前）
if backend == "autoconnect" and not repl_daemon_alive():
    # 仅当前进程是 inline（不是 repl daemon 自己）才提示
    if first_run_marker_not_set():
        print("ℹ️  Detected autoconnect backend. Each inline call triggers a Chrome 'Allow remote debugging' popup.", file=sys.stderr)
        print("    Run `browser-skill repl start` once to share a single long-lived connection.", file=sys.stderr)
        set_first_run_marker()
```

这只是提示，**不**强制——用户/agent 可以无视继续 inline。但 `install` wizard (v0.1 必备) 会问得更主动：检测到 autoconnect → 默认推荐 `repl start` 路径并教 user 怎么用。`rdp + 独立 profile` / `env` 路径不触发该提示。

**支持的浏览器源（install wizard 信息）**：

Skill 通过 daemon 的 backend 抽象支持以下浏览器源——Skill 端**零特殊处理**，daemon 提供 backend 抽象后 Skill 全部走统一接口。完整全景表见 `browser-daemon/design-v2.md §4.4`。常见来源：

| 浏览器源 | Daemon backend | 备注 |
|---|---|---|
| 用户日常 Chrome / Chromium | `autoconnect` | 每次 ws 握手弹 Allow；走 `repl start` 长连缓解 |
| `browser-daemon launch-chrome` 起的隔离 profile Chrome | `rdp`（自动检测端口） | install wizard 默认推荐路径，无打扰 |
| 用户自己启动的 `--remote-debugging-port=N` Chrome | `rdp`（`--port N`） | 用户已有自动化 profile 时直接复用 |
| **指纹浏览器**（AdsPower / MultiLogin / GoLogin / 比特浏览器 等） | `rdp`（`--backend rdp --port <你的指纹浏览器配置的端口>`） | 这些浏览器都暴露标准 `--remote-debugging-port` 风格 CDP discovery，跟普通本地 Chrome 没区别 |
| Chrome 扩展 relay（Playwriter 风格） | `extension`（v0.4 占位） | 走扩展权限模型，无 Allow 弹窗也无横幅可避免；v0.4 上 |
| Browser Use 远程云浏览器 | 复用 browser-harness `start_remote_daemon`，外部供给 `BS_CDP_WS` env → daemon `env` backend | 不归 daemon 启动；Skill 透明使用 |
| 已开的任意外部 CDP endpoint | `env`（`BS_CDP_WS=ws://...`） | 兜底；外部已管 ws 时直接复用 |

**指纹浏览器特别注意**：用户多 profile 切换是这类浏览器的核心 use case，每个 profile 通常对应不同 port。Skill v0.1 一次只服务一个 daemon (单 `BD_NAME`)；要切 profile = 让用户在指纹浏览器里启用目标 profile + 把对应 port 写进 `--port`（或 memory `daemon.notes` 备注当前 profile）。**未来增强**（不强求 v0.1）：detection 逻辑识别 user-data-dir 里的 AdsPower / MultiLogin 特征文件 → install wizard 主动提示 "检测到指纹浏览器，要不要绑定 backend rdp + port？"。详见 §11 开放问题。

### A.2 暴露的原语

**这一节直接采纳 browser-harness 的全套原语作为 v0.1 baseline**——这套已经在 96 个 domain-skills 里被实战验证，没必要重造。Skill 的"原创"是 memory 接口（A.5）和 task 加载（B）。

> **v0.5.1 ship status** (REVIEW.md F-4 catch-up): `EXPORTS` 实际暴露 36 个名字。`browser-harness` 全套对应 35 个，其中 **33 已实装** (`type_text` / `press_key` / `fill_input` / `scroll` / `dispatch_key` / `upload_file` / `wait_for_element` / `wait_for_network_idle` / `drain_events` / `ensure_real_tab` / `iframe_target` / `http_get` 等 — 详见 `tests/test_primitives_f4_catchup.py`)。**剩 2 个 deferred v0.6+**：`handle_dialog`（需要 `Page.javascriptDialogOpening` listener + dialog state machine）+ `try_recover_from_drift`（需要 selftest harness + drift heuristic + rollback；spec §B.4 主要 v0.6 feature 之一）。所有 deferred 调用现在 raise `AttributeError` 而非沉默 NameError 是因为它们**未在 EXPORTS** —— 修文档让 agent 不要尝试。

```python
# 导航 / 页面
goto_url(url)               # 在当前 attached tab 导航
new_tab(url="about:blank")  # 创建并切到新 tab
switch_tab(target)          # target = targetId 字符串或 current_tab() dict
list_tabs(include_chrome=True)
current_tab()               # CDP 当前 attached tab（可能是过去 attach 留下的）
current_page()              # ★US1★ 用户视觉前台 tab；走 BrowserDaemon.getActiveTab + auto-switch_tab
ensure_real_tab()           # 跳出 chrome:// 内部页（current_page 的退化版，当 active-tab 不可用时）
iframe_target(url_substr)   # iframe 的 targetId，给 js(..., target_id=...) 用

# 输入
click_at_xy(x, y, button="left", clicks=1)   # 坐标点击（默认）
type_text(text)                              # Input.insertText，bypass 框架监听
press_key(key, modifiers=0)                  # 真实键事件
fill_input(selector, text, clear_first=True) # 框架受控 input 专用
scroll(x, y, dy=-300, dx=0)
dispatch_key(selector, key="Enter", event="keypress")  # DOM keyEvent，框架友好
upload_file(selector, path)                  # DOM.setFileInputFiles

# 视觉
capture_screenshot(path=None, full=False, max_dim=None)
page_info()  # {url,title,w,h,sx,sy,pw,ph}；如有 dialog 返回 {dialog: ...}

# 执行
cdp(method, session_id=None, **params)  # 裸 CDP
js(expression, target_id=None)          # 自动 IIFE 包裹 return

# 等待
wait(seconds=1.0)
wait_for_load(timeout=15.0)
wait_for_element(selector, timeout=10.0, visible=False)
wait_for_network_idle(timeout=10.0, idle_ms=500)

# 事件 / 网络
drain_events()              # 排空 buffered CDP events
http_get(url, headers=None) # 纯 HTTP，无浏览器

# 对话框
handle_dialog(accept=True, prompt_text=None)  # 封装 Page.handleJavaScriptDialog
```

**Skill 新增原语**（与 browser-harness 的差异）：

```python
# Memory
remember(site, text)           # append-only 到 site-skills/<site>/memory.md
                               # ★US2★ 若 site 目录不存在，自动 bootstrap_site(site)
remember_global(text)          # append-only 到 ~/.browser-skill/global.md
remember_preference(key, value, confirm=True)  # ★US4★ 写 global.md 结构化偏好块
                               # confirm=True 时 raise NeedsUserConfirm，由 agent 走对话
memory_read(site=None)         # 返回 dict {global, site_specific}（task 运行时自动）
bootstrap_site(host, aliases=None)  # ★US2★ 显式 lazy-create site-skills/<host>/
                                    # 写 frontmatter + 空 memory.md + 空 SKILL.md
                                    # 已存在 = noop

# 站点 / 任务
list_site_skills(query=None)   # 查 index.json，可模糊匹配
load_site_skill(site)          # 注入该站点的辅助函数 + memory 进 namespace
run_task(site, name, **args)   # 不出 REPL 就调一个已固化的 task

# 错误恢复
try_recover_from_drift(site, name)  # 触发 task 的 selftest()；失败返回 None

# 固化建议（★US3★）
propose_solidify(name_hint=None) -> dict | None
    # 调用：one-shot 跑完，agent 觉得"这看起来可复用"时调一次
    # 返回 None = Skill 启发式判定 "不建议固化"（参数化弱 / 副作用大 / auth 太重）
    # 返回 dict = {
    #     "site": "<host-stem>",
    #     "suggested_name": "scrape_xxx",
    #     "readiness_score": 0.0-1.0,
    #     "reasons": ["参数化清晰", "无外发请求", "可重现"],
    #     "warnings": ["首次访问该站点", "selftest 需要 agent 补"],
    #     "draft_run_body": "<从 REPL history 提炼的 Python 代码>",
    #     "draft_args_schema": {...},
    # }
    # Skill 不直接问用户；agent 拿着这个 dict 在自己的对话流里问 user

solidify(spec)
    # spec 通常是 propose_solidify() 返回值 + agent/user 调整后的版本
    # 调它执行：写 tasks/<name>.py，跑 selftest，更新 index.json，记 LAST_VERIFIED
```

**`current_page()` 行为细节（US1 实现）**：

```python
def current_page() -> dict:
    """
    返回用户视觉前台 tab，自动 switch_tab 到它。
    走 daemon 的 BrowserDaemon.getActiveTab（Mode B）或 `browser-daemon active-tab --json`（Mode A subprocess）。

    Returns: {"targetId": "...", "url": "...", "title": "...",
              "accuracy": "heuristic-recent-activate" | "stale" | "unknown"}

    Behavior:
      - accuracy == "heuristic-recent-activate": ✓ 直接 switch_tab + 返回
      - accuracy == "stale" (daemon 几分钟没收到任何 activate 事件):
            warn agent + 降级到 list_tabs(include_chrome=False) 第一项 + 返回
      - accuracy == "unknown" (daemon 不支持 / 启动后从未见过 activate):
            等价 ensure_real_tab()
    """
```

Agent 写 US1 任务的标准开头：

```python
page = current_page()
# page["accuracy"] != "heuristic-recent-activate" 时 agent 应当先 confirm with user
print(page_info())
capture_screenshot("/tmp/start.png")
```

### A.3 Screenshot-first vs DOM-first 默认姿势

这是 browser-harness 哲学的核心，我们继承但**显式分层**了"什么时候降级到 DOM"——agent 经常因为没分清而做错。

**默认（screenshot-first）**：

1. `capture_screenshot()` →
2. 读像素找目标 →
3. `click_at_xy(x, y)` →
4. `capture_screenshot()` 验证。

Compositor 级输入（`Input.dispatchMouseEvent`）穿透 iframe / shadow DOM / cross-origin，绝大多数 click/scroll 都不需要 DOM。

**降级 DOM 的明确触发**：

| 场景 | 用哪个 | 为什么 |
|---|---|---|
| 隐藏 file input | `upload_file(selector, path)` | DOM.setFileInputFiles 直接设值 |
| 0×0 元素 / 没有视觉几何 | `js(...)` 或 `dispatch_key(selector, ...)` | 截图看不到，坐标没法点 |
| React 受控 input | `fill_input(selector, text)` | type_text 走 Input.insertText 不触发框架 onChange |
| CSRF 表单提交（GitHub star/unstar） | `js("document.querySelector('form[action$=...]').submit()")` | 按钮 click 被 React 吞，form.submit 直走 HTML POST |
| 大量结构化提取（评论列表） | `js("...querySelectorAll(...).map(...)")` | 一次 JS round-trip = N 次坐标读 |
| iframe 内同源元素 | 先 `iframe_target` 再 `js(..., target_id=...)` | 截图坐标进 iframe 没问题但读不出 DOM 数据 |
| 静态页 / 公共 API | `http_get(url)` 不开浏览器 | 不用付横幅成本 |

**降级 selector-first 的明确触发**：仅以上 6 种。其它情况都默认 screenshot。

`SKILL.md` 在 Agent 视图里把这张表前置——agent 收到任务后先**判断属于哪一档**，再选工具。这降低了"Playwright 习惯反射"（先 locate 再 click）的误用。

### A.4 错误模型

```python
class BrowserSkillError(Exception):
    """所有 Skill 抛出的异常根。Agent 可以宽 except 后判断子类。"""

class PageLoadFailed(BrowserSkillError):
    """wait_for_load 超时或 net::ERR_*."""
    url: str; reason: str

class ElementNotFound(BrowserSkillError):
    """wait_for_element 超时。"""
    selector: str; timeout: float

class AuthWall(BrowserSkillError):
    """检测到登录页 / 401 / 需要 OTP。Agent 应当停下问用户，不要从截图猜密码。"""
    url: str; signals: list[str]  # 例如 ["form[action*=login]", "url contains /login"]

class Captcha(BrowserSkillError):
    """检测到 captcha challenge。Agent 应当停下问用户或切换 cloud 浏览器。"""
    kind: str  # "recaptcha" / "hcaptcha" / "geetest" / "unknown"
    url: str

class NetworkError(BrowserSkillError):
    """fetch / XHR 层失败。"""
    url: str; status: int | None

class DaemonUnavailable(BrowserSkillError):
    """browser-daemon 起不来或 backend 全 fail。"""
    detail: str  # 来自 browser-daemon doctor --json

class SiteDrift(BrowserSkillError):
    """task 的 selftest 失败：URL 模式或顶层 selector 变了。"""
    site: str; task: str; failed_check: str

class CDPError(BrowserSkillError):
    """裸 CDP 返回了 error。"""
    method: str; params: dict; cdp_message: str
```

**如何"呈现给 Agent"**：

- **inline heredoc**：异常打到 stderr（JSON 单行：`{"type":"AuthWall","msg":"...","url":"..."}`），进程 exit 对应 code (`AuthWall=4`, `Captcha=5`, etc.)。Agent 读 exit code + stderr 就能分流。
- **repl socket**：response 里 `exception` 字段填充结构化对象，code 字段保持兼容。
- **task 运行**：同 inline，但额外把 `site/task` 写到 stderr 头部，方便 agent 报错。

**AuthWall / Captcha 的检测启发式**（v0.1 简单版）：

```python
# 在 wait_for_load 完成后跑一次:
def _detect_wall():
    info = js("""
        return {
          login_form: !!document.querySelector('form[action*="login" i], form[action*="signin" i], input[type="password"]'),
          captcha: !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], [class*="captcha" i]'),
          url_login: /\\b(login|signin|sign-in|auth)\\b/i.test(location.pathname),
          status_text: document.title.toLowerCase()
        }
    """)
    if info["captcha"]: raise Captcha(kind=_classify(info), url=current_url())
    if info["login_form"] and info["url_login"]: raise AuthWall(...)
```

这是默认开的；task 可以 `@suppress(AuthWall)` 关掉（例如 login flow 任务本身就在 login 页）。

### A.5 会话生命周期 / 状态携带

| 状态 | 谁存 | 跨 inline 调用 | 跨 repl 调用 | 跨 task 调用 |
|---|---|---|---|---|
| **CDP ws 连接** | repl daemon (Mode A) / browser-daemon (Mode B) | Mode A: 每次新建（弹窗）；Mode B: 复用 | 复用 | 复用 |
| **当前 attached tab** | repl daemon 内存 | 不保留 | 保留 | 由 task 自己 ensure_real_tab |
| **Cookies / localStorage** | Chrome 自己（用户 profile） | 永久（Chrome 持久） | 永久 | 永久 |
| **drain_events buffer** | repl daemon 内存 | 不保留 | 保留 | 保留但 task 入口 flush 一次 |
| **agent_helpers 加载** | exec namespace | 重新加载 | 保留 | 重新加载 |
| **memory 缓存** | exec namespace（lazy） | 重新读 | 保留 | task 入口 reload 当前 site |

**没有 cookie API**——v0.1 全靠 Chrome profile 持久化。需要 export/import cookies 的场景请用 `cdp("Network.getCookies")` / `Storage.setCookies` 自己卷，或者走 Browser Use cloud 的 profile-sync（已经在 browser-harness 里实现）。

### A.6 Helper 库的组织

借鉴 browser-harness 的双层但**显式 readonly 边界**：

| 层 | 路径 | 谁能改 | 何时加载 |
|---|---|---|---|
| **core primitives** | `src/browser_skill/core/` | 仅 Skill 维护者；agent 不许碰 | 进程启动 import |
| **interaction-skills** (md) | `interaction-skills/*.md` | 仅 Skill 维护者；agent 写 PR | agent 遇到机制问题时**主动读** |
| **site-skills** | `site-skills/<site>/` | agent + user 自由扩展 | `goto_url` 命中 host 或 `load_site_skill` 显式调 |
| **agent helpers** | `$BS_AGENT_WORKSPACE/agent_helpers.py` | agent + user 自由扩展 | exec 装配时**自动 import** |

**interaction-skills/ 的清单**（沿用 browser-harness 现有的 18 个，无明显新增需求）：

```
connection.md            cookies.md           cross-origin-iframes.md
dialogs.md               downloads.md         drag-and-drop.md
dropdowns.md             iframes.md           network-requests.md
print-as-pdf.md          profile-sync.md      screenshots.md
scrolling.md             shadow-dom.md        tabs.md
uploads.md               viewport.md          captcha-handoff.md  (新增)
```

`captcha-handoff.md` 新增是因为我们正式把 Captcha 作为错误类型，需要文档说明 agent 该怎么向用户求助。

`agent_helpers.py` 沿用 browser-harness 的 hot-reload 模式：每次 exec 装配前重新 `importlib.util.spec_from_file_location` 加载——agent 在跑任务过程中改了这个文件，下次 heredoc 立刻生效。

---

## B. 站点固化层 mechanics

### B.1 目录布局

```
site-skills/
├── index.json                # 自动维护的索引（站点 → 元数据 + 任务列表）
├── damai/
│   ├── SKILL.md              # 站点地图（agent 在 load 时读）
│   ├── memory.md             # 站点记忆（append-only）
│   ├── tasks/
│   │   ├── monitor_concert.py
│   │   ├── grab_seat.py
│   │   └── login_check.py
│   └── selftest.py           # （可选）跨 task 的站点级健康检查
├── xiaohongshu/
│   ├── SKILL.md
│   ├── memory.md
│   └── tasks/
│       ├── scrape_comments.py
│       └── search_notes.py
├── boss-zhipin/
│   ├── SKILL.md
│   ├── memory.md
│   └── tasks/
│       ├── search_jobs.py
│       ├── send_greeting.py
│       └── login_check.py
└── ...
```

**命名规范**：

- 目录名 = URL host 的"招牌词"（去掉 www. 和 TLD）。`xiaohongshu.com` → `xiaohongshu/`，`zhipin.com` → `boss-zhipin/`（Chinese-name 优先用品牌名而非 host stem），`mail.google.com` → `gmail/`。
- 任务名 = `snake_case` 动词短语：`scrape_comments`、`search_jobs`、`monitor_concert`。
- `SKILL.md` / `memory.md` / `selftest.py` 文件名固定。
- `tasks/` 子目录内可再分类（罕见），扁平优先。

`index.json` 由 `browser-skill index rebuild` 重建，单文件 + 文件锁，schema 见 §E.2。

### B.2 脚本格式

**用 Python，理由如表**：

| 候选 | 优点 | 致命缺点 |
|---|---|---|
| **YAML / TOML** (declarative) | 用户可读 | 没法表达条件分支、循环、异常恢复；REPL 探出来的 Python 代码无法平移 |
| **markdown + codeblock** | 文档 + 代码合一 | 跑起来要先解析 markdown，难以静态分析；codeblock 没有 type system / lint |
| **Python (.py)** | REPL 直接平移；可 import / 重用；标准 lint/test 生态 | 比 YAML 啰嗦 |
| **DSL (自家)** | 可以精确约束 agent | 维护成本爆炸；agent 还得学 |

`tasks/<name>.py` 模板：

```python
"""一句话描述任务用途。Agent 在 list_site_skills() 时读这一行做匹配。"""
from browser_skill.core import *  # 所有 primitives + remember/memory_read

# === 元数据（模块级常量，scaffolder 自动维护，可手改）===

ARGS = {
    "keyword": {"type": "str", "required": True, "desc": "搜索词"},
    "city": {"type": "str", "required": False, "default": "shanghai", "desc": "城市拼音"},
    "limit": {"type": "int", "required": False, "default": 20},
}

OUTPUT = "list[{title: str, url: str, salary: str, company: str}]"

TAGS = ["job-search", "scraping"]
REQUIRES_LOGIN = True            # selftest / runner 会先验证
ESTIMATED_DURATION_SEC = 30      # 给 Layer 3 调度参考
LAST_VERIFIED = "2026-05-15"     # selftest 通过时间，scaffolder 自动更新

# === self-check：在 run() 前调一次，失败抛 SiteDrift ===

def selftest():
    """快速验证站点结构没变。失败时 task 中止，agent 转 REPL 修复。"""
    goto_url("https://www.zhipin.com/web/geek/jobs")
    wait_for_load()
    assert page_info()["url"].startswith("https://www.zhipin.com/web/geek/jobs"), \
        "URL pattern drifted"
    assert wait_for_element("input[placeholder='搜索职位、公司']", timeout=5), \
        "search input selector drifted"

# === 主逻辑 ===

def run(args, ctx=None):
    """args = 验证过的 dict，ctx 包含 site memory / 上次运行结果（v0.2+）"""
    keyword, city, limit = args["keyword"], args["city"], args["limit"]

    # 站点级 memory 自动加载到 ctx.memory（dict-like），可以读用户偏好
    expected_city = ctx.memory.get("default_city") or city

    goto_url(f"https://www.zhipin.com/{expected_city}/")
    wait_for_load()
    fill_input("input[placeholder='搜索职位、公司']", keyword)
    press_key("Enter")
    wait_for_network_idle()

    results = js("""
        return Array.from(document.querySelectorAll('.job-card-wrapper'))
            .slice(0, %d)
            .map(c => ({
                title: c.querySelector('.job-name')?.textContent.trim(),
                url: c.querySelector('a')?.href,
                salary: c.querySelector('.salary')?.textContent.trim(),
                company: c.querySelector('.company-name')?.textContent.trim()
            }));
    """ % limit)

    # 命中"非显然"的事？记下来
    if any(r["salary"] and "面议" in r["salary"] for r in results):
        remember("boss-zhipin", "salary 字段可能是'面议'字符串，下游过滤要兼容")

    return results
```

**为什么不让 `run()` 直接返回未验证的 dict？** 因为 task 是给 agent 调的"黑盒"，agent 需要知道 shape 才能写后续 prompt。`OUTPUT` 字段是给 agent 看的——**不**强制 runtime 校验（v0.1）；v0.2 可以加可选的 `OUTPUT_SCHEMA = {...}` 走 pydantic。

### B.3 输入参数 / 输出契约 / 可发现性给 Agent 的接口

```python
# Agent 在 REPL 里:
list_site_skills(query="找工作")
# → [
#     {"site": "boss-zhipin", "task": "search_jobs",
#      "desc": "搜 BOSS直聘 的职位",
#      "args": {...}, "output": "list[{...}]", "tags": [...],
#      "match_score": 0.82},
#     {"site": "linkedin", "task": "search_jobs", ...},
#   ]

list_site_skills(site="boss-zhipin")  # 列站点下所有 task
list_site_skills()                     # 全量

# 跑一个
result = run_task("boss-zhipin", "search_jobs", keyword="后端", city="shanghai")
# 自动：load memory → selftest → run → 失败抛 SiteDrift
```

匹配靠 `index.json` + 简单 LLM-free 评分（B.5）。query 是自然语言时 agent 会自己做一次 LLM 排序。

### B.4 Probe → 固化的流转

这是**最重要的 UX 路径**——决定 agent 能不能"用一次学一次"。**US3 要求**：one-shot 完成后 Skill 必须能自动判断"是否值得固化"+ surface 询问 user。所以 §B.4 v0.1 必须实装最小可用版本（不再退到 v0.2）。

四个阶段：

#### B.4.0 Solidify-readiness 启发式（v0.1）

`propose_solidify()` 内部的判定逻辑。**输入** = REPL 历史 + 当前 site 信息。**输出** = readiness_score (0.0–1.0) + reasons + warnings。

```
score = 0.5  # baseline
                                                                  # readiness signals

+ 0.20  if 参数化清晰：识别到 1+ 输入变量（URL 模板 / search keyword / city / ...）
+ 0.15  if 输出结构化：最后一步 print/return 是 list/dict/json，不是无结构 print
+ 0.10  if 无外发副作用：流程里没有 click "submit"/"send"/"buy"/"pay" 类按钮
+ 0.10  if 不依赖手动决策：没有 input() / wait_for_user_confirm / "我看一下"
+ 0.10  if 类同任务可能重复：用户语言里有 "经常/每天/每次/每周/帮我监控"
                                                                  # readiness anti-signals
- 0.30  if auth wall 中途出现且需要 manual login：固化跑会再次撞墙
- 0.20  if CAPTCHA 出现过：高概率下次也撞，固化价值低
- 0.15  if 流程 > 30 步（探查噪音多，提炼难度高，readiness 降）
- 0.10  if site 首次访问（没有 memory 沉淀，selftest 写不准）

clamp to [0, 1]
threshold: ≥ 0.55 → propose；< 0.55 → return None（"不建议固化"）
```

启发式**故意保守**——宁可漏建议不要错建议（错建议浪费用户注意力 + 留下垃圾 task）。

输出示例：

```python
propose_solidify()
# → {"site": "producthunt", "suggested_name": "today_top_products",
#    "readiness_score": 0.78,
#    "reasons": ["参数化清晰：date 可变", "输出结构化：list[{name, votes, url}]", "无外发副作用"],
#    "warnings": ["首次访问该站点，selftest 需要 agent 补 URL pattern assert"],
#    "draft_run_body": "<提炼后的 Python>",
#    "draft_args_schema": {"date": {"type": "str", "default": "today", "desc": "YYYY-MM-DD or 'today'"}},
#   }
```

#### B.4.1 Solidify 询问协议（v0.1）

**Skill 不直接跟 user 对话**——Skill 没有 user-facing UI 层，只有 stdout/stderr 和 agent。所以"问用户"的呈现层 = **agent 在自己的对话流里**。

Skill 提供的工具：

```python
proposal = propose_solidify()
if proposal:
    # agent 现在做：
    # 1. 把 proposal["reasons"] + draft_args_schema 翻成自然语言
    # 2. 在自己的对话流里问 user：
    #    "刚才那个任务看起来可以保存成 producthunt/today_top_products，参数是 date。要保存吗？"
    # 3. user 回 yes/no/edit args
    # 4. agent 调 solidify(adjusted_spec)
```

`solidify(spec)` 等价于原来的 `browser-skill save <site>/<name>` 命令路径，多 ship 一个**自动提炼**的 `draft_run_body`（v0.1 最小提炼策略见 B.4.2）。

#### B.4.2 v0.1 最小提炼策略

**从 REPL history 提炼 `run()` 函数体的最小可用算法**：

1. **过滤**：取最近 N=50 条 exec 记录（按时间倒序到 `repl start` 为止）。
2. **保留**：仅留 `exception is None` 且 `stdout/result` 非 empty 的条目（success-only）。
3. **去探查**：丢弃满足以下模式的条目（启发式黑名单）：
   - `capture_screenshot(...)` 单独一行（agent 在看页）
   - `print(page_info())` / `print(current_tab())` 等纯观察
   - `list_tabs(...)` 纯观察
4. **去试错**：连续两次 `click_at_xy(x1, y1)` → `click_at_xy(x2, y2)`（不同坐标），仅保留后者 + 前一条 wait/screenshot（"试了第一个不对换第二个"模式）。
5. **去硬编码**：识别 `keyword = "..."` / `url = "https://..."` 模式 → 提升为 `args["keyword"]` 等参数。`draft_args_schema` 由此推导。
6. **结尾返回**：最后一个有结构化 stdout 的条目 → 包成 `return <expr>`。

**已知的不完美**：
- 提炼会带噪——产生 broken/redundant 代码。**所以**：agent 必须 review draft + 重新跑 selftest 才允许 commit。
- v0.2 加 `__BOOKMARK__` marker 协议（agent 在 REPL 里手动 print `__KEEP__` / `__SKIP__` 标记），把"哪些是 keeper"决策权前置给 agent，提炼精度跃升。
- 这是 §11 开放问题第一条的 v0.1 退化形态：**有总比没有强**，v0.1 必须 ship。

#### B.4.3 完整流程示例

```bash
# Stage 1: probe in repl
browser-skill repl start
browser-skill exec 'current_page()'           # 用户视觉前台 = ProductHunt
browser-skill exec 'capture_screenshot("/tmp/ph.png")'
browser-skill exec 'js("Array.from(document.querySelectorAll(\".... \")).map(...)")'
# ... agent 多轮探查，最终拿到 list of products

# Stage 2: propose
browser-skill exec 'proposal = propose_solidify(name_hint="today_top_products"); print(json.dumps(proposal))'
# → readiness_score = 0.78 提案

# Stage 3: surface to user (agent 走自己的对话流)
# Agent: "刚才那个任务看起来可以保存成 producthunt/today_top_products。要保存吗？"
# User: "好"

# Stage 4: solidify
browser-skill save producthunt/today_top_products --json-spec=<agent 调整后的 spec>
# → 写 site-skills/producthunt/tasks/today_top_products.py
# → 跑 selftest（agent 可能要补 URL/selector assert）
# → 更新 index.json
# → memory.md append "task today_top_products created on 2026-05-18"
```

为什么不做"AI 自动提炼"？

- REPL 历史里有大量探查（滚屏 / 截图 / 试错的 click），机器很难判断"哪几步对最终输出有贡献"。
- 一个错误的固化是负资产——下次 agent 信任它，结果它跑错，整链失败。
- 半自动让 agent 自己当 reviewer，固化质量天然过滤。v0.2 再做自动提炼 + diff review。

**Stage 3: 演化**

固化后 task 跑挂了 → `SiteDrift`。两条路径：

- **快路径**：`browser-skill task ... --recover` 自动把 task 代码 + memory 灌进 REPL namespace，agent 在 REPL 里调试 → 改 task 文件 → save 覆盖。
- **慢路径**：agent 删掉 task，重新 probe + scaffold。`memory.md` 保留——失败的 selector 也是知识。

### B.5 可重用性 vs 易碎性

**易碎来源**（按优先级）：

1. URL pattern 漂移 → `selftest` 第一行抓
2. selector 漂移 → `selftest` 第二行抓 + 主流程里捕获 ElementNotFound 时 raise SiteDrift
3. 私有 API endpoint 变 → `wait_for_network_idle` 间接发现；建议 task 不要直接 hardcode endpoint，走"用 UI 点出 XHR 然后 mimic" 路径（见 interaction-skills/network-requests.md）
4. 登录 / cookie 过期 → 触发 AuthWall

**self-test 强制约定**：每个 task **必须**有 `selftest()`。runner 在 `run()` 前调一次（v0.1 总是调；v0.2 加 cache：24h 内通过过就跳过）。失败 → 不调 run，直接 raise SiteDrift。

**没 selftest 的 task**：scaffolder 拒绝生成；linter (`browser-skill lint`) 报警告。

**memory 兜底**：site `memory.md` 里专门有 `## Known traps` 段，每次 SiteDrift 后 agent 可以追加一条。下次 scaffold 新 task 时，agent 应当先读 memory.md 再写代码——traps 段是必读。

### B.6 版本演化 / 回退路径

**没有显式版本号** v0.1。理由：

- task 文件在 git 里，git 就是版本。
- `LAST_VERIFIED` 时间戳 + `memory.md` 的"已知陷阱"段足够给 agent 决策"这 task 还能不能信"。
- 加显式 version 字段会让 agent 误以为需要做版本协商，反而拖慢。

**failed task 的处置**（按顺序）：

1. SiteDrift → agent 收到结构化错误，先看 `memory.md` 的"已知陷阱"。
2. 如果命中已知陷阱里写的 fix → 直接 patch task。
3. 否则进 REPL probe，按 Stage 3 慢路径走。
4. 多次失败（>3 次累积）→ agent 在 task 文件头加 `BROKEN_SINCE = "2026-MM-DD"`；`list_site_skills` 把它沉到最低优先级，避免 agent 反复踩坑。

---

## C. 记忆架构

### C.1 三个 store

| Store | 位置 | 用途 | 大小预期 | 加载时机 |
|---|---|---|---|---|
| **global memory** | `$BS_HOME/global.md` (默认 `~/.browser-skill/global.md`) | 跨站点偏好、别名、用户身份信息 | < 10 KB | 进程启动一次 |
| **site memory** | `site-skills/<site>/memory.md` | 站点级稳定知识：URL pattern、selector、私有 API、已知陷阱、用户对该站点的偏好 | 10–100 KB | `goto_url(host)` / `load_site_skill(site)` 命中 |
| **REPL 上下文** | 进程内 dict | 临时变量、上次截图路径、最近的 `current_tab()` | RAM-only | repl daemon 生命周期 |

### C.2 格式

**Markdown + 可选 YAML frontmatter**。

#### Site memory（`site-skills/<site>/memory.md`）

```markdown
---
site: damai
host_patterns: ["damai.cn", "www.damai.cn", "m.damai.cn"]
aliases: ["大麦网", "演唱会票", "concert tickets"]
last_updated: 2026-05-12
---

# damai 站点记忆

## 顶层 URL 结构
- 首页: https://www.damai.cn/
- 演唱会列表: https://search.damai.cn/searchajax.html?keyword=...
- 项目详情: https://detail.damai.cn/item.htm?id=<numeric>

## 稳定 selectors
- 项目卡片: `.items__row .items__row__item`
- 已售罄 badge: `.dm-btn.sold-out`

## 已知陷阱（Known traps）
- 项目页直接刷出来时，`#perform` 容器 lazy mount，要 wait_for_element(visible=True)。
- 加入购物车按钮在未登录态下显示但 click 跳登录页 → 先检查 `document.cookie.includes('_tb_token_')`。

## 私有 API
- `searchajax.html` 返回 JSON，字段：`{perform_id, name, price_range, venue, dates}`。
- 票档 API：`https://detail.damai.cn/api/skuApi?itemId=<id>` → 现成 JSON。

## 用户偏好（从对话累积）
- 用户偏好的座位区：前 5 排
- 用户的支付方式：支付宝（不要选微信）
- 用户**不**接受加价票
```

#### Global memory（`~/.browser-skill/global.md`）—— 含 daemon 偏好块（★US4★）

```markdown
---
schema_version: 1
daemon:
  preferred_backend: extension      # extension | autoconnect | rdp | env | cloud
  set_by_user_at: 2026-05-18T10:23:00Z
  # v0.5 REVIEW.md F-8: ``fallback_chain`` was an open-question proposal
  # that never shipped daemon-side. Retracted from schema. Use explicit
  # BD_BACKEND env or re-run ``browser-skill install`` to pick a
  # different backend instead.
  notes: "用户 2026-05-18 说'用我的浏览器插件'"
aliases:
  "演唱会票": "damai"
  "找工作": ["boss-zhipin", "linkedin"]
  "今天的会议": {"site": "gmail", "task": "list_calendar_events"}
last_choices:                       # E.4 消歧用，自动维护
  - {query: "新评论", chose: ["xiaohongshu", "scrape_comments"], at: "2026-05-17"}
---

# 全局 skill memory

## 用户身份
- 工作 timezone: Asia/Shanghai
- 工作语言：中文为主，英文 OK
- 默认城市：上海

## 跨站点偏好
- 一律不要在未确认前 click "购买/支付/发送"
- 找工作首选 BOSS直聘；wellfound / LinkedIn 是 fallback

## Daemon 连接说明（人类可读，与 frontmatter `daemon:` 对应）
- 优先用 Chrome 扩展 relay（无打扰，但要扩展安装好）
- 失败回退 autoconnect（每次需要点 Allow，但能连日常 Chrome）
- 不要用 rdp 隔离 profile（用户日常登录信息不在那里）
```

**为什么 daemon 偏好放 frontmatter** 而不是放在 markdown body 里：
- 机器可读：Skill 每次启动**必须**解析这块决定调哪个 backend，frontmatter 是 YAML 一行 parse 就行；markdown body 要写正则。
- 同步信息源唯一：human-readable 段在 body 解释 *为什么* 选这个 backend，frontmatter 是 *机器决定*。两者不冲突。
- v0.1 仅这一项结构化 frontmatter；其它 schema_version=1 留作扩展。

#### REPL 临时上下文

进程内 dict，不持久。Skill 启动时空，存 `last_screenshot_path` / `last_query_result` 等便利变量。

#### 为什么不用 JSON / SQLite？

- LLM 读 markdown 自然，不需要序列化层。
- `## headings` 提供天然的 section 锚，append/edit 简单（用 Python 的简易 markdown 编辑而非完整 parser）。
- frontmatter 是可选机器可读元数据，给 `index.json` rebuild 用。
- 用户能 vim 编辑——这是非负设计目标。

### C.3 读写时机 + 写入授权

**读**（自动）：

| 何时 | 读什么 |
|---|---|
| Skill 进程启动 | `global.md`（一次，pin 在 namespace）—— **包含 `daemon:` frontmatter 块，决定调哪个 backend** (★US4★) |
| `goto_url(url)` | 匹配 `url.host` → 自动加载对应 site memory 进 namespace 的 `__memory__` |
| `load_site_skill(site)` / `run_task(site, ...)` | site memory + 该 task 模块的 docstring 段 |
| `list_site_skills(query)` | 仅读 `index.json`（不展开 memory） |
| **用户问"我之前是怎么连的浏览器？"** | agent 读 `global.md` 的 `daemon` 块回答 (★US4★) |

**写**（按触发类别分级）：

| 类别 | 触发 | 调用 | 是否需要 user 确认 | 默认行为 |
|---|---|---|---|---|
| **类 A：发现** | Agent 命中"非显然事实"（私有 API、稳定 selector、URL 陷阱）。**包括执行流任意时刻**——US2 的 in-flight 写 (★US2★) | `remember(site, "...")` | 否，agent 自决 | append 到 site memory `## Notes` 段；**若 site 目录不存在 → 自动 lazy-create**（bootstrap_site） |
| **类 B：陷阱** | task 跑挂、SiteDrift、新发现的破坏性 selector | `remember(site, "...", section="Known traps")` | 否，agent 自决 | append 到 `## Known traps` 段 |
| **类 C：用户偏好** | 用户对话里说"记住 X" / "我喜欢 Y" | `remember(site, "...")` 或 `remember_global("...")` | 是，agent 必须先复述确认 | append；冲突时 prompt 用户 |
| **类 D：系统行为指令** ★US4★ | 用户说"用我的浏览器插件" / "改用 autoconnect" | `remember_preference("daemon.preferred_backend", "extension")` | **强制 confirm 一次**（不能 agent 自决） | 写 global.md frontmatter `daemon:` 块；旧值 → 移到 `notes` 历史段（不丢） |
| **类 E：自动元数据** | scaffolder 成功落 task 后 | 内部调用 | 否 | append `## Task history` 段 一行 `"`task X` created on YYYY-MM-DD`" |

**四条硬规则**：

1. **append-only by default**（类 A/B/C/E）。rewrite (`forget()` / `replace()`) 需要 user 显式说"删掉那条"或"改一下"。Skill API 拒绝 silent rewrite。**类 D 例外**——backend 偏好覆盖时旧值入 `notes`，frontmatter 新值，保留历史。
2. **不写敏感信息**。Agent 不许 `remember()` 密码、token、cookie 内容、信用卡、Chrome user-data-dir 绝对路径（可能含用户名）。Skill 在 `remember()` 入口跑 redaction 检查（正则：高熵字符串、`Bearer `、`/[A-Za-z0-9]{32,}/`、`/Users/[a-z]+/` 等）→ 命中就拒绝并打回 agent 让它换措辞。
3. **类 D 写入必须 user confirm**。`remember_preference(key, value, confirm=True)` 默认 raise `NeedsUserConfirm`，agent 必须先在对话流里跟用户确认 → 用户同意 → `remember_preference(..., confirm=False)` 强制写入。
4. **In-flight write（类 A，US2）合法**：执行流任意时刻 agent 调 `remember()`，Skill 在当前 frame 同步 flush（用 file lock 防 race，毫秒级）。**不**等到 task 结束。

**"和预想不一样" 的判定启发式（★US2★，给 agent 用）**：

Agent 在 task 执行中应当 `remember(host, ...)` 的触发信号（宽松判定，宁可多写不要少写）：

| 信号 | 例子 | append 到哪个段 |
|---|---|---|
| selector 失败但页面还在 | `wait_for_element('.foo', timeout=5)` 返回 False，但截图显示页面正常 | `## Known traps` "原 `.foo` selector 已失效，2026-MM-DD 测时实际是 `.bar`" |
| 文案与 memory 记载不一致 | memory 写 "搜索框 placeholder 是 '搜索职位'"，实际是 "搜索职位、公司" | `## Known traps` 一行更新 |
| URL 跳转出乎意料 | `goto_url(X)` 后落到 Y，且 Y 不是登录页（那是 AuthWall） | `## 顶层 URL 结构` "X 现在 redirect 到 Y" |
| 私有 API 返回字段变化 | XHR JSON 多出 `_version: 2` 字段 / 少了 `price_text` | `## 私有 API` "fieldname 变化记录" |
| UI 加了之前没见过的 modal / 弹层 | 操作中突然出现 cookie 同意横幅、订阅弹窗 | `## Known traps` "首次访问会有 X 弹层，需要先 dismiss" |
| 数字 / 标签反映出新功能 | "今日"标签变 "今日 NEW"，新增 sort by votes 等 | `## Notes` 一行 |
| **不**应该写的（agent 容易误触发） | • 正常 wait 超时（重试就过）<br>• 视觉布局微调（不影响 selector）<br>• 用户当次的偶发输入 | 跳过 |

Agent 在 SKILL.md 里被教育"看到这些信号 → 不要等 task 结束 → 立刻 `remember()` 一行"。这是 US2 "执行中沉淀" 的 operational definition。

**Daemon 偏好冲突解决** ★US4★：

读取优先级（高 → 低）：

```
CLI env (BS_DAEMON_BACKEND)
  > CLI flag (browser-skill --backend X)
  > global.md frontmatter daemon.preferred_backend
  > daemon doctor 推荐 (recommended 字段)
```

如果用户当次显式指定（前两档）跟 memory 偏好（第三档）不同 → Skill 走显式那一档 + agent 在 stderr 提示 user "本次用了 X 而不是 memory 里的 Y，要更新偏好吗？" 用户答更新 → `remember_preference()` 重写。

### C.4 与 Claude memory 的关系

Claude Code 自己有 `~/.claude/projects/.../memory/` 和 `CLAUDE.md` @-import 机制。Skill 选择**独立、不复刻**：

- Skill memory 是**工具的**，跨 agent runtime（Claude / Codex / 自家 SDK / cron task）。
- Claude memory 是**会话的**，跟 agent identity 绑。
- 两者重合是浪费——但单向桥接合理：

```bash
browser-skill memory export --site damai > damai-memory.md
# 用户手动 @-import 进 ~/.claude/CLAUDE.md 或者 /memory
```

Skill 也提供 `browser-skill memory import` 反方向，仅在用户明确把 Claude memory 段贴进来时用。不做自动同步——同步双向就会有冲突，冲突就会有 silent overwrite。

---

## D. Daemon 需求清单（Skill→Daemon 契约）★关键★

### D.0 设计角度：Skill 反推 Daemon

本节列出 Skill **要求** daemon 提供的能力，每条都绑到上面 §A/§B/§C 的具体场景。daemon 是 Skill 的依赖，**Skill 体验是 daemon 的 KPI**——任何"现有 daemon 不易做"的需求，由 daemon 改实现，不由 Skill 退让。

需求按"硬 / 软 / 设计倾向"三档：

- **硬需求**：缺一条 Skill 无法在那个场景工作；daemon 必须实现。
- **软需求**：缺了 Skill 仍能跑，但 UX 显著退化；强烈建议 daemon 实现。
- **设计倾向**：daemon 怎么做不重要，但 Skill 暗设了这种倾向（如 backend 透明），违反需要协商。

### D.1 解耦原则（硬）

- Skill **绝不 import** `browser_daemon` Python 包，零运行时耦合。
- 通信仅两条：
  - **Mode A**：subprocess `browser-daemon url` → stdout WS URL → Skill 自己开 ws。
  - **Mode B**：connect `ws+unix:///...sock`（POSIX）或 `ws://127.0.0.1:N?token=...`（Windows） → 标准 CDP JSON-WebSocket。
- 升级 daemon 不需要 Skill 重装。升级 Skill 不需要 daemon 重启。
- daemon 的二进制路径靠 `$PATH` / `BS_DAEMON_URL_CMD` env 注入。

### D.2 硬需求（缺一条 Skill 无法工作）

| ID | 需求 | 对应 Skill 场景 |
|---|---|---|
| D.2.1 | Mode A `browser-daemon url` 单行 stdout WS URL，稳定 exit codes (0/1/2/3) | inline heredoc / repl start 的每次启动都要拿 URL；exit code 决定 prompt 用户 vs 报 bug |
| D.2.2 | `doctor --json` 输出含 `schema_version: 1` + 7 字段 (name / available / ws_url / detail / ux_warning / needs_user_action / ux_cost) | `browser-skill doctor` 直接 forward；install 流程根据 `ux_cost` 排序候选；`needs_user_action` 文案直接给 user 看 |
| D.2.3 | `doctor` 默认**零副作用**：不开 ws、不触发任何 Chrome 弹窗 | install 流程要能"看一眼 Chrome 状态"而不打扰用户；agent 排查问题时也安全 |
| D.2.4 | `list-backends --json` 含 `needs_user_action` + `ux_cost` 机器可读字段 | install / repl start 失败时给 agent 决定 fallback backend 的依据 |
| D.2.5 | Mode B 完全 CDP-compatible wire format（标准 JSON-WebSocket，等价于直连 Chrome browser-level WS） | Skill 用任何 CDP client lib（cdp-use / 自家最小客户端）都能零改动复用 |
| D.2.6 | Mode B sessionId 透传：daemon 不翻译 sessionId（v0.2 单 client，passthrough；v0.3 多 client 时再做翻译表） | Skill 进程内 REPL 跑业务 + 同时调 task 共享同一 ws，sessionId 由 Skill 进程自己分配/区分 |
| D.2.7 | Mode B **stale upstream 关 client ws + 顺序通知**：先发 `Target.detachedFromTarget` per session → 发 `BrowserDaemon.upstreamClosed {reason}` → ws close 1011 | Skill 的 `try/except ConnectionClosed` 触发 re-resolve；agent 知道为什么 |
| D.2.8 ★US1★ | **`BrowserDaemon.getActiveTab`** RPC → `{targetId, url, title, accuracy, since_seconds}` 返回用户视觉上前台的 real page。`accuracy` ∈ `"heuristic-recent-activate" \| "stale" \| "unknown"`；v0.1 daemon 实现"最近一次 `Target.activateTarget` 命中"启发式（手动点 tab 切换 daemon 看不到，所以可能 stale）。v0.1 也提供 CLI `browser-daemon active-tab --json` 单次查询。 | REPL `current_page()` 原语的后端；US1 "帮我看这一页 / 帮我填这表单" 不需要用户指定 tab |
| D.2.9 ★US1★ | **`BrowserDaemon.subscribeFocus`** 事件（仅 Mode B v0.2）→ 当用户切换 tab / 关闭 tab 时推 `BrowserDaemon.activeTabChanged {targetId, url, reason}` | repl daemon 长驻期间能感知 "user navigated away from my working tab" → warn agent / 重新询问 |
| D.2.10 | 单 targetId 单 attacher 规则（**v0.3 上线**，跟 multi-client mux 同步）：v0.2 单 client 时退化为"Skill 进程内自管"——同一 tab 不要 attach 两次。v0.3 daemon 实装真正的 cross-client 检查 + CDP error 带 `data.holder` | v0.3 多 client 阶段防止 task 抢占 REPL 的 tab；v0.2 由 Skill 进程层保证（in-process discipline） |
| D.2.11 | Client 标签 (v0.2)：连接时带 `?client=skill-repl` query；Skill v0.2 进程只发一个 client，标签纯诊断用（doctor 输出） | `browser-skill doctor` 显示"daemon 在为 skill-repl PID 12345 服务"。`listClients` RPC daemon-architect 砍了（v0.2 用不到），v0.3 上 multi-client 时再加回 |
| D.2.12 | **`browser-daemon launch-chrome` 拉到 v0.1**：子命令启动独立 user-data-dir 的 Chrome (rdp backend)，输出 ws URL | install 流程必备——选 "无打扰" 路径时不要让用户记 `--remote-debugging-port=9222 --user-data-dir=...` 一长串 |
| D.2.13 ★US4★ | `browser-daemon url --backend X` 接受 backend 名称参数（已在 daemon-architect 现有设计里）；Skill 从 global.md `daemon.preferred_backend` 读出来后**直接传 flag**。Daemon 侧零新增。 | 用户自然语言"用我的浏览器插件" → agent 写 global.md → 下次启动 Skill 读偏好 → subprocess `browser-daemon url --backend extension` |
| D.2.14 ★US4 决定★ | **Mode B socket 握手 backend 参数 = 不做**。daemon Mode B 启动时 backend 已固定（per-daemon-process），不支持 per-client 切换。Skill 切 backend = `browser-daemon stop` + 用偏好重启 daemon。 | 同 daemon 共享一个 upstream backend 是 Mode B 的本质——同时维持两套 upstream 是 cloud daemon 才该做的事，超出 v0.2 范围 |

### D.3 软需求（缺了 UX 退化，但 Skill 能跑）

| ID | 需求 | Skill 退化形态 |
|---|---|---|
| D.3.1 | `BrowserDaemon.uiState` 查询返回 `{ws_count, last_popup_resolved_at}` | 无：Skill 假装"横幅一直在"，不主动 surface UI state；agent 自己问 user |
| D.3.2 | Mode B endpoint discovery：`browser-daemon endpoint` 单行输出 socket URL | Skill 硬编码默认路径 `ws+unix:///run/user/$UID/browser-daemon-default.sock`；用户改路径要手动配 |
| D.3.3 | `browser-daemon url --mode-b-proxy` 直接输出 Mode B 端点 | Skill 用 `browser-daemon endpoint` 替代，多一次 subprocess 调用 |
| D.3.4 | `?intent=long_lived` / `?intent=oneshot` query 提示 daemon 选择 idle policy | Skill 默认 `repl=long_lived` / `inline=oneshot` 全 hardcode，无 query 也行 |
| D.3.5 | 多 client 同 target attach 时 daemon 提供 "wait + handoff" 协调（**仅 v0.3+ 适用**，v0.2 单 client 无此场景） | v0.2 N/A。v0.3 第二个 client attach 已被占用的 target → 默认 fail；协调机制后续讨论 |

### D.4 设计倾向（daemon 怎么做不重要，但 Skill 暗设这些假设）

- **backend 名称对 Skill 透明**。Skill 不在代码里 case-on backend；backend 差异通过 doctor JSON 的 `ux_cost` / `needs_user_action` 字段标准化，Skill 只展示给 user 看。
- **Event fanout：browser-level events 必须广播**。无 sessionId 的 `Target.*` 系列每个 client 都要看到（否则 `Target.getTargets` 跑不对）。这是必要的状态同步，不是泄漏。
- **socket 鉴权简单为美**。POSIX 用 0600 文件权限即鉴权，Windows 用 token query；不要 OAuth / mTLS。
- **Idle policy 在 daemon 内部决策**，Skill 只在配置文件提示倾向；不需要 daemon 暴露 idle config CLI。
- **Sub-agents 并行支持留到 v0.3**。v0.2 Mode B 严格**单 skill-repl client**——daemon 第二个连接直接 reject (503)。task 调用走 **Skill 进程内 ws-reuse**（同一 ws 上分配不同 sessionId），不开新 daemon connection。多 client REPL / sub-agents 并行不是 v0.2 范围。

### D.5 daemon-architect 已确认采纳的事项（reference）

截至当前协调（与 daemon-architect 三轮往返），下列项已书面承诺写进 daemon `design-v2.md` 的 §13 "Layer 1↔2 接口契约"：

- D.2.1–D.2.7 全部
- D.2.8 / D.2.9：`BrowserDaemon.getActiveTab` + `subscribeFocus`，accuracy 字段为 `"heuristic-recent-activate" | "stale" | "unknown"`，v0.1 daemon 跑选项 A heuristic（"最近一次 `Target.activateTarget`"，**不**做 visibility-poll/隐式 attach 所有 tab）；v0.1 CLI `browser-daemon active-tab --json`，v0.2 同名 RPC ✓
- D.2.10：单 attacher CDP error 带 `data.holder`（**v0.3+ 上线**，v0.2 由 Skill 进程内 discipline 保证）✓
- D.2.11：`?client=skill-repl` query 用于 v0.2 诊断；`listClients` RPC 砍至 v0.3；`?intent=...` query **砍掉**（client label 已足够） ✓
- **D.2.12 已 ACK**：`browser-daemon launch-chrome` 拉到 v0.1。daemon-architect §5.5 完整 spec（chrome binary 查找、`--persistent` / `--tmp` 模式、detached spawn、pid 文件、不透传自定义 Chrome flag）。install wizard 可直接调，不用 fallback 教用户敲命令 ✓
- D.2.13：`browser-daemon url --backend X` 已支持，US4 daemon 侧零新增 ✓
- D.2.14：socket 握手 backend 参数 **不做**，per-client backend = 重启 daemon ✓
- **D.3.1 已 ACK**：`BrowserDaemon.uiState` v0.2 实装，返回 `{ws_count, last_popup_resolved_at, banner_visible_estimated}`（多了一个 `banner_visible_estimated` 字段）✓
- doctor `schema_version: 1` + `ux_cost` 受限枚举 (`"none" | "banner" | "popup-per-ws + banner" | "extension-permission"`) ✓
- 兼容 `BU_CDP_WS` / `BU_CDP_URL` env（browser-harness 用户迁移路径）✓
- **Mode B socket path-based**（不是 `ws+unix://` URL scheme）：`browser-daemon endpoint` 输出裸路径，Skill `CDPConn.connect_unix(path)` 调用 ✓
- **Upstream lazy 重连**：daemon **不**自动重连 upstream（避免 autoconnect 无操作弹窗）；只在 Skill 端发起新一次 connect 时 lazy 重开 ✓
- **v0.2 严格单 client (skill-repl)**：daemon 第二个连接直接 reject 503；多 client mux + sessionId 翻译表 + 单 attacher 检查 + listClients 全部留到 v0.3 ✓

待办：无 daemon 侧未签事项。Cross-ref D.2.x ↔ H1-H10 / S1-S3 编号映射见下方 §D 末注。

### D.6 配置切换（Skill 端）

```toml
# $BS_HOME/config.toml
[daemon]
mode = "auto"     # "A" / "B" / "auto"。auto = 先试 Mode B socket，失败 fallback Mode A
endpoint = ""     # Mode B 端点：unix socket 文件**绝对路径**（非 URL）；空 = 让 daemon 自报 (browser-daemon endpoint)
url_cmd = "browser-daemon url"           # Mode A
launch_cmd = "browser-daemon launch-chrome"  # 用 D.2.12 起 Chrome
```

`auto` 是 v0.2 起的默认。v0.1 = `"A"`（Mode B 还没上线）。

**注**：D.6 `endpoint` 是 socket 文件路径，不是 `ws+unix://` URL scheme——daemon-architect 协商一致后的最终设计。Skill 端 `CDPConn.connect_unix(path)` 直接接路径，不需要 URL parsing。

**Backend 选择**（★US4★）：

实际跑的 backend = §C.3 冲突解决后的结果。Skill 启动时：

```python
backend = (
    os.environ.get("BS_DAEMON_BACKEND")                          # 1. env
    or cli_args.backend                                          # 2. flag
    or read_global_md_frontmatter()["daemon"]["preferred_backend"]  # 3. memory
    or None                                                      # 4. daemon recommended
)

# Mode A: 拼 subprocess
if backend:
    cmd = [url_cmd, "--backend", backend]
else:
    cmd = [url_cmd]                  # daemon 自己挑 recommended
```

### D.7 Skill 端集成代码（实现草稿）

```python
# daemon_client.py 概要

class DaemonClient(Protocol):
    async def connect(self) -> CDPConn: ...
    def doctor(self) -> dict: ...
    async def get_active_tab(self) -> dict: ...   # 走 D.2.8 / Mode A 时退化为 None

class ModeAClient:
    """Mode A: subprocess resolver + Skill 自己开 ws。"""
    def __init__(self, url_cmd="browser-daemon url"):
        self._cmd = shlex.split(url_cmd)
        self._cached_url: str | None = None

    async def connect(self) -> CDPConn:
        if self._cached_url:
            try:
                return await CDPConn.connect_browser_ws(self._cached_url)
            except (ConnectionRefusedError, websockets.exceptions.InvalidStatus):
                self._cached_url = None    # 触发 re-resolve
        proc = await asyncio.create_subprocess_exec(
            *self._cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DaemonUnavailable(stderr.decode(), exit_code=proc.returncode)
        self._cached_url = stdout.decode().strip()
        return await CDPConn.connect_browser_ws(self._cached_url)

    async def get_active_tab(self):
        # Mode A v0.1 走 CLI 子命令 `browser-daemon active-tab --json`
        try:
            out = subprocess.check_output(
                ["browser-daemon", "active-tab", "--json"], text=True, timeout=2)
            return json.loads(out)
            # → {"targetId": "...", "url": "...", "title": "...",
            #    "accuracy": "heuristic-recent-activate" | "stale" | "unknown",
            #    "since_seconds": float}
        except (subprocess.CalledProcessError, TimeoutExpired):
            # daemon 不支持 active-tab 子命令 → 退化为 list_tabs 启发式
            return None

class ModeBClient:
    """Mode B: 连 daemon 的 socket，daemon 替你 multiplex。"""
    def __init__(self, endpoint=None):
        self._endpoint = endpoint or self._discover_endpoint()

    def _discover_endpoint(self) -> str:
        return subprocess.check_output(["browser-daemon", "endpoint"], text=True).strip()

    async def connect(self) -> CDPConn:
        try:
            conn = await CDPConn.connect_daemon_socket(
                self._endpoint, client_label="skill-repl")
            return conn
        except (FileNotFoundError, ConnectionRefusedError):
            raise DaemonUnavailable(f"daemon socket not available: {self._endpoint}")

    async def get_active_tab(self):
        # 走 BrowserDaemon.* RPC，daemon 直接告诉你
        return await self._conn.send("BrowserDaemon.getActiveTab")
```

**错误恢复策略**：

- 第一次 CDP 调用失败 → Skill 自动 `connect()` 重连一次（cache 已清），重连成功后**单次** retry 原调用。
- 第二次失败 → 抛 `DaemonUnavailable` 给 agent；agent 决定 `browser-skill doctor` 或问 user 起 Chrome。
- **绝不**做指数退避循环重试——根因要么 Chrome 关了要么用户 dismiss 了弹窗，重试无效。
- Mode B 收到 ws close 1011 → 抛 `DaemonUnavailable`，触发上述流程；如果 close 前收到 `BrowserDaemon.upstreamClosed`，把 `reason` 字段塞进异常。

### D.8 Mode A vs Mode B 在 Skill 层的体感差异

| Skill 操作 | Mode A | Mode B |
|---|---|---|
| `browser-skill <<'PY' print(page_info()) PY` | 起 subprocess → resolve URL → 开 ws (Allow popup if autoconnect) → CDP → 关 ws | 起 subprocess → connect socket → CDP via daemon → 关 socket（**无**弹窗 / 横幅不闪） |
| `browser-skill repl start` 一次 | 同上，但 ws 一直 open 到 stop | 同上，socket 一直连到 stop |
| Chrome 重启时 | 第一次操作 → ConnectionRefused → re-resolve → 新 URL → 新 popup | daemon 收到上游 close → 通知 client → Skill 抛 DaemonUnavailable → Skill **主动**新一次 socket connect → daemon **lazy** 重开 upstream（autoconnect 弹窗在用户主动重连时出现，符合预期） |
| 获取"用户当前在看的 tab" ★US1★ | `browser-daemon active-tab --json` subprocess（每次新 fork，可接受） | `BrowserDaemon.getActiveTab` RPC（持久 ws，零开销） |
| user 切换 tab 时 REPL 反应 ★US1★ | 不感知（除非每次 reattach 重查 active-tab） | 收 `activeTabChanged` 事件 |
| Skill 进程内 REPL + task 并发调用 | task 借同进程 ws-reuse，sessionId Skill 自管 | 同左（v0.2 daemon 仍是单 client，不参与）|
| 起两个独立 Skill 进程 / sub-agent | **不支持** —— 两个 ws 都引弹窗 + 多 attach 冲突 | **v0.2 不支持** —— daemon 第二个 connection 直接 reject 503。**v0.3+ 支持** —— daemon multi-client mux + sessionId 翻译表 + 真单 attacher 检查 |
| US2 站点首次访问 | `goto_url` 后 `remember(host, ...)` 自动 lazy-create `site-skills/<host>/` | 同 Mode A |
| US3 propose_solidify | 读 REPL daemon history（`browser-skill exec` 调过的所有片段） | 同 Mode A |
| US4 改 backend 偏好 | `remember_preference("daemon.preferred_backend", "X")` → 下次 subprocess 用 `--backend X` | 同 Mode A，但 Mode B 已 running 时切 backend = Skill 提示用户先 `browser-daemon stop` |

→ Skill v0.1 文档明确：**autoconnect backend + Mode A 时只支持 `repl start` 长驻**，inline heredoc 会反复弹窗（§A.1 提示）。`rdp 独立 profile + Mode A`（含 v0.1 的 `browser-daemon launch-chrome` 引导）没这个问题。v0.2 Mode B 上线后所有 backend 都流畅。

**Upstream lazy 重连的语义保证**（与 daemon-architect 协商一致）：

daemon **不**在后台主动重试 upstream（避免 autoconnect 用户在没操作浏览器时突然冒一个 Allow 弹窗）。流程是：

```
Chrome 退出 / upstream ws 断开
  ↓
daemon → 给每个 client 发 Target.detachedFromTarget per session
  + BrowserDaemon.upstreamClosed {reason}
  + ws close 1011
  ↓
Skill 收 ws close → 抛 DaemonUnavailable（含 reason）
  ↓
Skill 进入 §D.7 错误恢复：retry connect 一次
  ↓
daemon 收到新 client connect → lazy 重开 upstream
  → autoconnect 用户**此时**看到 Allow 弹窗（用户主动行为触发，符合心智）
```

这把 "何时弹窗" 的控制权放回 Skill / 用户，daemon 不替谁做决定。

### D.9 Cross-ref：D.x.y ↔ daemon design-v2 §3 H/S 编号

为协调 design-v2.md (`browser-daemon/design-v2.md`)，保持本文档 `D.x.y` 编号为**正本**；daemon 端 §3 表用 H1-H10 / S1-S3 编号 + 此映射列指回这里。

| Skill D.x.y | Daemon §3 H/S | Daemon §落点 (design-v2) |
|---|---|---|
| D.2.1 (Mode A url resolver) | H1 | §5.1 |
| D.2.2 (doctor JSON shape) | H2 | §5.2 |
| D.2.3 (doctor 零副作用) | H2（同 H2 spec）| §5.2 |
| D.2.4 (list-backends 含 ux_cost) | H3 | §5.3 |
| D.2.5 (Mode B CDP-compatible wire) | H4 | §6.3 |
| D.2.6 (sessionId 透传 v0.2 / 翻译 v0.3) | H5 | §6.3 |
| D.2.7 (stale upstream 关闭礼仪) | H6 | §6.5 |
| D.2.8 (getActiveTab + CLI active-tab) | H8 part A | §5.4 + §6.4.1 |
| D.2.9 (subscribeFocus event) | H8 part B | §6.4 |
| D.2.10 (单 attacher 规则，v0.3+) | H7（v0.3 上线） | §6.8 |
| D.2.11 (client label query) | S2（v0.2 诊断用） | §3.2 + §6.2.1 |
| D.2.12 (launch-chrome v0.1) | H9 | §5.5 |
| D.2.13 (--backend flag from memory) | H10 | §5.1 args |
| D.2.14 (socket 握手 backend = 不做) | 拒绝列表 | §3.3 |
| D.3.1 (uiState query) | S1 | §6.4 |
| D.3.2 (endpoint discovery) | S3 | §6.1 |
| D.3.3 (url --mode-b-proxy) | 不做（用 endpoint 替代） | §3.3 |
| D.3.4 (?intent query) | 砍掉 | §3.3 |
| D.3.5 (多 client handoff，v0.3+) | 推迟 v0.3+ | §3.4 |

**改编号约定**：我 §D 加新条目用下一个 D.x.y 编号（不重排）。daemon 端 H/S 表只需追加一行映射。任一方都不需要 force-sync 编号空间。

---

## E. 可发现性

### E.1 自然语言请求 → 站点 + 任务的路径

```
agent 收到 "帮我刷一下小红书最新评论"
  ↓
agent 调 list_site_skills(query="小红书 评论")  ←── 在 REPL 里
  ↓
Skill 内部:
  1. 加载 $BS_HOME/global.md，提取 aliases 表
     例: {"演唱会票": "damai", "找工作": ["boss-zhipin","linkedin"]}
  2. 在 query 上做 alias 替换 + 简单 tokenize
  3. 在 index.json 上跑评分（详见 E.3）
  4. 返回 top-K 候选
  ↓
agent 看候选 → 选 ("xiaohongshu", "scrape_comments")
  ↓
agent 调 run_task("xiaohongshu", "scrape_comments", post_url="...")
```

### E.2 index.json schema

`browser-skill index rebuild` 扫 `site-skills/` 重新生成。每个 task scaffold/edit 后自动调一次（git pre-commit hook 也保险）。

```json
{
  "version": 1,
  "generated_at": "2026-05-18T10:00:00Z",
  "sites": [
    {
      "site": "damai",
      "host_patterns": ["damai.cn", "www.damai.cn", "m.damai.cn"],
      "aliases": ["大麦网", "演唱会票", "concert tickets"],
      "description_first_line": "大麦网票务网站",
      "tasks": [
        {
          "name": "monitor_concert",
          "desc": "监控指定演唱会的票务状态变化",
          "tags": ["monitor", "ticket"],
          "args": {"concert_id": {"type": "str", "required": true}},
          "output": "{has_tickets: bool, price_ranges: list[str], last_check: str}",
          "requires_login": false,
          "last_verified": "2026-05-15",
          "broken_since": null
        },
        {"name": "grab_seat", "...": "..."}
      ]
    },
    {"site": "xiaohongshu", "...": "..."}
  ]
}
```

`broken_since` 字段在 task 头部 `BROKEN_SINCE = "2026-..."` 时填，匹配引擎会把它降权到最低。

### E.3 匹配评分算法（v0.1，无 LLM）

```python
def score(query: str, site_entry: dict, task: dict) -> float:
    q = normalize(query)
    s = 0.0
    # alias hit （最强信号）
    for alias in site_entry["aliases"]:
        if alias in q or fuzzy_match(alias, q) > 0.85:
            s += 1.0; break
    # host hit
    for h in site_entry["host_patterns"]:
        if h.split(".")[0] in q:
            s += 0.5; break
    # task desc hit
    s += 0.3 * jaccard(tokens(task["desc"]), tokens(q))
    # tag hit
    for t in task["tags"]:
        if t in q: s += 0.2
    # broken_since penalty
    if task.get("broken_since"): s -= 0.5
    # last_verified bonus（30 天内 +0.1）
    if days_since(task["last_verified"]) < 30: s += 0.1
    return s
```

**为什么不直接 LLM 排序？**

- Skill 不该带 LLM 依赖——它是工具，runtime agent 才是 LLM。
- 简单评分 + agent 自己 re-rank 已经够：candidate 通常 ≤ 5，agent 一次 prompt 就能选。
- 评分公开、可解释，agent 能告诉 user "我选这个因为它别名匹配 + 30 天内验证过"。

### E.4 全局 memory 怎么帮消歧

`global.md` 的 `## Aliases` 段：

```markdown
## Aliases (user-defined)
- "演唱会票" → site:damai          # 用户上次说 "帮我买演唱会票" → 我猜大麦 → 用户确认 → remember_global
- "找工作" → site:boss-zhipin      # 默认；用户也用过 linkedin/wellfound，但首选 boss
- "今天的会议" → site:gmail task:list_calendar_events
```

匹配引擎在评分阶段把 alias 当成 weight=1.0 的强信号。**多个 alias 同时命中**：

1. 取最 specific 的（`site+task` > `site`-only）。
2. 否则按用户最近选择（`global.md` 末尾自动维护一段 `## Last choices`）。
3. 仍歧义 → agent 必须问 user，不能猜。

### E.5 优先级（多个 task 都能满足时）

```
1. 用户在 query 里明确说了 site → 锁定该 site，在其 tasks 里挑
2. global memory 有 alias → 用 alias 指向的 site/task
3. index 评分最高的（≥ 0.7 即可）
4. 候选 ≥ 2 且 top1 和 top2 分差 < 0.15 → 问 user
```

### E.6 用户自查类问题（★US4★ 副作用）

用户偶尔会问："我之前是怎么连的浏览器？" / "我用的是哪个 backend？" / "我对小红书设过什么偏好？" 这些都是**对 memory 的反向查询**。Agent 应当：

| 用户问 | Agent 做 |
|---|---|
| "我用的是哪个 backend？" | 读 `global.md` frontmatter `daemon.preferred_backend` → 直接告诉 |
| "我之前怎么连的浏览器？" | 同上 + body 段 "Daemon 连接说明" 翻译给用户 |
| "我对 X 站点有什么偏好？" | 读 `site-skills/X/memory.md` 的 "用户偏好" 段 |
| "我设过什么别名？" | 读 `global.md` frontmatter `aliases` 块 |
| "我之前跑过哪些 task？" | 扫所有 `site-skills/*/memory.md` 的 `## Task history` 段 |

这条不需要新原语——`memory_read(site=None)` 已经能返回所有内容，agent 自己筛选。Skill 端文档（`SKILL.md`）把这些常见查询模式列出来，避免 agent 重新发明。

---

## F. 实测约束 / Daemon 反向影响（汇总）

（细节已在 §2，这节是 checklist 形式给 implementer。）

**Skill 必须避免的反模式**：

- [ ] 每个 ad-hoc 操作起新 subprocess + 新 ws（autoconnect 致命）
- [ ] 同时开 2+ browser-level ws
- [ ] doctor / install 流程未经显式 opt-in 就 probe ws
- [ ] 短时间内 close + reopen ws（横幅闪烁）
- [ ] 自行实现 backend discovery / `DevToolsActivePort` 扫描（这是 daemon 的事）

**Skill 必须做的**：

- [ ] 默认 long-lived ws (repl daemon 或 Mode B 复用上游)
- [ ] 提供 `browser-skill doctor` 但直接 forward `browser-daemon doctor --json` 输出
- [ ] inline heredoc 在 autoconnect+ModeA 组合下打 warning（说"请改用 `repl start`"）
- [ ] DaemonUnavailable 异常包含 daemon doctor 的诊断（agent 直接给 user 看）

**Mode A vs B 对 Skill 配置的影响**：

| 选择 | 影响 |
|---|---|
| Mode A + rdp 独立 profile | 完美。inline heredoc OK，repl start OK，无打扰。 |
| Mode A + autoconnect | repl start OK；inline heredoc 每次弹窗（文档警告） |
| Mode A + env (BD_CDP_WS already set) | inline heredoc OK，因为外部已经管 ws 生命周期 |
| Mode B + 任意 backend | 完美。daemon 替你管 ws 生命周期 |

---

## 7. CLI 设计

```
browser-skill [options]

子命令：
  repl start                          启动 long-lived REPL daemon
  repl stop
  repl status
  exec <code>                         向 REPL daemon 发一段代码（client）

  task <site>/<name> [--arg=val ...]  跑一个已固化 task
  list-tasks [--site SITE] [--query Q] [--json]
  save <site>/<name> [--from-repl]    scaffold 新 task

  index rebuild                       重建 index.json
  doctor [--json]                     转发 browser-daemon doctor + Skill 自检

  memory show [--site SITE | --global]
  memory export [--site SITE]
  memory import [--site SITE]

  version
  --help / -h

入口：
  browser-skill                       等价于 `inline heredoc` 入口 (读 stdin)
  browser-skill <<'PY' ... PY        ← 主要使用方式
```

**全局 env / 配置**：

| 变量 | 含义 |
|---|---|
| `BS_HOME` | memory 根目录，默认 `~/.browser-skill` |
| `BS_AGENT_WORKSPACE` | agent_helpers.py 所在目录，默认 `$BS_HOME/agent-workspace` |
| `BS_DAEMON_MODE` | `A` / `B` / `auto`；覆盖 config |
| `BS_DAEMON_SOCKET` | Mode B socket 路径 |
| `BS_DAEMON_URL_CMD` | Mode A 调用命令，默认 `"browser-daemon url"` |
| `BS_VERBOSE` | 写诊断到 stderr |
| `BS_DEBUG_CLICKS` | 沿用 browser-harness：截图叠红色标记点击位置 |

---

## 8. 与 Layer 3 (Tasks) 的接口

**Skill 提供给 Layer 3 的契约**：

- `browser-skill task <site>/<name> --json-args='{...}' --json-output` → 单次调用，stdin/stdout 都 JSON。
- `browser-skill list-tasks --json` → 给 Layer 3 当 catalog。
- DaemonUnavailable / AuthWall / Captcha 通过 exit code (4 / 5) + stderr JSON 告知。

**Skill 显式不做的**：

- 调度 / cron / 重试策略 / 错误通知（apprise webhook 之类）—— Layer 3 的事。
- 跨 task 的状态（"上次跑的结果"）—— Layer 3 自己存。
- 多账号 / 多 browser 实例并发 —— Layer 3 起多个 Skill 进程 + 多个 daemon BU_NAME。

---

## 9. Related Work / 借鉴与拒绝

参考的项目都在 `../`：

### browser-harness（最相关，全面借鉴）

**借鉴**：
- 全套 core primitives（§A.2 直接复用 helpers.py）。
- "Coordinate clicks default, drop to DOM only when必要" 哲学。
- 三层 helper 组织：core / interaction-skills (md) / agent-workspace (Python)。
- `goto_url` 命中 host 后曝光 site-skills 列表的发现机制。
- `BH_AGENT_WORKSPACE` + `agent_helpers.py` 的 hot-reload 模式。

**改造**：
- browser-harness 把 daemon 也内置（`browser_harness.daemon`）；我们拆出来 = Layer 1。Skill 不 import daemon 包。
- browser-harness 的 `domain-skills/<site>/*.md` 是 **markdown only**，纯文档；我们 `site-skills/<site>/tasks/*.py` 是 **可执行**——markdown 给 agent 学姿势，Python 给 agent 直接调用。这是借鉴 OpenCLI 的 cases/ 思路反过来。
- 默认 cloud auto-bootstrap：browser-harness 在 `BROWSER_USE_API_KEY + BU_AUTOSPAWN` 时起远程 daemon；我们不做这个 —— Skill 不管 daemon 怎么起。

### browser-cli（structured skill router）

**借鉴**：
- `workflows/<name>.ts` = "pure function: schema → run" 的契约 = 我们的 task 格式。
- `tasks/<name>.ts` = scheduled stateful wrapper 的设计——这是 Layer 3 该长的样子，我们 §8 留接口。
- `SKILL.md` 作为 sub-flow router（依据 user 意图加载不同子 .md）：我们 `SKILL.md` 也走这个模式，针对"REPL 使用"vs"task 调用"vs"memory 管理"分发。
- "Project workflows" vs "global workflows" 的两级 namespace —— 我们 v0.1 只做全局，v0.2 加 project-level (`./.browser-skill/site-skills/`)。

**拒绝**：
- browser-cli 把 schema 写在 TS 里 + 编译时类型检查；我们 v0.1 是 Python + 字典 ARGS，不强制 schema validation。理由：我们更看重 REPL → task 的零摩擦平移，强类型把这条路径加上"先写 zod schema"的负担。v0.2 可以 optional pydantic。
- browser-cli 的 "sub" 机制（订阅别人的 git 仓库）—— 是个好 idea，但 v0.1 范围外。

### playwriter（chrome 扩展 relay）

**借鉴**：
- `MEMORY.md` 的形态：人 + agent 混写、按时间 + 主题分段、纯 markdown。我们 `memory.md` 完全一样的格式。
- "运行 `playwriter skill` 一次性把所有文档 dump 给 agent" 的反 RAG 思路 —— 我们 `browser-skill help --full` 也提供（v0.2）。

**拒绝**：
- Playwriter 的 chrome 扩展 + relay 架构 —— 是 daemon 的 backend 选择问题，跟 Skill 无关。
- 它的 "session" 概念（`playwriter session new` 起一个 sandbox）—— 我们用"REPL 进程"代替，更轻。

### OpenCLI（adapter + cases narrative）

**借鉴**：
- `cases/<name>.md` 用叙事 markdown 记录"我做了 X 用了什么命令得到什么" —— 这是站点 `SKILL.md` 的灵感。我们的 `SKILL.md` 里也用 narrative 描述"在这个站点用什么姿势"。
- "adapter 输出 identifier-rich JSON, 一个 adapter 的 output 是另一个的 input" 链式思路 —— Layer 3 的输入设计应该参考。

**拒绝**：
- OpenCLI 的 `cases/` 是"recipe 给人读"，不直接被 runtime 调用；我们 `tasks/*.py` 必须可被 agent 直接调用。两者不冲突，我们站点的 `SKILL.md` 类似 cases/。

---

## 10. v0.1 / v0.2 / v0.3 路线图（与 daemon 对齐）

### v0.1（Daemon Mode A only：env / rdp / autoconnect + `launch-chrome` 子命令）

Skill 范围：
- [ ] core primitives 全部（§A.2，沿用 browser-harness）
- [ ] **`current_page()` 原语** ★US1★：subprocess `browser-daemon active-tab --json` + auto switch_tab + accuracy 字段处理
- [ ] **`bootstrap_site(host)` 原语** ★US2★：lazy-create `site-skills/<host>/`
- [ ] **`propose_solidify()` + `solidify()` 原语** ★US3★：readiness 启发式 + REPL history 最小提炼 + scaffolder
- [ ] **`remember_preference()` API** ★US4★：写 global.md frontmatter `daemon:` 块，confirm 协议
- [ ] inline heredoc + `repl start/stop/exec` 三个入口
- [ ] inline 入口 auto-suggest `repl start`（autoconnect 检测）
- [ ] `daemon_client.py` Mode A 实现 + 第一次失败 retry 一次
- [ ] error 类型体系（§A.4）+ `_detect_wall()` 启发式（默认 off，站点 SKILL.md 显式 opt-in）
- [ ] memory: `global.md`（frontmatter daemon 块 + body）+ 每站点 `memory.md`，append-only + lazy-create + redaction 检查
- [ ] memory in-flight write ★US2★：执行流任意时刻 `remember()` 同步 flush（file lock）
- [ ] backend 偏好读取 + 冲突解决（§C.3）：env > flag > memory > daemon recommended
- [ ] site-skills bundled 起步集（5–10 个）：damai、xiaohongshu、boss-zhipin、github、gmail、google-calendar
- [ ] `list-tasks` + `index rebuild` + 简单评分（§E.3）
- [ ] `doctor` 转发 browser-daemon doctor
- [ ] `install` wizard：检测 Chrome → 询问 "用日常 Chrome (autoconnect) 还是隔离 profile (rdp + launch-chrome) 还是指纹浏览器 (rdp + 用户提供 port)?" → 走对应路径 → 询问"要把 backend 偏好记进 memory 吗？" (★US4★)。**指纹浏览器（AdsPower / MultiLogin / GoLogin / 比特浏览器 等）走 rdp backend + user 提供端口，Skill 端零特殊处理**，详见 §A.1 末"支持的浏览器源"表 + daemon design-v2 §4.4。
- [ ] `SKILL.md` 全文：REPL 怎么用 + site-skill 找/调/写、memory 管理、Mode A 下的 popup 警告、4 个 user story 的标准姿势
- [ ] interaction-skills/*.md 沿用 browser-harness 18 个 + `captcha-handoff.md` + `solidify-protocol.md`（US3 prompt 模板）

依赖 daemon 在 v0.1 必须提供（详见 §D.2）：
- D.2.1–D.2.4：Mode A url / doctor / list-backends 契约
- **D.2.8 part A**：`browser-daemon active-tab --json` 子命令（v0.1 走 CLI；v0.2 Mode B 走 RPC）—— accuracy = `heuristic-recent-activate`
- D.2.12：`browser-daemon launch-chrome` 子命令（daemon-architect §5.5 已锁定 v0.1）
- D.2.13：`--backend X` flag（daemon 已有，零新增）
- 无 `BrowserDaemon.*` RPC 命名空间，`current_page()` 走 subprocess（v0.1 可接受）

Skill 在这个阶段的体验：
- `rdp + 独立 profile`（install 默认推荐路径）：完美，无打扰。
- `autoconnect`：`repl start` 必经；用户被指引"启动一次 Allow，之后零打扰"。
- `env`：完美（外部管 ws）。
- US1 (`current_page` + 当前页 one-shot)：✓ 通过 `browser-daemon active-tab` heuristic + auto switch_tab
- US2 (新 tab + in-flight memory)：✓ `remember()` lazy-create site dir，append in any frame
- US3 (post-task solidify ask)：✓ 最小提炼策略 + `propose_solidify()` 返回结构化建议，agent surface 给 user
- US4 (backend 偏好 in memory)：✓ `remember_preference()` + 启动时读 frontmatter + 传 `--backend` flag

### v0.2（Daemon Mode B 上线）

Skill 范围：
- [ ] `daemon_client.py` Mode B 实现 + `auto` 切换逻辑
- [ ] inline heredoc 在 Mode B 下变首选；文档去掉"反复弹窗"警告
- [ ] `BrowserDaemon.getActiveTab` / `subscribeFocus` 集成到 `current_tab()` / `ensure_real_tab()` —— REPL 默认 attach 到用户视觉前台 tab
- [ ] REPL 监听 `BrowserDaemon.activeTabChanged` → 用户切走时 warn agent "你现在在的 tab 不再是用户视野中的那个"
- [ ] `save --from-repl` 增加自动提炼：用 REPL 历史的 success-only 子集 + diff
- [ ] task selftest 24h cache（避免每次跑都 navigate 一次）
- [ ] `OUTPUT_SCHEMA = pydantic` optional 验证
- [ ] project-level site-skills (`./.browser-skill/site-skills/<site>/`)
- [ ] memory `forget` / `replace` API（需要 user 确认 prompt）
- [ ] cross-site `list_site_skills(query)` 的 LLM-free 评分升级（加 embedding cache, optional）

依赖 daemon 在 v0.2 必须提供：
- D.2.5–D.2.11：Mode B 完整契约（CDP 透传、sessionId multiplex、stale 关闭礼仪、`BrowserDaemon.*` 命名空间、单 attacher 规则、client 标签）
- D.3.1（uiState query）软需求，没有 Skill 也能用

### v0.3（与 Layer 3 联调）

Skill 范围：
- [ ] `browser-skill task ... --json-output` 严格 JSON 模式（Layer 3 编排用）
- [ ] task 之间共享一个 repl daemon ws（同 agent 跑 5 个 task 不开 5 个 ws）
- [ ] subscription 机制（借鉴 browser-cli sub）：从 git 拉别人写的 site-skills
- [ ] memory export 到 cloud（可选；用户授权）
- [ ] `selftest` 的 cron + dashboard：站点漂移监控

---

## 11. 开放问题

- [ ] **probe → 固化自动提炼的精度**（US3 前移到 v0.1 后**部分**解决，但仍粗糙）。v0.1 用 success-only filter + 启发式去探查；v0.2 加 `__BOOKMARK__` marker 协议（agent 在 REPL 里主动 print `__KEEP__` / `__SKIP__` 标记），让 agent 直接告诉 scaffolder 哪些步骤是 keeper。
- [ ] **`current_page()` accuracy 退化时 agent 应当怎么 prompt user**？v0.1 daemon 的 heuristic-recent-activate 在用户**手动点 tab 切换**后会 stale。Skill 拿到 `accuracy != "heuristic-recent-activate"` 时应该 raise 还是 warn 还是降级？v0.1 倾向"warn agent + 降级到 list_tabs[0]"，但 agent 拿到 warn 后默认行为不明确。要在 SKILL.md 里写一个 sample prompt。
- [ ] **`_detect_wall` 误报率**。简单 selector 启发式肯定误报（很多页面都有 `[class*=login]`）。v0.1 默认 off，站点 SKILL.md 显式 opt-in。
- [ ] **多 Chrome profile 切换**。用户可能想"工作 profile 用一个 daemon，个人 profile 用另一个"。v0.1 = 一次只一个，靠 env 切；v0.3 想做 namespace 级隔离。US4 偏好块也要扩展支持 per-profile。
- [ ] **Captcha 自动求助 UX**。`raise Captcha` 后 agent 是该自己截屏给 user 看，还是 Skill 应该提供 `await_human_solve()` 阻塞 API？v0.1 倾向纯 raise，让 agent 决定，但要在 `captcha-handoff.md` 写清楚 patterns。
- [ ] **memory 的版本控制 / merge**。两个 agent 进程同时 `remember(...)` 同一个 site → race condition。v0.1 用 file lock + truncate-append；v0.2 想加 conflict marker。US2 的 in-flight write 会让这个问题更频繁。
- [ ] **REPL daemon 的 idle timeout**。Daemon Mode A 下 Skill repl daemon 长驻 = 上游 ws 长驻 = 横幅长驻。daemon-architect 提议：Skill 在自己 idle 时调 `browser-daemon disconnect` 让上游 ws close → 横幅消失，下次 Skill 操作时 lazy 重开（autoconnect 弹窗）。这跟 Skill 自己的 idle policy 互补不冲突。v0.2 实装。
- [ ] **`form.submit()` 兼容性**。GitHub 的 CSRF 表单 pattern（§A.3 表）值得不值得提升到 core primitive `submit_csrf_form(form_selector)`？暂时不（一个特定 site 的 pattern，留 site-skill 里）。
- [ ] **agent 写错 task 怎么办**。`save` v0.1 没有 review 步骤——agent 写完直接 commit 到 site-skills/。US3 的解法是 propose → user confirm → solidify → selftest 验证，验证失败回滚。但 user confirm 走 agent 对话流，agent 自己可能"假装 confirm"。v0.2 想加"先写到 staging tasks/.draft/，下次成功跑过再 promote"的双阶段。
- [ ] **US4 用户偏好 vs 系统检测冲突**。用户 memory 里写 `preferred_backend: extension`，但当次启动检测到扩展没装。Skill 该 fallback 还是报错？~~v0.1 走 `fallback_chain` 字段~~ **v0.5 REVIEW.md F-8 retracted** — daemon-impl-2 F-5 确认 daemon 不实现自动 fallback；v0.5 行为是显式报错 + 走 install wizard 让 user 选下一个 backend（doctor-driven menu 标 "coming v0.x" / "available"）。`fallback_chain` 字段彻底从 schema 里删除。
- [ ] **指纹浏览器主动检测**（来自 vision review）。v0.1 install wizard 只**被动**接受 user 选 "指纹浏览器 (rdp + 端口)"；理想是**主动**扫常见路径（macOS `~/Library/Application Support/AdsPower Global/` / Windows `%LOCALAPPDATA%\AdsPower Global\` / `MultiLoginPortable/` 等）的特征文件，detect 到后 install wizard 主动提示 "检测到 AdsPower-style 指纹浏览器，要不要绑 rdp + port？"。v0.2 加。这一条 daemon 端做更合理（detection 跟 backend discovery 同源），届时跟 daemon-architect 协商谁来实装。

---

## 12. 命名 / 边界

为什么叫 **browser-skill** 而不是 `browser-harness-2` / `browser-repl` / `browser-agent` / `browser-tasks`？

- "skill" 是 agent 生态的现成术语（Claude / Codex / OpenAI Skills）—— 一个文件夹 + `SKILL.md` + 一组工具的组合。我们正是这个模式。
- "repl" 太窄，掩盖了 site-skills / memory 的存在。
- "agent" 错位 —— agent 是用我们的人，不是我们本身。
- "harness" 已经被 browser-harness 占了，且字面含义偏向"约束/绑定"，不包括知识沉淀。

**边界**（再强调一次）：

- Skill **不**实现 CDP discovery、不启动 Chrome、不管 ws lifecycle 长尾——daemon 的事。
- Skill **不**做 LLM 调度、planning、tool routing——agent 的事。
- Skill **不**做定时调度、跨 task 状态、apprise 通知——Layer 3 的事。
- Skill **只**做：拿到 ws 之后，给 agent 一组好用的浏览器原语 + 一套 site-skills 知识管理 + 一套 memory。

---

附：协作 / 反馈渠道

- 与 `daemon-architect` 的接口契约：见 §D.2 和发给 daemon-architect 的初步需求列表；收到他的反馈后会在本文件 §D 顶部 patch 一个"v1 contract"小节。
- 站点 skill 贡献：PR 到 `site-skills/<site>/`，schema 跟 browser-harness 的 domain-skills 一致——agent-generated 优先于手写。
