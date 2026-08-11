# tab group 的标题就是 session 的绑定

extension backend 上，session 和它那个 Chrome tab group 之间的**唯一**标识是组标题：

```
<name>-BW<sid>          例：fetch-BW12
```

`name` 是 `session new --name` 给的人类标签，`sid` 是 ledger `next_id` 分配的 session id。
查组只按这个标题查。没有第二个锚。

这条推翻 [#29](https://github.com/broven/browserwright/issues/29)，也就是 per-tab
ownership marker 那套机制 —— 它被整个删掉。

## 前提：组名不会变

这是一条**明写出来的假设**，不是没想到的缺口。用户在 Chrome 里手动改掉组名 →
我们找不到那个组 → 按「组已不存在」正常收尾，不告警、不重试、不留 ledger 行。
那个组从此归用户。

接受这条，是因为它换来的是**只有一条查找路径**。此前的三锚并存（marker /
`runtime.group_id` / 遗留的 title+tabid 启发式）每一个都只覆盖一部分失效场景，
彼此的边界又对不齐 —— #29 自己就是被"两个启发式互相打架"逼出来的，再叠一层是同一个
陷阱的第三次。

## 为什么是 title，不是另外两个

三个候选的区别只有一句话：**谁拥有它，它活多久。**

| 候选 | 谁写的 | 什么时候失效 |
|---|---|---|
| `group_id` | **Chrome 分配的**，我们只是拿到回包 | 浏览器重启后回收重发；组一变空 Chrome 即删组 |
| ownership marker | 我们写的，但存在 `chrome.storage.session` | **Chrome 在浏览器重启时清空**（这正是当初选它的理由——不会有陈旧标记） |
| **title** | **我们写的**（建组那一刻 `chrome.tabGroups.update`） | 只在用户手动改名时失效 |

`group_id` 是**借来的句柄**：生命周期不归我们管，重启之后那个数字可能指着别人的组。
marker 归我们，但它的存储介质被设计成随浏览器重启蒸发。

**title 是唯一一个「归我们所有」且「活过浏览器重启」的东西** —— 而浏览器重启恰恰是
孤儿组唯一可能存在的场景（Chrome 不恢复会话时，那些 tab 随浏览器一起没了，问题自己
消失）。锚的存活条件和问题的发生条件正好对上。

## #29 当年为什么否掉 title，现在为什么不成立

#29 一次废掉两个启发式，但它们的失败方向完全不同，被"都失败了"打包处决时这个区别丢了：

- **last-known-tab-id** —— 重启后 id 被回收，可能采纳**陌生人的组**并关掉它。这是**安全**
  失败，不可接受。
- **title** —— 用户改名后恢复和 teardown 永久卡死。这是**可用性**失败。

title 当年被否的两条理由：

- **不唯一** —— `-BW<sid>` 解决了。`sid` 由 ledger 的 `next_id` 单调分配、`remove()`
  不回退，所以标题是**构造性唯一**，不靠随机熵。
- **用户可改** —— 上面「前提」一节明确接受。而且组标题只有用户能在 Chrome UI 里改，
  网页改不了，所以这里不存在攻击者，只存在用户自己的选择。

`-BW` 这个可见记号是有意的：它保证一次 title 匹配**永远不可能命中用户自建的组**，
把 #29 真正在防的那个安全方向结构性堵死，而不是靠概率。

## 后果

**扩展侧**

- `ownedTabs` / `markTabOwned` / `unmarkTabOwned` / `persistOwnedTabs` 及其 SW 重启后的
  reload、`BD_OWNED_KEY`、queryGroup 回包里的 `ownedSessionId` 字段、几个 tab 事件里的
  unmark 钩子 —— 全删。`chrome.storage.session` 在整个扩展里的使用归零。
- `_resolveSessionGroup` 从「只认数字 id」改为「按标题查」。
- **按标题查必须用 `chrome.tabGroups.query({})` 全量取回、在 JS 里精确字符串比对。**
  不要用 `query({title})` —— 该字段的文档语义是 *"Match group titles against a pattern"*，
  模式语法未公开，而 `name` 里出现 `*` / `?` 会悄悄改变匹配语义。

**daemon 侧**

- `_validate_recovered_group_ownership`（marker 分支 + legacy 降级分支）整个删掉，
  `GroupOwnershipUnproven` 随之消失。
- `_resolve_session_group` 的候选链（ledger → explicit → 逐个验证）塌成一次标题查询。
- `_persist_retry_anchors` 整套删掉，连同两处「ledger checkpoint 写失败就中断 teardown」
  —— 浏览器操作不该被本地文件写入绑架。重试不再需要记住"哪些 tab 可能还活着"：
  按标题重查一次活成员即可，而 **live membership 本来就是 source of truth**。
- ledger `runtime` 去掉 `group_id`、`retry_target_ids`、`owned_tab_ids`
  （`owned_tab_ids` 本来就没有任何读取方）。`current_target_id` 保留 —— 它记的是
  "这个 session 现在在哪个 tab 上"，与组所有权无关。
- `--group-id` CLI 参数与 `groupId` RPC 参数（endSession / recoverSession /
  openBackgroundTab）全部去掉：daemon 从 ledger 记录自己算得出标题。
- auto-prune 原本靠 `group_id is None` 判断「从没在 Chrome 里绑过东西」，改用
  「`runtime` 为空」判断，等价。

**顺带修掉的 bug**

- 扩展重连后 teardown 直接 `break`、剩余 tab 全不尝试（`extension_upstream.py` 的
  `ConnectionError` 分支）—— 这个分支消失了。标题不受重连影响，重查一次接着关即可，
  不需要"重新验证所有权"。
- 「组 id 查不到就报 `ok: True, closed: []`，随后 auto-prune 删掉 ledger 行」这个
  静默丢失（[#53](https://github.com/broven/browserwright/issues/53)）—— 判定链变成
  「按标题查 → 找到就关整组 → 找不到就是组确已不存在」，歧义没有了。

**文档**

- `CONTEXT.md` 的 *binding* 词条整段重写（现在通篇讲 marker）。
- `docs/session-workspaces.md` 里引用 #29 所有权锚的段落同步。

## 与 teardown 预算的关系

本 ADR 同期把 workspace teardown 的预算从 8s 改到 **60s**，外层等待相应排为
CLI **70s** / Layer 2 **80s**（必须严格大于内层，否则调用方会在 teardown 中途掐断 ——
那正是 [#32](https://github.com/broven/browserwright/issues/32) 修掉的原始症状）。
文档里三处 "unbounded workspace teardown" 的说法同时改成"上限 60s"。

两件事同期做，是因为按标题查组多出来的那次查询、以及重连后接着关而不是放弃，都需要
原来 7.5s（还要跟 executor reap 分，实际常只剩 ~3.5s）装不下的时间。

同时 `_graceful_shutdown` 必须开始 drain 在飞的 teardown：在飞窗口从 7.5s 变成 60s，
daemon 重启/升级撞上"关了一半"的概率随之上升。drain 要放在 `trigger_close` **之前**，
顺序反了等于没 drain。
