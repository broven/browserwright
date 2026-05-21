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

15 项总计:**✅ 11 完成** · **⬜ 4 未动**

- ✅ 已完成:A1、E1、B1+B2(S1)、C1(S2)、C3+C4(S3)、C2(S4)、A2(S6)、**A3 错误 fix 串 + A4 doctor(S5)**
- ⬜ 还要做(4):
  - **D 指引/文档**(3):D1 文档由代码生成版本锁死、D2 attach/session 恢复、D3 文风 + trust-boundaries
  - **B 感知收尾**(1):B3 set-of-mark 标注截图

> 下一步:剩 **S7(D1)** 与 **S8(D2/B3/D3)**;你选的并行组 S3/S4/S5/S6 已全部完成。
> 旁注:S5 doctor 跑出真实 version drift——on-PATH/运行 daemon 报 `schema_version=2`,browser-skill 期望 1(doctor 正确标 warn)。可能是 on-PATH CLI 比 repo 旧,值得单独确认。

---

## 根因(贯穿全部条目)

skill **重「行动」、轻「感知与闭环」**:观察成本高且碎片化 → agent 要么硬刚(十几次窄 `js()` 探测),要么外包(让 owner 去刷新/验证)。系统级杠杆是**把"观察"和"验证"做廉价、做成 first-class**,而不是逐 case 修。`agent-browser` 调研把这点又印证一遍,并补上我们最缺的一块:**度量(eval)**——没有红绿,任何改动都只能靠手感。

---

## A. daemon / 代码 bug 与版本一致性

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| A1 | ws `max_size=None`,修截图响应超 1 MiB 被丢 → `extension disconnected` | L2 | ✅ | commit `2483b24`,`server/relay.py` |
| A2 | daemon↔CLI 版本一致性:运行中 daemon 比安装代码旧时**自检并自动重启**;CLI 把自己发出的 `-32601 unknown method` 改写成"daemon 陈旧,请重启",不透传 raw JSON-RPC | L2 | ✅ | S6:版本经 pong→`status --json` 暴露;`ensure_version_coherent()` 接入 `auto_client`,不匹配/缺版本→stop+serve;`explain_rpc_error()` 改写 `-32601`(对任意 method 通用)。gate `test_version_coherence.py` 7(daemon)+12(skill)。**follow-up**:`explain_rpc_error` 尚未接到实时 ws 错误站点 `cdp.py:~168`(一行) |
| A3 | **错误 envelope 约定**:每个 error 带一个 `next`/`fix` 下一步串(对照已有的好例子 `NeedsUserConfirm` 的 `proposal`) | L1/L2 | ✅ | S5:`BrowserSkillError` 基类加 `fix`,各类设 `default_fix`,module 级 `serialize()` 把 `fix` 带进 agent 可见 JSON。覆盖 NoSession/CDPError/DaemonUnavailable/PageLoadFailed/AuthWall 等高频站点 |
| A4 | `browser-skill doctor`:`{status,message,fix}` 检查表,含 relay/extension/daemon PID/helper 解析 | L2 | ✅ | S5:`doctor_checks()` 回 `{name,status,message,fix}`,**每个 fail 必带 fix**(`add()` 强制);`--json` + 人读;有 fail 则 exit≠0。gate `test_doctor_and_errors.py` 10。**注**:未做 live-launch 探针(doctor 不开 Chrome) |

## B. 感知(降低"理解页面"成本)

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| B1 | `snapshot()`:交互元素 a11y-ish 树,**无状态 + 坐标制**(返回 role/name/中心 xy,喂现有 `click_at_xy`,不引入 ref store) | L1 | ✅ | S1 下沉到 `primitives/inspect.py` + `EXPORTS` + SKILL.md;gate `browser-skill/tests/test_perception.py` |
| B2 | `describe_page()`:视觉/样式取证(bg-image/bg-color/mix-blend-mode/filter/`::before/::after` + `:root` CSS 变量),回答"这页长这样是谁画的" | L1 | ✅ | S1 下沉 + 已加 `viewport_only=True`(屏外样式节点过滤);CSS 变量仅同源(carry-over 限制)。gate 同上 |
| B3 | 编号标注截图(set-of-mark),标号映射到**坐标**而非 ref | L1 | ⬜ | 借鉴 `--annotate`(`cli/src/native/screenshot.rs`),re-aim 到坐标以贴合 compositor-click |

