---
site: ycombinator.com
host_patterns: ["news.ycombinator.com", "ycombinator.com"]
aliases: ["hacker news", "hn", "黑客新闻"]
last_updated: 2026-05-18
---

# Hacker News site memory

## 顶层 URL 结构

- 首页 / top: https://news.ycombinator.com/
- newest: https://news.ycombinator.com/newest
- best: https://news.ycombinator.com/best

## 稳定 selectors

- 表格行: `tr.athing` — id 属性是 story id
- 标题链接: `tr.athing span.titleline > a`
- 分数: 紧邻 athing 之后的 `tr` 下的 `span.score`
- 评论数: 同上 `tr` 下面的最后一个 `a`

## Notes

- HN 上很少改 DOM。这里的 selector 在过去 10 年基本没变。
