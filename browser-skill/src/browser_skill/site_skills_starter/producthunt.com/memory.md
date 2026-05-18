---
site: producthunt
host_patterns: ["producthunt.com", "www.producthunt.com"]
aliases: ["product hunt", "ph", "产品猎手", "今日新品"]
last_updated: 2026-05-18
---

# producthunt.com site memory

## 顶层 URL 结构

- 今日榜: https://www.producthunt.com/

## Known traps

- Cookie consent 横幅可能挡住第一屏；如果选择器返回空，先 dismiss 一次再重试。
- ProductHunt 偶尔 A/B 切 React tree，hard-coded selector 会变；当 selector
  miss 时改用 anchor `a[href^="/posts/"]` 兜底。

## 稳定 selectors

- 卡片: `[data-test^="post-item"]` 或 `a[href^="/posts/"]`
- 名称: 卡片内 `a[href^="/posts/"]` 的 innerText
- vote 数: 卡片内 `[data-test="vote-button"]` 的 innerText

## Notes
