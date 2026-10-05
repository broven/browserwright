# 搜索收敛为 `browserwright search` 命令；pi 扩展只剩一层薄壳、没有 fallback

部分推翻 [ADR-0008](0008-pi-extension-is-a-subpackage.md) 的「抓取逻辑留在 JS 侧」和
「链引擎完整保留」两节。

## 搜索进 CLI

ADR-0008 把 Google 结果页的 DOM 提取留在 npm 包里，理由是发版节奏：选择器改了只发
npm，不用等 PyPI 和每台机器 `upgrade-global`。

这条理由被两件事压过了：

- **第二个消费者出现了。** feedmind（服务端 Node 应用，跑在没有 Python 的容器里）也要
  web_search，只能把同一段提取 JS 和 `/goto?url=` 解码再抄一份。Google 一改版就要改
  两处，而且两处必然漂移。一个 CLI 命令是所有消费者都能调的唯一形状。
- **JS 侧的代价比预期大。** ADR-0008 自己记了：会话生命周期和崩溃恢复要在 JS 里重写，
  并逐条继承六条实测出来的 executor 行为（stdout ~10KB 静默截断、`sys.exit()` 杀
  executor、neterror 页以成功导航返回……）。这些全是 `browserwright markdown`
  （ADR-0006）早就在 Python 侧解决过的问题；放在 CLI 里，`search` 直接复用同一套一次性
  会话（新建 → executor → 结果走磁盘 → `finally` 里拆除）。

于是有了 `browserwright search <query>`：形状照抄 `markdown`，提取在
`browserwright/repl/search.py`。默认输出给 agent 读的文本，`--json` 给程序。被拦截
（captcha、consent 墙）抛 `Captcha`、exit 5；Google 明说「没有匹配」才返回 0 条结果。
「提取必须打在 live DOM 上」那一节（ADR-0008）的结论不变，只是代码换了地方。

发版节奏的代价照单收下：改选择器 = 发一次 PyPI。

## pi 扩展：一个工具一次 CLI 调用，不再有 fallback

旧扩展是一个声明式 provider 链引擎（`providers/*.json` + `config.json` 的顺序 +
`failWhen` 判定 + probe），fetch 走 本机 Chrome → 远端浏览器 → raw，search 走
本机 → 远端。

现在：

- `bw_web_fetch` = `browserwright markdown <url>`，`bw_web_search` = `browserwright
  search <query>`。扩展只负责声明工具、拼 argv、把 CLI 的错误 envelope 转成一句话抛出。
- **浏览器二选一，不兜底**：设了 `BW_REMOTE_CDP` 就只走远端（`--attach`），没设就只走
  本机 Chrome。raw 档删掉。

去掉 fallback 的理由：兜底让失败变得不可见。本机 Chrome 断了，答案悄悄换成没有登录态
的远端结果或者 raw HTML；调用方以为拿到的是同一种东西。选哪个浏览器是配置，不是运行时
的猜测——要远端就设变量，失败就是失败，原因原样告诉模型。

链引擎、provider JSON、probe、`verify.ts` 和它们的单元测试一并删除。扩展剩下的测试只
钉 CLI 契约（argv 形状、错误 envelope → 抛出的句子），用 PATH 上的 stub
`browserwright` 跑；CLI 一侧由 `tests/daemon/e2e/test_search_command_cdp.py` 和
`test_markdown_command_extension.py` 钉。

## 后果

- 想加匿名或更便宜的抓取源，不再有「往 `providers/` 丢个 JSON」的口子，得改代码。
- 装了新扩展、旧 CLI（没有 `search` 命令）的用户会得到 `unknown command: 'search'`
  的明确报错；两者同 tag 发布，一起升级即可。
- feedmind 这类非 pi 消费者可以直接调 `browserwright search --json` /
  `browserwright markdown`，不必再抄提取逻辑。
