---
site: github
host_patterns: ["github.com", "www.github.com"]
aliases: ["github", "pull request", "issue tracker", "代码仓库"]
last_updated: 2026-05-18
---

# github.com site memory

## 顶层 URL 结构

- repo home: https://github.com/<owner>/<repo>
- issue list: https://github.com/<owner>/<repo>/issues?state=open
- PR list: https://github.com/<owner>/<repo>/pulls?state=open

## 稳定 selectors

- 列表项: `div[aria-label="Issues"] [data-testid="issue-pr-title-link"]` 或经典 `a.js-navigation-open`
- 标题文字: `[data-testid="issue-pr-title-link"]`
- 状态徽标: `[data-testid="issue-state"]`
- 数字编号: `a[id^=issue_]`

## Known traps

- React-rendered "new issues UI" 在某些仓库上替代了 classic — 选择器要双路。
- 私有仓库会重定向到 login → AuthWall。
- `https://github.com/issues` 在未登录态下也会跳 login。

## Notes
