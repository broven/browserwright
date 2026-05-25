"""``python -m browserwright._executor --session <id>`` entrypoint."""
from __future__ import annotations

import sys

from .process import main

if __name__ == "__main__":
    sys.exit(main())
