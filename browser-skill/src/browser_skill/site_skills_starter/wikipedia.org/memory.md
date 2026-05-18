---
site: wikipedia
host_patterns: ["wikipedia.org", "en.wikipedia.org"]
aliases: ["wikipedia", "wiki", "维基百科"]
last_updated: 2026-05-18
---

# wikipedia.org site memory

## 顶层 URL 结构

- 英文: https://en.wikipedia.org/wiki/<Title_With_Underscores>
- 中文: https://zh.wikipedia.org/wiki/<Title>
- 检索: https://<lang>.wikipedia.org/w/index.php?search=<query>&fulltext=1

## 稳定 selectors

- 摘要段: `div.mw-parser-output > p:not(.mw-empty-elt)`
- 章节标题: `h2 > span.mw-headline`
- 第一段: `div.mw-parser-output > p:not(.mw-empty-elt):first-of-type`

## Notes
