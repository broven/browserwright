# Agent 体验改进 — 跟踪文档

> **性质**:常驻 living tracker,随完成进度更新本文 status,不是一次性 plan。
> **来源**:`session-1.txt`(真实 code agent 用 skill 的全过程)的摩擦分析 + `snapshot/describe_page` 原语原型 + 对 `vercel-labs/agent-browser` 的借鉴调研(2026-05-21)。
> **原则**:代码是唯一事实来源,引用尽量带 `path:line`;每条改进标注**层**(L1 skill 接口 / L2 daemon 代码 / L3 agent 指引)。
> **明确不做**:`bash-compound-allow` 逐行审批噪音 —— 那是 owner 自己的第三方权限插件,与本仓库无关,owner 会自行删除。

## status 图例
- ✅ done(已落地到 core 并验证)
- 🟡 prototype(已实现但只在 `agent_helpers.py` 热加载层,未下沉 core)
- ⬜ todo
- ⏸ 暂缓 / 待前置项

## 进度快照(2026-05-21)

15 项总计:**✅ 15 完成 · ⬜ 0** 🎉  —— 8 个阶段(S1–S8)全绿

- ✅ 全部完成:A1、E1、B1+B2(S1)、C1(S2)、C3+C4(S3)、C2(S4)、A3+A4(S5)、A2(S6)、**D1 `--print-skill`(S7)**、**D2 attach 恢复 + B3 标注截图 + D3 trust-boundaries(S8)**

**收尾 — 全部已处理**:
- ✅ **D3 文风**人审通过:`skill/trust-boundaries.md` + SKILL.md 两段(名失败模式→规则→CORRECT/WRONG,有声音、不泛泛),无需改。
- ✅ **S6 follow-up**:`explain_rpc_error` 改为 staticmethod 并接到实时站点 `cdp.py`(新 `_rpc_error_fix`,-32601 时给具名"daemon 陈旧"fix);`test_version_coherence.py` 加 wiring 测试。
- ✅ **version drift 根因=S5 bug**(doctor schema 检查只认 `1`,而当前 daemon 发 `2`):改为接受 `_SUPPORTED_DOCTOR_SCHEMAS=(1,2)`,并修了那条用 v1 掩盖问题的测试(加回归断言)。**非陈旧 CLI**——on-PATH `browserwright` 指向 repo venv、`browserwright-daemon` 0.5.3。

---

## 根因(贯穿全部条目)

skill **重「行动」、轻「感知与闭环」**:观察成本高且碎片化 → agent 要么硬刚(十几次窄 `js()` 探测),要么外包(让 owner 去刷新/验证)。系统级杠杆是**把"观察"和"验证"做廉价、做成 first-class**,而不是逐 case 修。`agent-browser` 调研把这点又印证一遍,并补上我们最缺的一块:**度量(eval)**——没有红绿,任何改动都只能靠手感。

---

## A. daemon / 代码 bug 与版本一致性

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| A1 | ws `max_size=None`,修截图响应超 1 MiB 被丢 → `extension disconnected` | L2 | ✅ | commit `2483b24`,`server/relay.py` |
| A2 | daemon↔CLI 版本一致性:运行中 daemon 比安装代码旧时**自检并自动重启**;CLI 把自己发出的 `-32601 unknown method` 改写成"daemon 陈旧,请重启",不透传 raw JSON-RPC | L2 | ✅ | S6:版本经 pong→`status --json` 暴露;`ensure_version_coherent()` 接入 `auto_client`,不匹配/缺版本→stop+serve;`explain_rpc_error()` 改写 `-32601`(对任意 method 通用,已设 staticmethod 并接到实时站点 `cdp.py` `_rpc_error_fix`)。gate `test_version_coherence.py` 7(daemon)+13(skill) |
| A3 | **错误 envelope 约定**:每个 error 带一个 `next`/`fix` 下一步串(对照已有的好例子 `NeedsUserConfirm` 的 `proposal`) | L1/L2 | ✅ | S5:`BrowserwrightError` 基类加 `fix`,各类设 `default_fix`,module 级 `serialize()` 把 `fix` 带进 agent 可见 JSON。覆盖 NoSession/CDPError/DaemonUnavailable/PageLoadFailed/AuthWall 等高频站点 |
| A4 | `browserwright doctor`:`{status,message,fix}` 检查表,含 relay/extension/daemon PID/helper 解析 | L2 | ✅ | S5:`doctor_checks()` 回 `{name,status,message,fix}`,**每个 fail 必带 fix**(`add()` 强制);`--json` + 人读;有 fail 则 exit≠0。gate `test_doctor_and_errors.py` 10。**注**:未做 live-launch 探针(doctor 不开 Chrome) |

