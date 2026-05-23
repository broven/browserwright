---
site: google
host_patterns: ["google.com", "www.google.com"]
aliases: ["谷歌", "google search", "search the web"]
last_updated: 2026-05-18
---

# google.com site memory

## 顶层 URL 结构

- 搜索: https://www.google.com/search?q=<query>&hl=en
- 图片搜索: https://www.google.com/search?tbm=isch&q=<query>

## 稳定 selectors

- 结果块: `div.g`
- 结果标题: `div.g h3` / `div[data-hveid] h3`
- 结果链接: `div.g a[href^=http]`
- 结果摘要: `div.g div[data-content-feature="1"]`

## Known traps

- 偶尔 SERP 被 "before you continue" cookie consent 拦截：先 dismiss `button#L2AGLb` 或 `form[action*="consent"]`。
- 中文 query 在某些区域返回 "did you mean…" 重定向，结果数为 0；要识别并提示 user。

## Notes
