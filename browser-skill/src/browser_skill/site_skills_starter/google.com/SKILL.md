# google.com

Search Google and extract the top organic results.

## Conventions

- Always start a fresh `new_tab(...)` so the user's working tab isn't clobbered.
- The result selector `div.g h3` is stable across the consumer SERP. The mobile
  layout uses `div[data-hveid] h3` — `tasks/search.py` falls back if needed.
- Don't try to login. Google's bot detection is aggressive; skill stays
  unauthenticated and reads public SERP only.

## Tasks

- `search` — query → `list[{title, url, snippet}]`
