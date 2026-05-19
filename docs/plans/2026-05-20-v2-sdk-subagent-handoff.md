# v2 SDK sub-agent E2E — handoff

**TL;DR**: v1 (fixture-style E2E) 已合到 main，291 unit / 11 e2e pass。v2 把 v1
的 driver 层换成 Claude Agent SDK 起的 sub-agent —— 让 agent **只看
`browser-skill/SKILL.md`** 学怎么用 daemon，然后真的去用。同时验证代码 + 验证
文档可读性。

## 上游 context

- **Design**: `docs/plans/2026-05-19-real-extension-e2e-design.md`（§4 "v2 sub-agent path" 是这一步的契约）
- **v1 plan**: `docs/plans/2026-05-19-real-extension-e2e-plan.md`
- **v1 关键 commits**: `ee56b71..df756a4`（含 review-loop 修的 4 个 fix）
- **v1 入口**: `browser-daemon/tests/e2e/`（README 在同目录）

## v1 已经为 v2 留好的钩子

| 钩子 | 位置 | v2 怎么用 |
|---|---|---|
| Session-scoped 真 daemon + 真扩展 fixture | `tests/e2e/conftest.py` (`e2e_daemon`, `patched_ext_dir`, `e2e_chrome`, `ext_ready`) | sub-agent 跑多个 task 共用一套 setup，不重启 |
| `run_skill(script, *, backend, ...)` | `tests/e2e/helpers.py` | sub-agent 调 SDK 用同一组 env 即可（`BD_NAME`, `BD_EXTENSION_PORT`, `BS_DAEMON_*` 已全部设好） |
| Action-level assertions（`page_info`、DOM、screenshot 大小） | `test_l2_user_flows.py` / `test_l3_parity.py` | sub-agent 自由路径完成 task 后，harness 用同一组 assertion 独立验证 |
| 隔离边界（port 29989/29990、`BD_NAME=bd-e2e`、env scrub） | conftest + helpers | 原样复用，不能破坏 |

## v2 要新加的东西

1. **`verdict.json` 协议**
   sub-agent 完成 task 后输出结构化证据，eg:
   ```json
   {"task": "open example.com and screenshot",
    "evidence": {"page_info": {...}, "screenshot_path": "/tmp/..."},
    "self_assessment": "completed"}
   ```
   harness 独立 verify（不信 self_assessment，去 fixture 的 daemon 里查实际状态）。

2. **SDK 调用层**：替换 `run_skill` 的 subprocess 调用，改成 `claude_agent_sdk` 客户端起 sub-agent，pipe 同一组 env。sub-agent 的工具集应限制在 Bash（跑 `browser-skill <<PY ... PY`）+ Read（只读 `SKILL.md`）。**绝对不给它源码访问** —— 它只能通过文档学。

3. **Sub-agent prompt 模板**：给它任务 + skill 路径。不告诉它实现细节、不给它示例代码。

4. **失败 surface**：sub-agent 跑挂时，harness 拿到 sub-agent 的最后 N 条消息 + verdict.json + daemon log，全部 dump 进 `_artifacts/`。

## 建议第一步（prove-it test）

不要一上来写一堆 task。先写**最小可行**的一个：

```python
@pytest.mark.real_chrome
def test_sdk_subagent_can_screenshot(ext_ready, tmp_path):
    """Sub-agent 只看 SKILL.md，完成 'screenshot example.com' 任务。"""
    verdict = run_sdk_subagent(
        task="Open example.com in the browser and save a screenshot to /tmp/v2-shot.png. Output a verdict.json when done.",
        allowed_tools=["Bash", "Read"],
        env=_test_env(),  # 复用 helpers 里的 env 构造
    )
    assert verdict["self_assessment"] == "completed"
    # 独立验证：不信 sub-agent，自己 page_info
    info = run_skill("print(page_info())", backend="extension")
    assert "example.com" in info.stdout
    assert Path("/tmp/v2-shot.png").stat().st_size > 5_000
```

这个跑通了，v2 就有骨架了。后面扩展 task 集是机械工。

## 开放问题（给 v2 owner 决策）

1. **SDK model**：opus（贵但理解 doc 强）vs sonnet（便宜，绝大多数 skill 用法够用）？倾向 sonnet 起步。
2. **Token budget per task**：sub-agent 容易绕弯路。设一个硬 cap（比如 50k input + 10k output）。
3. **Skill 改了怎么办**：每次 SKILL.md 改动应该重跑 v2，看 sub-agent 还能不能 figure out。可以加 marker `slow_real_subagent` 把它从默认 e2e 里剔出去（v1 e2e 90s 够快，sub-agent 一次可能 60s+）。
4. **CI**：v1 design §4 把 CI 推到 v2 也没强求。先本地。

## 不要做的事

- 不要碰 `chrome-extension/background.js` 的 `RELAY_URL` 硬编码 —— v1 的 patcher 已经处理。
- 不要重做 fixture —— 直接 reuse `ext_ready`、`e2e_chrome`、`e2e_daemon`。
- 不要给 sub-agent 源码访问（让它"作弊"读实现就失去测文档的意义）。
- 不要让 sub-agent 启动自己的 daemon / Chrome —— 用 fixture 起好的。
