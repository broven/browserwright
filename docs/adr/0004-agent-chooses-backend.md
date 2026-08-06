# backend 由 agent 选，daemon 从不参与选择

一个会话用哪个 backend，是 **agent** 在 `browserwright session new` 时决定的，
依据是 `skill/memory.md`（`skill/SKILL.md` 明确要求它在选 backend **之前**先读）
以及持久化在 `~/.browserwright/global.md` frontmatter 里的
`daemon.preferred_backend` 偏好。daemon 把 `backend` 当作必填参数接收并服从。

这个选择是**任务形状**的，不是机器形状的。「这个任务需要用户已登录的会话」与
「这是个测试，不该碰用户的浏览器」之间的判断，只有 agent 握得住，因为只有 agent
看得见任务。把它做成 daemon 策略，等于把任务语义编码进一个永远看不到任务的进程。

## 后果

**`session_create.py` 和整个 `daemon/` 里没有任何一处读取
`daemon.preferred_backend`。** 它长得像 daemon 配置，但不是 —— 它是给模型消费的
建议性文字，经由 `remember_preference()` 的两步确认流程写入
（`primitives/site.py`）。后来的人几乎必然会假设 daemon 会自动选型；它不会。

这个键名也不是可以随手改的：它已经持久化在用户 `global.md` 的 `daemon:` 段
frontmatter 下，改名等于一次迁移。
