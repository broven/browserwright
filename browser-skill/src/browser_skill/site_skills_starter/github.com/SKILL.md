# github.com

Browse public GitHub issues + repos without login.

## Conventions

- Public issue / PR listings render fully without login — no auth wall.
- Use `https://github.com/<owner>/<repo>/issues?q=...` and parse the
  list view; the API would be more polite but Skill is fundamentally
  browser-first.

## Tasks

- `list_issues` — owner/repo (+ optional state filter) → list of issues.
