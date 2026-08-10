# pi 扩展住在本仓库，作为同 tag 发版的 npm 子包

browserwright 是给 code agent 用的浏览器层，但它自己不认识任何一个 agent。要让 pi
用上它，中间需要一层：把 `web_fetch` / `web_search` 注册成 pi 的工具，再 shell out
到 `browserwright` CLI。

这一层原先游离在外（一个 untracked 的 dotfiles 目录）。问题不是它没人管，而是**它和
CLI 的版本没有任何锁步机制**：它依赖 `browserwright markdown` 的 flag、`session
new` 的 stdout 分流、错误 envelope 的形状和退出码，而这些没有一条写在用户可见的文档
里。CLI 一次无心重构就能静默打断它，且没有任何测试会红。

所以它进本仓库，成为 `pi-extension/`，走和 `chrome-extension/` 同一条路：**代码在仓
库里，不进 Python wheel，由独立 CI job 从同一个 git tag 发布**。

## 版本锁步沿用哨兵模式

`pi-extension/package.json` 的 `version` 常驻 `0.0.0`，和 `pyproject.toml`、
`chrome-extension/manifest.json` 一样，由 `release.yml` 在发版时从 tag 盖章。

`0.0.0` 不是随便挑的：它永远不会被误认成真实发布，而一个「陈旧但看着合理」的数字会
——这个仓库的 `pyproject.toml` 曾经卡在 `0.6.2` 而 tag 已经到了 v0.8.0。

`tests/skill/test_release_versioning.py` 从 `release.yml` 里正则抠出各 job 的 tag 校
验规则并断言它们完全一致。加 npm job 时这条断言从「两个 job 必须一致」扩到「三个 job
必须存在且一致」——否则一次发布可能产出有 wheel 而没有配套 npm 包的版本，而这种错误
只在 tag 推出去之后才暴露，届时只能删 tag 重来。

## 抓取逻辑留在 JS 侧

`web_search` 需要 DOM 提取——实测 `browserwright markdown` 的输出解析不出搜索结果：
`--mode=auto` 会丢掉标题和链接，`--mode=full` 里标题和面包屑粘连、还混进 YouTube 章
节时间戳。而 DOM 提取要管会话生命周期。

那这段逻辑可以放两边：Python 侧加一个 `browserwright search` 一次性子命令（照
`markdown` 的形状，ADR-0006），或者留在 npm 包里自己 `session new` → `-s
--code-stdin` → `session end`。

**选后者，理由是发版节奏**：Google 改版是常态，而选择器住在 Python 侧意味着每次改选
择器都要发一次 PyPI 并等所有机器 `upgrade-global`。住在 npm 包里，`npm publish` 就
够了。

代价是会话生命周期和崩溃恢复要在 JS 里重写一遍——而这正是 `.retired/
browserwright.sh` 那 122 行 shell 干过、又被 0.9.0 的 `markdown` 命令干掉的活。所以
不能凭空重写，那六条**实测**出来的 executor 行为必须逐条继承（见
`providers/browserwright-search.ts` 的文件头）：stdout 在 ~10KB 处静默截断且 exit 0、
`sys.exit()` 会杀死 executor、Chrome 以成功导航返回 `neterror` 页、executor 会中途死
掉、错误是每行一个 JSON 对象、会话不关会在用户窗口里堆标签组。

这也是 `kind: "module"` 这个 provider 类型第一次真正被需要：它在 webfetch 的设计里预
留了很久，注释写着「在有东西真正需要它之前不实现」。搜索就是那个东西——一次 shell 调
用表达不了「建会话 → 导航 → 提取 → 失败重试一次 → 无论如何拆除」。

## 提取必须打在 live DOM 上,不能解析文档响应

搬完之后自然会有人问:既然 organic 结果就在 HTML 里,为什么不直接拿
`response.text()` 解析,省掉一次 `page.evaluate`?2026-08-10 实测过,答案是不行。

先确认 SERP 到底是什么形状:挂上网络监听跑一次,整页 6 个 xhr + 6 个 fetch 全是自动
补全、CSS/JS 资源、广告质量遥测和日志,**没有任何一个携带搜索结果**。所以不存在"监
听 Google 的接口"这条路——organic 结果是服务端渲染的,就在主文档里。

但分层是反直觉的:

| | 初始 HTML | 渲染后 DOM |
|---|---|---|
| 体积 | 500 KB | 1.86 MB |
| organic 结果 | ✅ | ✅ |
| **AI Overview 正文** | ❌ 完全不存在 | ✅ |

AI Overview 是流式后注入的,初始 HTML 里逐字符搜不到。解析文档响应能拿到链接,但会
丢掉整个直接答案——而那恰恰是搜索 API 里最贵的字段。所以提取跑在 live DOM 上。

顺带两个实测结论:一是 browserwright 的 `page.goto` 已经等得足够久(它返回后 0.02s
内 organic 和 AI Overview 都已就位),不需要额外等待;二是同一个 URL,裸 `http_get`
拿到 91KB / 0 个 `<h3>`,真实 Chrome 拿到 429KB / 10 个——Google 按客户端指纹发不同
东西,所以"用浏览器取"这一步省不掉。

## 只保留 browserwright 的 provider

搬迁时砍掉了 jina / cloudflare / curl 三个 rung。这个包的定位是「browserwright 官方
pi 扩展」，不是「一个通用的 web 工具包」。

砍掉不等于封死：链引擎完整保留，用户往 `providers/` 丢一个 JSON 就能把更便宜的匿名
rung 排到浏览器前面，不需要注册。

代价要说清楚：每次 `web_fetch` 都会在用户的日常 Chrome 里开标签页（实测 SSR 文档站
4.3s，对比匿名 reader API 的约 1s），并以用户身份访问目标站。换来的是登录态和完整
JS 渲染，这是任何匿名 rung 都给不了的。

顺带解决一个安全问题：`cloudflare.json` 里硬编码着一个活的 API token 和 account id，
不迁移即不会带进公开仓库。

## 后果

- 仓库从单语言变成多语言。`node` 进了 `mise.toml` 的 `[tools]`——它其实早就是事实依
  赖（三个 daemon 测试 shell out 跑 `background.js` 片段），只是一直没钉版本。
- 发布目标从两个变成三个（PyPI、GitHub Release zip、npm）。三者同 tag，`version
  check` 只覆盖前两者，npm 包的版本一致性由发版测试保证。
- `browserwright` CLI 现在有了一个仓内消费者。它的退出码（0/1/2/3/4/5）和 stderr 三
  态（JSON envelope / 裸 traceback / 纯文本）至今没有写进任何用户可见文档，也没有稳
  定性承诺。**这层耦合已经存在，应当补上契约文档和断言测试**，否则本 ADR 想解决的问
  题只是从「跨仓库静默打断」变成了「跨目录静默打断」。
- pi 扩展的迭代不再受 PyPI 发版节奏约束，但也因此可能领先于 CLI。两者同 tag 发布可以
  保证版本号一致，但保证不了一个装了旧 CLI、新扩展的用户不出问题——扩展应当在报错时
  把 CLI 版本要求说清楚。