## B. 感知(降低"理解页面"成本)

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| B1 | `snapshot()`:交互元素 a11y-ish 树,**无状态 + 坐标制**(返回 role/name/中心 xy,喂现有 `click_at_xy`,不引入 ref store) | L1 | ✅ | S1 下沉到 `primitives/inspect.py` + `EXPORTS` + SKILL.md;gate `browserwright/tests/test_perception.py` |
| B2 | `describe_page()`:视觉/样式取证(bg-image/bg-color/mix-blend-mode/filter/`::before/::after` + `:root` CSS 变量),回答"这页长这样是谁画的" | L1 | ✅ | S1 下沉 + 已加 `viewport_only=True`(屏外样式节点过滤);CSS 变量仅同源(carry-over 限制)。gate 同上 |
| B3 | 编号标注截图(set-of-mark),标号映射到**坐标**而非 ref | L1 | ✅ | S8:`capture_screenshot(annotate=True)` 从 `snapshot()` 画 `[N]` 角标,回 legend `[{n,role,name,x,y}]`(无 ref)。gate `test_annotate_screenshot.py` 3 |

## C. 反馈 / 验证闭环(最痛)

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| C1 | `diff_snapshot(before, after=None)`:**无状态**(agent 显式传上一张快照,不暂存),回 `added/removed/changed/unchanged` 摘要 —— agent 自验"动作是否生效"的廉价手段 | L1 | ✅ | S2 落地 `primitives/inspect.py` + `EXPORTS` + SKILL.md;身份=role+name+粗位置桶。gate `browserwright/tests/test_diff_snapshot.py` 6/6。借鉴 `agent-browser` diff snapshot |
| C2 | 改状态操作配一步式验证,如 `userscript push --verify`(push→reload 实时 tab→回新截图) | L1 | ✅ | S4:`_cmd_userscript` 加 `--verify`,push 成功后 `cdp("Page.reload")`+截图并打印路径。gate `test_userscript_verify.py` 3(mock 编排;真实 e2e 仍需 live extension) |
| C3 | first-class `reload(*, hard=False)` 原语(原来只能 `goto_url(self)`,不直观,agent 因此让 owner 刷新) | L1 | ✅ | S3:`primitives/page.py`(`Page.reload`+`wait_for_load`)+ `EXPORTS` + SKILL.md。gate `test_reload.py` 5 |
| C4 | SKILL.md 行为规则:"浏览器完全由你驾驶,凡你能做的浏览器动作绝不让 owner 代劳" | L3 | ✅ | S3:SKILL.md 加规则;🎯 `cu-04` eval(forbidden 6 个中英变体抗过拟合)绿 |

## D. agent 指引 / 文档 steering

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| D1 | **skill 文档由运行代码生成、与版本锁死**(`browserwright --print-skill`):agent 读到的指南永远 == 运行的 helper 面 | L1 | ✅ | S7:`skill_doc.render()` 从 `EXPORTS`(签名+docstring 首行)+ `__version__` 运行时生成,新增/删原语零改自动同步。gate `test_print_skill.py` 5(版本+全 EXPORTS 成员) |
| D2 | session/attach 恢复规则:attach 失败 → `ensure_real_tab()`/`open_background()`,**不要新建 session**;active tab 是内部/扩展页时 `attach_active()` 自动降级 | L1/L2 | ✅ | S8:`attach_active()` 对非可附着内部 URL 自动降级 `open_background`;SKILL.md 加恢复规则。gate `test_attach_recovery.py` 5 + `cu-05` eval(forbidden 新建 session 多变体) |
| D3 | SKILL.md 文风:"先点名失败模式再给规则" + CORRECT/WRONG 成对例子 + 正经 trust-boundaries 文档(页面内容一律视为不可信) | L3 | ✅ | S8:新 `skill/trust-boundaries.md` + SKILL.md "Trust boundaries"/"Attach failed" 段(名失败模式 + CORRECT/WRONG)。gate `cu-06` 注入 eval。**文风本身只能人审** |

