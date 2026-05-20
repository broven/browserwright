# v2 SDK sub-agent E2E — design

**Status**: design approved 2026-05-20 (brainstorm with @Randy). Ready for implementation plan.
**Upstream**: `docs/plans/2026-05-20-v2-sdk-subagent-handoff.md` (handoff), `docs/plans/2026-05-19-real-extension-e2e-design.md` (§4 "v2 sub-agent path").

> **路径更正**：handoff doc 写的 "sub-agent 看 `browser-skill/SKILL.md`" 是笔误。
> agent-facing 文档实际在 `skill/SKILL.md`（+ `skill/memory.md` + `skill/tasks.md`）。
> `browser-skill/{README,design,ONBOARDING}.md` 是给开发者看的，不是 agent 入口。

## TL;DR

v1（fixture-style E2E，已合 main，291 unit / 11 e2e）测的是**代码层通不通** —— pytest
直接 subprocess 调 `browser-skill`，Mock 掉 agent。v2 补一个**新维度**：真起一个 Claude
Agent SDK 的 sub-agent，只让它看 `skill/` 下的 `.md` 文档，看 **skill 文档 + 代码组合
起来能不能让真实 Code Agent 流畅使用浏览器**。同时验证代码 + 文档可读性 + skill 触发质量。

v2 不替代 v1，两者独立共存。

## 工作模式：case = 北极星 spec，skill 是被测对象

这几个 case 是为**最终目标状态**设计的，**不是**验证 skill 现状。现在的 skill 大概率满足
不了全部 case。工作模式是 test-driven：

- case 措辞/断言确认无误的前提下，skill 跑红 → **改 skill**（文档 `skill/*.md` 或代码
  `browser-skill/src/`）直到通过，而不是改 case 迁就现状。
- **严禁过拟合**：不为过测试而特化 skill。不 hardcode 测试用例的 URL/措辞，不加针对 test 的
  special-case 分支。每个改动必须是"让 skill 对**这一类**任务更好用"的通用改进 —— 任何换个
  措辞 / 换个站点就失效的改法都是过拟合，要拒绝。
- 红/绿之外加一道**过拟合 review**：每次为过 case 改了 skill，自问"这个改动对真实用户的同类
  任务也有用吗？还是只为骗过这条 test？"
- **多变体抵御过拟合**：每个 case 配多个话术变体（中英文、不同措辞、不同站点）。变体共用一套
  断言；若只有一种措辞能过，说明改窄了，是过拟合信号。

## 框架选型：promptfoo

调研了 promptfoo vs Inspect AI（UK AISI）。结论：**promptfoo，中-高置信度**。

| 需求 | promptfoo | Inspect AI |
|---|---|---|
| 包 Claude SDK sub-agent | ✅ Python custom provider | ✅ `agent_bridge`（更好，但无 claude-agent-sdk 例子，glue 自己写） |
| Session-scoped daemon+Chrome | ✅ `extensions:` + `beforeAll`/`afterAll` | ≈ `on_run_start`/`on_run_end`（但 sandbox per-sample，跟长跑 host daemon 打架） |
| 文件系统副作用断言 | ✅ `assert: type: python` + `get_assert()` | ≈ scorer（结构更清晰） |
| Mock 用户回答 | ✅ 在 provider 内部拦截注入 | ❌ 无 canonical 模式 |
| Debug trace UI | 普通 HTML | ✅ `inspect view`（护城河） |

决策理由：规模仅 ~5 类 case × 变体（≈10-15 sample），Inspect 的 agent-native 优势收不回
成本；Inspect 的 per-sample sandbox 跟我们 session 级长跑真 Chrome 冲突；Case B/C 需要
mock 用户确认，promptfoo 自然支持。**若后期 >30 sample 或需跨 eval 对比，再考虑 Inspect。**

## 架构

放在 `browser-skill/tests/agent-e2e/`（贴近被测文档 `skill/`，与 v1 的
`browser-daemon/tests/e2e/` 分离）。四个组件：

1. **`provider.py`** — promptfoo custom provider。`call_api()` 内部用 Claude Agent SDK 起
   sub-agent：配工具集、system prompt（指向 workspace 的 `skill/SKILL.md`）、model、budget；
   拦截 sub-agent 的"问用户"动作注入确定性回答；返回 `{output, tokenUsage, metadata}`。
2. **`hooks.py`** — promptfoo `extensions:`。`beforeAll` 起隔离 daemon + CfT + patched
   extension（复用 v1 `_patch_extension`），prep workspace；`afterAll` 拆；`beforeEach`
   reset workspace 到干净态。
