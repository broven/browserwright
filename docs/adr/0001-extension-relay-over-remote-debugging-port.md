# 驱动用户的 Chrome 走扩展 + relay，不用 `--remote-debugging-port`

给 agent 拿到 CDP 权限，显而易见的做法是用 `--remote-debugging-port` 启动
Chrome。对**用户自己的浏览器**我们刻意不这么做：那个 flag 会让 Chrome 弹出一条
agent 无法关闭的横幅，而且它没法作用在一个用户已经登录着、正在用的浏览器上。
我们的做法是让一个 unpacked Chrome 扩展通过 `chrome.debugger` 附着，转发给本地
relay（`daemon/server/relay.py`），Playwright 再用 `connect_over_cdp` 连上去。
这套架构借自 [playwriter](https://github.com/remorses/playwriter)，见 `AGENTS.md`。

raw-CDP 家族**照常使用** `--remote-debugging-port` —— 那里浏览器是我们自己的，
不涉及用户的 profile。本条只管用户的浏览器。

## 后果

extension backend **没有真实的 CDP target**。relay 得伪造它们
（`make_target_info`，即 ghost target），facade 还得在其上合成 browser 级 CDP。
本仓库里几乎每一处 extension 侧的复杂度 —— ghost target、facade synthesis、
tab group 的所有权证明、relay 那套 app 级协议 —— 都是这一个选择的后代。
推翻它不是换个传输层，是删掉整个 extension backend。
