# 页面内容宁可多给，绝不静默少给

markdown 这条路上的每一个取舍，失败方向都必须倒向「**给多了**」。

理由是两种失败的**可发现性**天差地别：

- **给多了** —— agent 拿到导航、页脚、侧边栏。它**看得见**这些噪音，下次调用加一个
  参数就能收窄。失败是可见的、可自纠的。
- **静默少给** —— agent 拿到一段结构干净、读起来完全正常的 markdown，只是内容少了
  96%，而且**没有任何信号**说明它是残片。agent 会拿这个残片继续推理，错误从这里开始
  向下游扩散，谁也追不回来。

所以本文档不是「质量偏好」，它是一条**约束**：任何会让内容变少的机制，要么能证明自己
没丢东西，要么必须把「我丢了东西」说出来。

## 由此推出的五条规则

### 1. 页内规范化是必需的一步，不是优化

不能直接把 `page.content()` 的结果丢给转换器 —— 它会**静默丢掉两类东西**（本仓在
Playwright 1.60 + Chrome for Testing 上实测）：

- **open shadow root 完全不被序列化。** `developer.mozilla.org` 一个文档页有 **24 个
  shadow host**；展平之后多恢复出 **17 个链接、6 个表格行、2 个代码块**。
- **相对链接无法在 Python 侧还原。** 没有任何一个 Python 转换器会解析 `<base>`；而
  浏览器的 `a.href` IDL 属性**永远**返回正确的绝对地址（含 `<base>`、`srcdoc`、逐 frame
  各自的 base URI）。绝对链接数 before → after：MDN **15 → 414**，Hacker News
  **34 → 198**，StackOverflow **361 → 758**。

这两件事**只能在页内做**，所以 `page.evaluate` 的规范化是管线里不可省的一环：绝对化
`a[href]` / `img[src]`、展平 open shadow root、剥掉 `script`/`style`/`noscript`/`svg` 之类，
再序列化交给 Python 转换。

注意 `page.evaluate` 跑在 **main world**，页面可以篡改内建对象（实测：页面覆盖
`Element.prototype.innerHTML` 的 getter 后，`page.evaluate("document.body.innerHTML")`
返回被替换的内容）。这是这条路径已知的代价，也是它**不该**扩大用途的理由。反过来，
`add_script_tag` 会被页面 CSP 挡掉而 `page.evaluate` 不会（CDP `Runtime.evaluate` 带
`allowUnsafeEvalBlockedByCSP`），所以注入一律走 `page.evaluate(<源码文本>)`，**不要**用
`add_script_tag`，也**不要**为此打开 `bypass_csp` —— 那会让页面被挡的内联脚本跑起来、
让 Trusted Types 停止生效，等于改变了我们正要抓取的那个 DOM。

### 2. 正文提取默认不做；自动判据只在证明没塌陷时才采用

正文提取（Readability 那一类）在文章型页面上是 10–20 倍的 token 胜利，在应用型页面上是
灾难。实测同一份语料：

| 页面 | 全页 | 提取后 | 结论 |
|---|---|---|---|
| GitHub issue | 2,866 tok / 118 链接 | **92 tok / 0 链接** | 塌陷 |
| StackOverflow | 44,565 tok / 631 链接 | 2,049 tok / 10 链接 | **成功**（代码块与表格全在）|
| Wikipedia | 89k tok | 28.5k tok / 896 链接 | 成功 |

**不能用保留率判。** GitHub 与 StackOverflow 在比例上几乎重合（链接 0% vs 1.6%，文本
3.2% vs 4.6%），任何比例阈值要么放过前者，要么误杀后者 —— 而后者是整份数据里最好的
一次。

**用绝对量判**，因为塌陷的特征是「绝对意义上什么都没剩」：

```
提取结果为空 或 < 500 字符   → 判定塌陷    （对齐 Readability 自己的 charThreshold）
否则                          → 采用提取结果
```

**塌陷时的兜底不是「原始全页」，是「全页减去 chrome」。** 调用这条路的目的是读正文；
塌陷之后甩回一屏导航，等于用一种噪音换掉另一种。所以移除分成两级，区别是**会不会猜**：