3. **`scorers/*.py`** — 每 case 一组独立 Python 断言：查 daemon 状态、读 workspace 文件系统、
   检查 sub-agent trace。
4. **`promptfooconfig.yaml`** — 声明 case × 变体，绑 provider + scorers。

## 隔离边界 & workspace

在 v1 隔离矩阵基础上再隔离一层，避免撞 v1（v1/v2 可并行）：

| 维度 | 生产 | v1 | v2 |
|---|---|---|---|
| daemon extension port | 19989 | 29989 | **39989** |
| RDP port | default | 29990 | **39990** |
| `BD_NAME` | default | bd-e2e | **bd-agent-e2e** |

**Workspace 布局**（固定 `browser-skill/tests/agent-e2e/_workspace/`，`beforeEach` reset）：

```
_workspace/
  skill/
    SKILL.md          # symlink → 真文档（只读 — 测的就是它）
    tasks.md          # symlink → 真文档（只读）
    memory.md         # 拷贝（可写 — Case B 写偏好到这；不污染 repo）
  .browser-skill/     # BS_HOME 指这里（空初始；Case C/D 写 task / site-memory）
    site-skills/
```

**两手隔离**（确认依据 `src/browser_skill/memory/global_mem.py:27` `BS_HOME` env）：
1. `BS_HOME=_workspace/.browser-skill` → site-skills / global / tasks 全落 workspace。
2. SKILL.md 偏好写到相对路径 `./memory.md`，sub-agent cwd 在 `_workspace/skill/` → 改副本。

daemon + Chrome 是 session 级，不随 `beforeEach` reset（省启动成本）。

## Sub-agent provider 细节

`call_api(prompt, options, context)`：

1. **起 sub-agent**（Claude Agent SDK）：
   - `cwd` = `_workspace/skill/`
   - `model` = **Sonnet 起步**（后期可加 opus matrix）
   - `allowed_tools` = Bash, Read, Write, Edit, Grep, Glob（近似真 Code Agent）
   - **工具守卫**：Read/Grep 拦 `.py` 和 workspace 外路径；Bash 只放行 `browser-skill`/
     `browser-daemon` 开头命令（防 `cat` 源码绕过 Read 守卫）
   - `system_prompt`：极简 —— "你是 Code Agent，用户让你做下面的事，可用工具见 SKILL.md。"
     **不**塞实现细节、不给示例代码（测文档的意义所在）
   - `env`：`BS_HOME` / `BD_NAME` / `BD_EXTENSION_PORT=39989` 等隔离 env
2. **Mock 用户**（B/C 共享）：sub-agent 文本问"save as task?"/"确认写偏好?"并停下等输入时，
   provider 检测 stop reason + 末条消息含问句 → 注入预设回答（默认 `"yes, go ahead"`）继续
   loop。每 case 可在 YAML 配自己剧本。
3. **预算护栏**：硬 cap `max_turns`（建议 25）+ token cap。超了停，标 `budget_exceeded`。
4. **返回 metadata**：完整 tool_trace、turns 数、是否问了用户、连续失败 Bash 次数。

## Case 形态 & 断言

独立验证原则：**不信 sub-agent 自评，harness 自己查 daemon + 文件系统 + trace。** 无 verdict.json
协议（真实 Code Agent 不写 verdict.json，强制写会污染流畅度测量）。

### Case A — 连接 + 打开 + 总结
- 话术：`"用浏览器打开 https://example.com，告诉我这个页面在讲什么。"`
- 断言：
  1. [查 daemon] harness 自己 `page_info()` → url 含 `example.com`
  2. [trace] 用了 `browser-skill` 且成功连上；>2 次连续失败 Bash = 走弯路警告
  3. [output] `llm-rubric`：输出含页面要点（"example domain / illustrative"）

### Case B — 保存偏好到 memory.md
- 话术：`"我以后做这类自动化都用 extension backend 连我日常 Chrome，记住这个偏好。"`
- 路径：读 `memory.md` → 请求确认 → mock 注入 yes → Write/Edit 到 `## User preference`
- 断言：
  1. [fs] workspace 副本 `memory.md` 的 `## User preference` 出现 extension backend 偏好
  2. [fs] 未破坏其它 section（backend capability table 完好）
  3. [trace] **没请求确认 = warning**（写对就算过；与 C 不对称，B 主考"写对没"）