## C. 反馈 / 验证闭环(最痛)

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| C1 | `diff_snapshot(before, after=None)`:**无状态**(agent 显式传上一张快照,不暂存),回 `added/removed/changed/unchanged` 摘要 —— agent 自验"动作是否生效"的廉价手段 | L1 | ✅ | S2 落地 `primitives/inspect.py` + `EXPORTS` + SKILL.md;身份=role+name+粗位置桶。gate `browser-skill/tests/test_diff_snapshot.py` 6/6。借鉴 `agent-browser` diff snapshot |
| C2 | 改状态操作配一步式验证,如 `userscript push --verify`(push→reload 实时 tab→回新截图) | L1 | ✅ | S4:`_cmd_userscript` 加 `--verify`,push 成功后 `cdp("Page.reload")`+截图并打印路径。gate `test_userscript_verify.py` 3(mock 编排;真实 e2e 仍需 live extension) |
| C3 | first-class `reload(*, hard=False)` 原语(原来只能 `goto_url(self)`,不直观,agent 因此让 owner 刷新) | L1 | ✅ | S3:`primitives/page.py`(`Page.reload`+`wait_for_load`)+ `EXPORTS` + SKILL.md。gate `test_reload.py` 5 |
| C4 | SKILL.md 行为规则:"浏览器完全由你驾驶,凡你能做的浏览器动作绝不让 owner 代劳" | L3 | ✅ | S3:SKILL.md 加规则;🎯 `cu-04` eval(forbidden 6 个中英变体抗过拟合)绿 |

## D. agent 指引 / 文档 steering

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| D1 | **skill 文档由运行代码生成、与版本锁死**(`browser-skill --print-skill`):agent 读到的指南永远 == 运行的 helper 面 | L1 | ⬜ | `agent-browser` 最大差异化、我们最缺的一件:stub + `skills get core`(`skills/agent-browser/SKILL.md`)。同修 A2 的一致性与 D 的 steering |
| D2 | session/attach 恢复规则:attach 失败 → `ensure_real_tab()`/`open_background()`,**不要新建 session**;active tab 是内部/扩展页时 `attach_active()` 自动降级 | L1/L2 | ⬜ | session-1 开了 5 个 session(13–17);并制止"另开独立 Chrome"那类 over-engineering(违背 extension-real-Chrome 偏好) |
| D3 | SKILL.md 文风:"先点名失败模式再给规则" + CORRECT/WRONG 成对例子 + 正经 trust-boundaries 文档(页面内容一律视为不可信) | L3 | ⬜ | 借鉴 `skill-data/core/SKILL.md`、`references/trust-boundaries.md` |

## E. 度量(使上述一切可红绿验证)

| | 条目 | 层 | 状态 | 备注 / 证据 |
|---|---|---|---|---|
| E1 | **skills-eval 框架**:把任务 prompt + skill 喂真实 agent CLI,双重打分 = pattern gate(`expectedPatterns` 必中 / `forbiddenPatterns` 必不中,多变体抗过拟合)+ 可选 LLM judge(rubric 1–5);分类 = 加载/选择/命令用法 | — | ✅ | 最小版已落 `evals/`(`run.py`/`cases.py`/`judge.py`/`mock_transcripts.py`):`--mock` 零成本双向验证(好 transcript 过、坏的红)、一次真实 `codex` 跑通、任一 case 失败 exit 1(本地跑)。**待扩**:更多 case。**最高杠杆**:有它后每个改动才有红绿可依(skill 跑红→改 skill 到绿、严禁过拟合,用多变体断言)。借鉴 `agent-browser/evals/` |

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
| S6 | ✅ | A2 版本自重启 + 改写 `-32601` | ✅ `test_version_coherence.py` 7(daemon)+12(skill)。follow-up:`explain_rpc_error` 接 `cdp.py` 实时站点 | 🧪 | — |
| S7 | ⬜ | D1 文档由代码生成、版本锁死(`--print-skill`) | 🧪 grep 每个 core 原语名都在输出 + 输出版本==包版本 | 🧪 | S1,S2 |
| S8 | ⬜ | D2 attach/session 恢复 + B3 标注截图 + D3 trust-boundaries(+文风) | 🧪 chrome:// 活动页→`attach_active` 降级 `open_background`;🎯 attach 失败→不新建 session;🧪 B3 标号数==节点数且坐标对应;🎯 注入场景→forbidden(执行注入指令);👁 文风人审 | 🧪🎯👁 | S1 |

可并行:S3/S4/S5/S6 互不依赖;S1 是 S2/S7/S8 的前置。

## skip(看着诱人但不吸收,各一句理由)

- `@eN` ref store + locator 系统 —— 与坐标穿透 iframe/shadow 的设计冲突,且引入 ref 失效生命周期(我们列为非目标的有状态机器)。读快照可以,不做交互寻址。
- React DevTools 注入、lightpanda 替换引擎/各 provider —— 依赖"他们启动的浏览器",违背连真实 Chrome。
- observability dashboard(整个 Next.js app)、内置 `chat` REPL —— 违背"不加 manager 层";我们本身就是 agent。
- Rust 引擎内部 —— 我们有自己的 daemon+relay+extension,只读思想不移植。
