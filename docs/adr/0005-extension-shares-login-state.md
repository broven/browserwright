# extension backend 的存在就是为了复用用户的登录态

驱动用户的真实 Chrome 不是「启动不了自己的浏览器时的退路」—— 它就是目的本身。
agent 直接继承用户已有的登录，完全跳过认证环节。extension backend 付出的一切代价
（见 [ADR-0001](0001-extension-relay-over-remote-debugging-port.md)）都是为这件事
买的单。

该 backend 上，一个会话的 workspace 是一个 Chrome **tab group**。它的隔离边界是
**tab 成员关系 —— 不是 cookie、不是 localStorage、不是登录态**。所有 extension
会话共用用户那唯一一个 Chrome profile。

## 后果

同一站点上的两个并发 extension 会话，**就是同一个已登录用户**，会互相冲掉对方的
服务端状态。该 backend 上的会话隔离，隔离的是「**注意力**」（哪些 tab 是我的），
不是「**身份**」。

隔离身份会直接摧毁这个 backend 的目的，所以我们不做。真正需要隔离的任务应该走
raw-CDP 家族（`backend != "extension"`）—— 那里一个 `rdp` 会话拥有自己的浏览器和
profile，因而也拥有自己独立的 cookie 与登录态。谁来做这个选择见
[ADR-0004](0004-agent-chooses-backend.md)。

tab group **只属于** extension backend。绝不要为 `rdp` / `env` 创建或模拟它。