## E. 度量(使上述一切可红绿验证)

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| E1 | **skills-eval 框架**:把任务 prompt + skill 喂真实 agent CLI,双重打分 = pattern gate(`expectedPatterns` 必中 / `forbiddenPatterns` 必不中,多变体抗过拟合)+ 可选 LLM judge(rubric 1–5);分类 = 加载/选择/命令用法 | — | ✅ | 最小版已落 `evals/`(`run.py`/`cases.py`/`judge.py`/`mock_transcripts.py`):`--mock` 零成本双向验证(好 transcript 过、坏的红)、一次真实 `codex` 跑通、任一 case 失败 exit 1(本地跑)。**待扩**:更多 case。**最高杠杆**:有它后每个改动才有红绿可依(skill 跑红→改 skill 到绿、严禁过拟合,用多变体断言)。借鉴 `agent-browser/evals/` |

## F. v0.6.4 eval batch — 2026-05-28 ZER-48 反馈

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| F1 | `remember_preference` 写 frontmatter 时保留 `global.md` body | L1 | ✅ | `GlobalMemory.set_preference()` 已保留 body,新增回归覆盖 body 不变 |
| F2 | `NeedsUserConfirm.fix` 不再误导 `confirm=True`;偏好写支持 `commit=True` | L1 | ✅ | `errors.py` 默认 fix 改为 `confirm=False`;`remember_preference(..., commit=True)` 作为清晰别名 |
| F3 | `userscript push --verify` 绑定 `-s/--session/BD_SESSION` 后再 reload/screenshot | L2 | ✅ | `_cmd_userscript()` 转发 session 到 daemon,verify 段调用共享 session binder |
| F4 | `browserwright task <site>/<name>` 支持 session binding | L2 | ✅ | 支持 `browserwright -s <id> task ...`、task 内 `--session`、`BD_SESSION`;`--output json` alias |
| F5 | `run_tasks_concurrent` 走 task_runner isolated session | L2 | ✅ | `_run_one()` 调 `run_task(..., isolated=True)`,保持顺序与错误聚合 envelope |
| F6 | 文档同步原语名与 Playwright 主路径 | L1/L3 | ✅ | runtime guide / skill shell 改为 `page.goto`、Playwright locator、显式 import internal primitives |
| F7 | `--print-skill` 区分 inline 默认 namespace 与可 import internal primitives | L1 | ✅ | `skill_doc.render()` 增加 `browserwright.primitives` 列表与导入说明 |
| F8 | `page.goto()` 后 target 同步 | L1 | ⏸ | 当前 Phase C steady-state 由 resident executor 持有 live `page`;legacy inspect 原语仍基于 `current_target_id`,需 facade target 映射设计后补 |
| F9 | 降低 snapshot 截断风险并显式回报 annotate 截断 | L1 | ✅ | REPL `snapshot(max_chars=20000)`;annotated screenshot 结果增加截断信号 |
| F10 | `doctor` 上浮 backend `ux_warning` | L2 | ✅ | `doctor_checks()` 为 raw backend warning 生成顶层 warn check |
| F11 | 子命令 help 覆盖 wrapper flags | L2 | ✅ | `browserwright task --help` / `browserwright userscript --help` 走本地 help |
| F12 | SKILL/runtime 增加抽文本、复杂 quoting、memory 决策规则 | L1/L3 | ✅ | `skill_runtime.md` + `skill/SKILL.md` 更新 |
| F13 | session UX:新建成功 stderr 提示、JSON alias、prune 默认 24h | L2 | ✅ | stdout 仍裸 sid;`session list --output json`;`session prune` 默认 24h |
| F14 | daemon status socket 暂不可达时短重试并标记 transient | L2 | ✅ | `status --json` 增加 `probe_state` |
| F15 | daemon serve 已有实例提示 status/restart | L2 | ✅ | 现有 single-daemon guard 保持 exit 1,提示改为 `status`/`restart` |
| F16 | `--output json` alias | L2 | ✅ | task/list-tasks/session list/doctor 支持 alias;execute unknown args 已显式 usage error |