### Case C — 固化 task
- 话术：`"每天早上帮我抓 Hacker News 首页前 5 条标题。"`（"每天"=recurring + feed-like，命中
  SKILL.md solidify 触发条件）
- 路径：完成 flow → 主动问 "save as task?" → mock yes → 读 `tasks.md` → Write task 到
  `$BS_HOME/site-skills/news.ycombinator.com/tasks/`
- 断言：
  1. [trace] **主动问了 = 必须；不问 = fail**（"主动建议固化"是 C 的核心考点）
  2. [fs] task 文件落在 `$BS_HOME/site-skills/<host>/tasks/` 对的位置
  3. [内容] 合法 Python + 含抓取逻辑骨架（能 import / 结构对）

### Case D — site memory（明示触发）
- 话术：明示要求写，如 `"…顺便把这个站点需要注意的坑记到 site memory 里。"`（SKILL.md 无
  site-memory 触发条款，故只测"能不能正确写到对的位置+格式"，不测主动性）
- 路径：操作 → append 到 `$BS_HOME/site-skills/<host>/memory.md`
- 断言：
  1. [fs] site memory 文件 append 了经验条目
  2. [内容] 条目是站点级 durable 经验（append-only，不覆盖既有）

### Case E — skill 自动触发（测 description 质量）
- **根本不同维度**：测 triggering 不是 usage。不需要真 daemon + 真 Chrome（agent 一旦决定
  "用 browser-skill"，case 即结束）。独立轻量 provider。
- 实现：**轻量文本分类**。system prompt 列 `skill/SKILL.md` frontmatter 的真实
  `description` + 几个干扰 skill 的 description（find-domain / context7 / frontend-design），
  喂"该触发但没明说用浏览器"的任务（中英文变体，如"帮我看看 example.com 写了啥并截图"、
  "scrape 这个页面标题"），看 agent 输出选不选 browser-skill。
- 范围：**只测召回**（该用时会用）；不测过度触发。
- 断言：agent 选中 browser-skill。

## 失败诊断 & artifacts

promptfoo 原生 HTML/JSON report。在此之上每 case fail 时 scorer dump 到 `_artifacts/<case>/`：
- `agent_trace.json` — sub-agent 完整 tool 调用序列 + 每轮输出（**最重要**：看哪步走错、读了
  哪些文档、Bash 失败在哪）
- `daemon.log` — 隔离 daemon stderr
- `failure.png` — 失败时刻 `capture_screenshot()`
- `workspace_snapshot/` — `memory.md` + `.browser-skill/site-skills/` 树快照
- `env.txt` — 隔离 env / model / budget

成功不写。复用 v1 artifact 思路。

## 交付顺序（prove-it first）

1. **骨架**：`hooks.py`（起隔离 daemon+CfT，复用 v1 `_patch_extension`）+ workspace prep +
   `provider.py` 最小版（起 sub-agent、返回 output+trace）。无断言。
2. **Case A 跑通** ← 第一里程碑（handoff doc 的 prove-it test）。证明 promptfoo + SDK +
   真 daemon 骨架活了。
3. **mock-user 组件**（B/C 共享）。
4. **Case B**（偏好写入 + 确认 warning）。
5. **Case C**（含"主动问 = fail"断言）。
6. **Case D**（明示写 site-memory）。
7. **Case E**（独立轻量，不依赖 daemon，可随时插）。
8. **README** + sonnet→opus matrix + CI 决策。

每个 case 步骤（2、4-7）走 TDD 循环：**写 case + 变体 → 跑（大概率红，因为 case 是北极星）
→ 改 skill 到绿 → 过拟合 review**。骨架（步骤 1）只验通路、不改 skill。

## 开放问题（留给实现计划）

1. Claude Agent SDK 的"问用户后停下等输入"具体事件形态 —— 需对照 SDK 实测，确定 provider
   怎么 detect + inject（stop reason？特定 tool？文本启发式？）。
2. 工具守卫（Read 拦 .py、Bash 命令白名单）在 SDK 里用 hooks 还是 permission callback 实现 —— 查 SDK。
3. Case C task 文件"内容符合预期"的断言深度：只检查结构/可 import，还是真跑一遍？v1 思路是
   action-level，倾向只验结构 + 可 import。
4. Token / turns budget 的具体数值需要跑几次 Case A 校准。
5. CI：v1 推到本地优先，v2 同样先本地；headed Chrome + xvfb 的 CI 化二者一起做。
