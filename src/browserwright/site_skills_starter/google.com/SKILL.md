# google.com

Search Google and extract the top organic results.

## Conventions

- Navigate the bound `page` in place with `page.goto(...)` — reuse the working
  tab, don't open a new one per query.
- The result selector `div.g h3` is stable across the consumer SERP. The mobile
  layout uses `div[data-hveid] h3` — `tasks/search.py` falls back if needed.
- Don't try to login. Google's bot detection is aggressive; skill stays
  unauthenticated and reads public SERP only.

## Tasks

- `search` — query → `list[{title, url, snippet}]`