| | 机制 | 会不会把正文本身删掉 |
|---|---|---|
| 正文提取（Readability） | 给每块打分，猜哪块是正文 | **会** —— GitHub issue 页 118 链接 → 0 |
| 去 chrome | 按**标签与 class 名**删已知框架（`nav` / `aside` / `footer` / 表单 / sidebar 类） | **不会** —— 只删认识的，其余一律留下 |

于是 `mode` 是三态、而实际发生的路径有三种（`mode_used`）：

| `mode` | 行为 | 可能的 `mode_used` |
|---|---|---|
| `"auto"`（默认）| 先提取；塌陷则退到「去 chrome 的全页」 | `article` / `stripped` |
| `"article"` | 强制提取，塌陷也不回退（调用方问的就是提取结果，换个东西给它等于答非所问，但要在带外说明）| `article` |
| `"full"` | **一字不删的逐字全页** —— 为「我要导航 / 要表单 / 要每一个链接」而存在 | `full` |

去 chrome 有一个有界的代价：sidebar 是按 class 名判定的，所以理论上会误删一个恰好取名
`sidebar-*` 的正文块。失败只波及匹配到的那个元素，不像打分那样波及整页 —— 但按本文档
的规矩，仍然要在带外说明「已移除页面框架」。

`isProbablyReaderable` **不能**用作这个闸门。它的算法只是在 `p, pre, article` 上累加
`sqrt(文本长度 - 140)` 直到超过 20 —— 衡量的是「有没有大段文字」，**完全不看链接**。
GitHub issue 页有大段文字，它会放行，而那正是塌陷最严重的一页。Mozilla 自己把它标为
quick-and-dirty、"likely to produce both false positives and false negatives"；它的设计用途是
决定 Firefox 要不要显示阅读模式按钮，不是守护数据完整性。它对中文/日文站还有已知的系统性
误判（字符数阈值是按空格分词的语言调的，见 mozilla/readability#429）。

### 3. 同源 iframe 收进来，跨源排除并标记

`page.content()` 把 iframe 正文**全部**丢掉。同源 iframe 可以在页内经 `contentDocument`
拿到，跨源只能从 Python 侧走 `page.frames`。

**只收同源。** 这条界线恰好避开本仓已知最脆的机制：跨源 iframe 就是 OOPIF，而
`chrome-extension/background.js` 为它备了一整套 `Target.setAutoAttach` + 恢复子 session
的逻辑（Chromium 会把这类目标暂停到 debugger 主动恢复为止，注释里点名了 OOPIF 节流导致
永久卡死的场景）。同源 iframe 与主文档同进程，用 `contentDocument` 直接读，完全不碰那套
机制。

跨源被排除，所以**必须标记**「有 N 个跨源 frame 未包含」—— 否则就是本文档禁止的那种静默
丢失。方向可加不可减：以后补跨源是扩大内容，发了再撤是破坏性变更。

### 4. 非 HTML 一律报错，不做尽力而为

只处理 `text/html` 与 `application/xhtml+xml`；其余一律报错，错误信息带上实际的
content-type。检测手段两个面都有：CLI 面走 `page.goto()` 拿 `Response` 头（
`repl/_smart_goto.py` 保证调用方照常拿到正常的 Playwright `Response`），会话内的 view 读
`document.contentType`。

PDF 尤其必须**响亮地失败**：在 Chrome 里打开 PDF，内置阅读器会渲染出一个**真实存在的
DOM**（一个 `<embed>` 壳），于是 `page.content()` 成功、转换器返回一小段看起来完全正常的
markdown —— 这正是本文档要禁止的静默空结果。

这也是范围声明：**browserwright 不长文档管线**。PDF、图片、二进制交给调用方路由到别的
处理器，我们给一个清楚的、带 content-type 的错误，让回退链自己往下走。

### 5. 截断必须配全文，且标记走带外

executor 响应的 console 在 10,000 字符处被裸切（`_executor/protocol.py` 的
`MAX_TEXT_CHARS`），而一个文档页的 markdown 轻松三五万字符。所以：**截断后的正文之外，
完整内容写入一个临时文件，把路径交给调用方，由它决定读不读。**

