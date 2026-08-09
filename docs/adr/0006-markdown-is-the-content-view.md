# markdown 是 content view，与 snapshot 并列

`snapshot()` 回答的是「这页上**能做什么**」—— 一棵 a11y 树，每个可操作节点带一个
`[ref=eN]`，用来决定动作和验证动作。它从来不打算回答「这页上**写了什么**」。

今天 agent 想读内容，被指向 `page.locator("main").inner_text()`
（`skill_runtime.md`）—— 那会丢掉**每一个链接**和全部结构。而 agent 抓一个页面，很
大一部分动机就是顺着它的链接往下走；文档站更是如此，markdown 比 HTML 清楚得多。

所以 markdown 不是「又一个工具函数」，它是 `snapshot()` 缺的那一半：

| | 回答的问题 | 用途 |
|---|---|---|
| `snapshot()` | 这页能做什么 | action view —— 决定动作、验证动作 |
| `read_markdown()` | 这页写了什么 | content view —— 读内容、跟链接 |

## 这一类东西叫 view

在此之前，这一类只有 `snapshot()` 一个孤例，它的约束只存在于实现里，没人写下来。
两个成员起就不行了，所以 `CONTEXT.md` 引入 **view** 这个词：**逐 heredoc 注入的、
把当前 `page` 渲染成 agent 可读文本的只读函数**。

「只读」是硬约束，也是 `read_markdown()` **不接受 `url` 参数**的原因。让它顺手导航
看起来很方便，代价是：它会把 agent 的工作 tab 挪走，于是 agent 手上**所有
`[ref=eN]` 当场失效**（ref 绑定在该页最近一次 snapshot 上），
`skill_runtime.md` 反复强调的 observe → act → observe 纪律被一个「看起来只是读一下」
的调用打断。要换页，agent 自己调 `page.goto(url)` —— 那条路已经被
`repl/_smart_goto.py` 就地打过补丁，白捡 SPA 安全等待。

## 两个接入面

markdown 有两个消费者，需求恰好相反，所以给两个面而不是硬塞进一个：

1. **一次性 CLI command** —— `browserwright markdown <url>`，自己建一条临时 session、
   用完销毁，`--backend` 默认 `extension`。服务的是「随用随走取一个页面」的外部调用方
   （例如把 browserwright 当作 fetch 回退链最后一级的 agent）。**要完整内容。**
2. **会话内注入的 view** —— `read_markdown()`，借用 agent 已有的 session，只看当前页。
   服务的是 agent 的**内容上下文**。**要精简内容** —— agent 的上下文窗口才是真约束。

## 后果

- **view 进不了 `EXPORTS`。** `EXPORTS` 里是模块级函数，拿不到 `page`；view 由 `repl/`
  下的 `make_*(handle)` 工厂产出，在 `build_globals()` 里注入。这正是 `snapshot()` 今天
  不在 `EXPORTS` 里的原因。
- **因此 `--print-skill` 的自动生成区看不见 view。** `skill_doc.py` 是遍历 `EXPORTS`
  渲染的，所以 `snapshot()` 今天根本没出现在那份清单里。**view 必须由
  `skill_runtime.md` 的散文显式承载** —— 那段指向 `inner_text()` 的旧指引要改写成两个
  view 的分工。
- **CLI 里它叫 command，不叫 verb。** `CONTEXT.md` 的 **verb** 已经指
  `BrowserwrightDaemon.*` 那些 daemon 自己应答的 JSON-RPC 方法。CLI 侧一律是 command
  （代码里的 `_cmd_*`）。
- **`browserwright markdown <url>` 是第一个「驱动浏览器却不收 session 参数」的
  command。** 在此之前 CLI 命令干净地分两类：驱动浏览器的（`-e` / `task` / `userscript`
  / `whoami`）**全部**要 `-s`；不要 session 的（`doctor` / `install` / `memory` / …）
  **全部**不碰浏览器。这个新形状是有意的，不是漏网的。
- **它仍然绕不开 executor。** 「每一条驱动浏览器的路径」都必须在 session 的 executor 里
  跑（见 `CONTEXT.md` 的 *executor*），临时 session 也不例外。所以这个 command 的成本
  包含一次 session 创建 + 一次 executor 启动 + 一次 teardown。teardown 在高频路径上的
  脆弱性见 [#53](https://github.com/broven/browserwright/issues/53)。
- **不新增 session 生命周期。** 那条临时 session 是一条**真** session —— 落 ledger、有
  workspace、按既有规则 teardown。没有第二种 session 语义，`CONTEXT.md` 的 *session*
  词条不动。相应地，`--isolated`（`task_runner.py` / `session.isolated_session()`）指的是
  **进程内一个新的 `Session` 对象**，不新建 workspace，与这里无关 —— 不要复用那个词。

内容边界（哪些东西算「这页写了什么」）由
[ADR-0007](0007-content-never-silently-shrinks.md) 决定。
