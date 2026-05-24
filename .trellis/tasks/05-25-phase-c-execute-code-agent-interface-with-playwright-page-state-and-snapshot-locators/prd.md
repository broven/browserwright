# Phase C: execute(code) agent interface — real Playwright page/state + snapshot locators

## Goal

照抄 playwriter 的 **agent 接口层**,真正消除 code agent 的"狂开 tab / 爱截图不用 snapshot"。Phase A 已让真 Playwright 能经 daemon CDP facade 驱动 rdp+extension 后端(含高层 API)。Phase C 把这能力交到 agent 手里:让 agent 写 Playwright 代码、操作一个注入的 `page` 句柄、用 snapshot(行即 locator)定位,并以 prompt 约定固化 tab 纪律。

## 背景/前提(已落地)

- Phase A:`chromium.connect_over_cdp(ws://daemon)` 可驱动两后端;高层 `new_page()/goto()` 在 extension 跑通。契约见 `.trellis/spec/backend/playwright-cdp-facade.md`。
- 执行模型:`browserwright <<'PY' … PY` heredoc = **独立进程、in-process `exec`**(`repl/inline.py`),命名空间由 `repl/_namespace.py:build_globals()` 注入 EXPORTS。**进程间无 in-process 状态**(持久 live 对象是 Phase B)。
- 现有 agent 原语 43 个(`open/goto_url/snapshot/click_at_xy/...`),`open()` 永远新开 tab(tab 爆炸根因)。
- daemon ledger 持久化 session 的 `current_target_id`(跨 heredoc)。

## Decision (ADR-lite)

**Fork 1(已定):全面对齐 playwriter,推翻历史设计,零向后兼容、不留包袱。** 直接**删除**旧的 CDP 原语浏览器驱动面(`open/new_tab/open_background/goto_url/reload/switch_tab/click_at_xy/type_text/press_key/fill_input/scroll/dispatch_key/js/cdp/capture_screenshot/describe_page/diff_snapshot/wait*/attach_*/iframe_target/...` 这些 page/tab 交互原语),不标 deprecated。heredoc 命名空间改为注入 Playwright `page/context`(+ `state` 占位)+ 新 snapshot。非浏览器驱动、不与 Playwright 重叠的助手(`memory/remember*/site-skills/run_task/list_site_skills/userscripts/http_get/bootstrap_site`)保留。现有 bundled site-skills 若用旧原语,一并改写或删除(零包袱)。

## 分叉已全部收敛

- **Fork 1**:全面对齐 playwriter,删旧 CDP 原语浏览器驱动面,零兼容(见上 Decision)。
- **Fork 2**:注入的 `page` 每 heredoc 自动绑定 daemon `current_target_id`;`page.goto()` 复用、`context.new_page()` 显式新开。
- **Fork 3**:**Playwright 第一方 AI aria snapshot**(`[ref=eN]` + `page.locator("aria-ref=eN")`,playwright-mcp 同款)。⚠️ 需实现时确认 Playwright 1.60 Python sync 暴露的确切 API(`_snapshot_for_ai`/`aria_snapshot` ref 模式);若第一方 ref 模式在 Python 不可用,退化为移植 playwriter 自定义 aria-snapshot。
- **Fork 4**:Playwright Python = **sync API**。

## 非分叉设计判断(已定)

- **暂不注入 `state`**:无 Phase B 时跨 heredoc 不持久,注入空 dict 反成 footgun。句柄持久由 Fork 2 的 page 自动绑定解决;跨步骤记忆靠 agent 对话上下文。`state` 留到 Phase B。
- **`page`/`context` 懒连接**:heredoc 首次访问才 `connect_over_cdp`(纯 memory/site-skill 脚本不连浏览器);heredoc 结束断开。
- **daemon 自动启用 facade**:Phase C 依赖 facade 端点;daemon 需默认拉起 facade(或 skill 层确保其在),不再靠手动 `--facade-port`。

## Requirements (evolving)

- agent 能在 heredoc 里拿到一个连接好的 Playwright `page`(绑定 session 当前 tab)并直接 `goto/click/fill/locator`。
- snapshot 输出可直接喂 `page.locator(...)`,不需 agent 编选择器、不退化到截图。
- 连续操作只用一个 tab(除非显式新开),根治 tab 爆炸。
- skill.md/`--print-skill` 固化 tab 纪律(复用 about:blank、原地导航、snapshot 优先、never close、observe→act→observe)。

## Acceptance Criteria (evolving)

- [ ] heredoc 内 `page.goto(url1)` 后另一 heredoc `page.goto(url2)` 仍是同一 tab(复用,不新开)。
- [ ] snapshot 行可直接 `page.locator(<行里的 locator>)` 点击成功。
- [ ] 复现最初场景:连续访问 N 个 URL 只产生 1 个 tab。
- [ ] extension + rdp 两后端均可用。

## Definition of Done

- 单测 + e2e(复用 phase A harness)覆盖句柄复用、snapshot locator、跨 heredoc 同 tab。
- `--print-skill`/`skill_runtime.md` 更新为 Playwright-based 接口 + tab 纪律。
- lint/typecheck/CI 绿;memory 决策更新。

## Out of Scope

- Phase B:持久 per-session executor / live `state` 跨调用存活(本期句柄靠重连+ledger 重绑,不做常驻 sandbox)。
- 合并 phase A 分支(另行处理)。

## Research References

- [`../05-24-tab-handle-model-for-code-agents/research/playwriter-exposure.md`] — playwriter agent 暴露面(execute/state/snapshot/skill.md)。
- `.trellis/spec/backend/playwright-cdp-facade.md` — facade 契约(phase C 经它连 Playwright)。

## Technical Notes

- 注入点:`repl/_namespace.py:build_globals()`、`repl/inline.py`;facade 连接:`connect_over_cdp(ws://127.0.0.1:<facade_port>/cdp)`;session→tab 绑定:`session_runtime`/`current_target_id`。
- Playwright 已是 dev 依赖;phase C 若要运行时用,需提为正式依赖(uv)。
