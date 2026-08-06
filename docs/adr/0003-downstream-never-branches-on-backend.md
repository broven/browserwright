# downstream 永不按 backend 分支

所有连**进** daemon 的东西 —— agent CLI、skill 客户端、facade 上的 Playwright
客户端 —— 看到的是同一套接口。backend 的一切分歧都被吸收在 daemon **内部**。
当某个概念确实是 backend 特有的，daemon 返回的是**同形状**下最接近的诚实等价物，
而不是换个形状、编个值、或者直接报错。

这是产品命题，不是代码风格偏好：browserwright 是给 agent 的**统一**浏览器入口。
一旦下游代码开始按 backend 分支，每个面向 agent 的 primitive、每个 site skill 都得
知道自己拿到的是哪种浏览器，「统一入口」就从事实降格成了口号。

## 后果

抽象的成本全部由 daemon 承担，集中在两处 —— 动它们之前先知道：

- **verb**（`daemon/server/verbs.py`）必须在每个 backend 上返回同形状的诚实结果。
  「形状统一」是硬要求，「含义相同」不是。
- **facade**（`daemon/server/facade_extension.py`）只为 `extension` 合成 browser 级
  CDP。对 raw-CDP 家族它是逐字节透传，**这套合成绝不能被复制进那些路径**。

这条规矩是双向的：当某个 backend 真的做不到某件事，诚实的答案属于 daemon 的响应，
不属于下游的一个 `if`。