---

## 建议推进顺序

1. ~~**E1**(eval 框架最小版)——先有度量。~~ ✅ 完成(`evals/`)
2. 下沉 **B1/B2** ✅(S1 完成)。⬅ **下一步:C1 `diff_snapshot()`(S2)**——补最痛的验证闭环,复用已建感知。
3. **A3 错误带 `fix` 串** → 长成 **A4 `doctor`**;**A2 版本自重启**。
4. **D1 版本锁死文档**(结构性,修 A2/D 两头)。
5. 收尾:**B2** 补 viewport-only 后下沉、**B3** 标注截图、**C2/C3/C4** 验证与驾驶规则、**D2/D3** 指引与文风。

## 执行阶段(gate-first,交 subagent)

**原则**:每阶段**先写验证 artifact(test/eval case)并确认 RED**,再让 subagent 实现到 GREEN,最后做一道过拟合自审。验证档:🧪 pytest 确定性 / 🌐 live 断言 / 🎯 `evals/` 行为门(`--mock` 确定 + 多变体抗 flaky)/ 👁 只能人审。

| 阶段 | 状态 | 内容 | 验证门 | 档 | 依赖 |
|---|---|---|---|---|---|
| S1 | ✅ | 下沉 B1 `snapshot()` + B2 `describe_page()`(含 viewport-only),并写进 SKILL.md 原语清单 | ✅ `test_perception.py` 7/7 + evals `--mock` 6/6。**注**:4 条 surface 断言离线 durable;4 条行为断言需本地 Chromium(没有则 `pytest.skip`) | 🧪🎯 | — |
| S2 | ✅ | C1 `diff_snapshot()` | ✅ `test_diff_snapshot.py` 6/6(add/remove/change 三类断言 + 无 `after` 自取快照) | 🧪 | S1 |
| S3 | ✅ | C3 `reload()` + C4 "你是司机"规则 | ✅ `test_reload.py` 5 + `cu-04` eval 绿(reload 重置断言 + 司机规则 forbidden 多变体) | 🧪🎯 | — |
| S4 | ✅ | C2 一步式 verify(`userscript push --verify`) | ✅ `test_userscript_verify.py` 3(mock 编排:push→reload→截图;真实 e2e 需 live extension) | 🧪 | — |
| S5 | ✅ | A3 错误带 `fix` 串 + A4 `doctor` | ✅ `test_doctor_and_errors.py` 10 + 全套 324 无回归;`serialize()` 带 fix 已独立复核 | 🧪 | — |
| S6 | ✅ | A2 版本自重启 + 改写 `-32601`(已接 `cdp.py` 实时站点) | ✅ `test_version_coherence.py` 7(daemon)+13(skill) | 🧪 | — |
| S7 | ✅ | D1 文档由代码生成、版本锁死(`--print-skill`) | ✅ `test_print_skill.py` 5(44 callables 全覆盖 + 版本) | 🧪 | S1,S2 |
| S8 | ✅ | D2 attach/session 恢复 + B3 标注截图 + D3 trust-boundaries(+文风) | ✅ `test_attach_recovery.py` 5 + `test_annotate_screenshot.py` 3 + evals `cu-05`/`cu-06`;文风待人审 | 🧪🎯👁 | S1 |

可并行:S3/S4/S5/S6 互不依赖;S1 是 S2/S7/S8 的前置。

## skip(看着诱人但不吸收,各一句理由)

- `@eN` ref store + locator 系统 —— 与坐标穿透 iframe/shadow 的设计冲突,且引入 ref 失效生命周期(我们列为非目标的有状态机器)。读快照可以,不做交互寻址。
- React DevTools 注入、lightpanda 替换引擎/各 provider —— 依赖"他们启动的浏览器",违背连真实 Chrome。
- observability dashboard(整个 Next.js app)、内置 `chat` REPL —— 违背"不加 manager 层";我们本身就是 agent。
- Rust 引擎内部 —— 我们有自己的 daemon+relay+extension,只读思想不移植。
