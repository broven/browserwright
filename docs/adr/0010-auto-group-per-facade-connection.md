# 无 session 的 facade 连接自动获得一个专属 tab group

extension backend 上，**每个 CDP 消费者只拥有一个 tab group** —— 无论它带不带
`?session=`。一个连接只能看到、只能操作自己 tab group 里的标签页；`Target.getTargets`
（页面列表）也只返回自己 group 内的内容。**没有任何例外。**

这条原则是 homelab 的远程消费场景提出的：infra 上的 hermes / windmill 以裸 CDP
客户端身份直连 Mac 上 daemon 的 facade（共享 backend），每个消费者都想在用户真身
Chrome 里拥有一个互不可见的隔离工作区，而不用先在本机建 session、再带外传 id。

## 现状（改动前）

- 带 `?session=<id>` 的 facade 连接：`getTargets` / `attachToTarget` 已经按该
  session 的 tab group 收编（`scoped_target_infos` / `target_belongs_to_session`）。
- **无 session 的裸连接**：保持"历史无收编"行为 —— 能看到并操作真身 Chrome 里
  的全部已附加标签页（`facade_extension.py` 里 `_authorize_target` 对
  `session_id is None` 直接放行；`_replay_all_targets` 走 `list_ghost_targets`
  全量）。这就是被本 ADR 废除的例外。

## 决定

无 session 的 extension facade 连接进入 **auto 模式**：

- 连接建立时生成合成 sid：`auto-<8位hex>`，组标题 `<label>-BWauto-<hex>`
  （ADR-0009 同款 `<name>-BW<sid>` 结构；`-BW` 记号保证永远不命中用户自建组）。
  `label` 来自 `?label=` 查询参数（清洗后最长 40 字符），缺省 `anon`。
- 标题覆盖只存在于该连接自己的 `ExtensionUpstream` 实例（`_title_overrides`），
  **不进持久化 ledger** —— auto 会话是瞬态的，不该出现在 `session list`。
- 所有既有按 session 收编的路径原样生效：`scoped_target_infos`、
  `target_belongs_to_session`、`_record_group_binding`、`_group_required`
  （强制"tab 必须落进组"，违反即报错）。

### 生命周期（断连回收）

1. 连接正常关闭 → bridge `aclose()` → 关闭本组全部标签页、清掉组绑定与标题覆盖
   （`close_auto_group`，best-effort；Chrome 空组自动消失）。
2. 突然断连（网络抖动 / Mac 睡 / Chrome 崩）→ facade ws 心跳（ping/pong 20s）
   检测到死连接后走同一收尾。
3. **TTL 孤儿 reaper**：daemon 每 15 分钟 + 启动后各扫一次，枚举 Chrome 全部组
   （扩展新增 `listGroups` 消息），关闭标题匹配 `-BWauto-<hex>` 但 sid 不在存活
   注册表里的组 —— 覆盖 daemon 崩溃 / 扩展 SW 死亡后残留的孤儿组。

### 不做的

- 不把 auto 会话写进 ledger（瞬态、无 owner、无 CLI 语义）。
- 不暴露 relay（19989）远程化：扩展永远连本机 relay，只有 facade（19990）对外。
- 不跨连接复用组：每次连接一个全新组，重连即换组（连接保持 = 组保持）。

## 后果

- 远程消费者（Playwright `connect_over_cdp` / puppeteer `connect` / hermes
  browser toolset）直连 `ws://<mac>:19990/cdp` 即获得隔离工作区，无需预建
  session；组标题就是消费者标签，用户一眼可辨。
- `?session=` 语义不变：显式路由到 ledger 会话（CLI 建的 session 仍可被远端
  指定）。auto 模式只接管**无** session 的连接。
- cdp backend（隔离 Chrome / 外部浏览器）无此改动 —— 它的浏览器实例本身
  就是隔离单元，不需要 tab group（docs/session-workspaces.md 既有硬不变量）。

## 关键文件

- `src/browserwright/daemon/server/facade_extension.py` —— bridge auto 模式、
  `auto_title`、aclose teardown
- `src/browserwright/daemon/server/facade.py` —— `?label=` 解析、存活注册表、
  reaper（`_auto_reaper_loop` / `_sweep_auto_groups`）
- `src/browserwright/daemon/server/extension_upstream.py` —— `_title_overrides` /
  `bind_group_title` / `_group_title_for` / `close_auto_group`
- `src/browserwright/daemon/server/relay.py` —— `list_groups` / `close_group_tabs`
- `chrome-extension/background.js` —— `listGroups` 消息