截断本身按**行边界**做，不按字节 —— 理由与 `repl/snapshot.py` 的 `_truncate_lines` 一致：
切断 `[文字](http://…` 会造出一个坏链接，正如切断 `[ref=eN]` 会造出一个能点错元素的残
ref。（那道 10,000 的墙自身的两个既有缺陷见
[#54](https://github.com/broven/browserwright/issues/54) 与
[#55](https://github.com/broven/browserwright/issues/55)。）

「这次走的是哪条路」（提取 / 全页、有无跨源 frame 被排除、是否截断及全文路径）**走带外**
—— stdout 是内容，stderr 与响应元数据是元信息，这个划分在 `repl/inline.py` 对 warnings
与 screenshot 路径的处理里已经存在。正文里不塞注释、不塞 frontmatter（这也是转换器必须
关掉 `extract_metadata` 的原因）：每一个下游都得剥掉它，代价是重复的。

## 后果

- **转换器是可替换的，但它的输出不是。** 选定 `html-to-markdown` 并跟随其版本，意味着
  markdown 方言与转义会随上游变化，而那会改变输出的**每一个字节**。因此
  **golden-file 测试（固定 HTML 输入 → 期望 markdown 输出）是这个决定成立的前提**，不是
  可选的保险 —— 它是让漂移在 CI 里现形、而不是悄悄改变所有 agent 读到的内容的唯一手段。
  同理，转换器调用必须收在**一个函数**后面，换实现时只有一处要改。
- **转换器的默认值本身就在违反本文档，必须显式关掉。** `preprocessing=None` 不是「关」，
  而是「用内置默认」，内置默认是
  `enabled=True, remove_navigation=True, remove_forms=True` —— 实测
  `<nav><a href="/n">nav link</a></nav><p>body</p><form>…</form>` 默认转换出来只剩
  `'body\n'`。也就是说开箱状态下**每一次**转换都在静默删除导航和表单，包括调用方明确要求
  逐字返回的那一次。删减必须由我们经 `mode` 显式决定并说明，绝不能是某个没人选过的默认值。
  因此 `mode="full"` 用 `PreprocessingOptions(enabled=False)`，`auto` 的兜底层用
  `preset="aggressive"`。
- **其余必填配置三项**：`compact_tables=True`（否则宽表膨胀约 2.7 倍）、
  `extract_metadata=False`（否则往输出顶上塞 YAML frontmatter）、`heading_style="atx"`
  （**目前恰好就是上游默认值**，显式写上是因为我们跟随版本，上游改默认时要由 golden
  file 报警而不是悄悄改变每一个标题）。
- **提取路径上的空输出已被规则 2 接住。** 该转换器有一个已知缺陷：游离的
  `<td>`/`<th>` 会让整个子树静默变成空串。它**在浏览器序列化出来的 HTML 上不可能触发**
  （浏览器出的永远是良构的），只在手工拼的 fragment 上触发 —— 也就是 `mode="article"`
  那条路。而那条路上「空 → 回退全页」本来就是规则 2 的第一条，逻辑闭合。
- **closed shadow root 无解，而且连「有多少个」都测不出来。** 页 JS 里 `el.shadowRoot`
  恒为 `null`，这与「这个元素压根没有 shadow root」**不可区分**，所以我们无法像规则 3 对
  跨源 iframe 那样给出计数 —— 那条标记做不到，不要在别处承诺它。已知的唯一出路是 CDP 的
  `DOM.getDocument({pierce: true})`，未验证，不在 v1 范围内。这是本文档承认的一个真实缺
  口：这一类内容会静默缺失，而我们连缺失了都不知道。
- **上述实测数字来自一份 7 页语料，不是基准。** 这个领域**没有**可用的基准可校准：现有
  抽取基准全是静态 HTML 快照，最新的 WCXB 里「需要 JS 的页面」只占 1.1%，而且给的是空的
  参考答案；没有任何基准覆盖登录后的页面。所以规则 2 的两个阈值（空 / 500 字符）是拟合出
  来的，**必须放在一处常数里**，并预期会被调整。

这些规则约束的是内容边界；两个接入面本身与 view 的定义见
[ADR-0006](0006-markdown-is-the-content-view.md)。
