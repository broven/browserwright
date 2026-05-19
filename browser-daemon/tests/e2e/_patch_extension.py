"""Copy chrome-extension/ to a tmpdir and rewrite RELAY_URL to the test port.

Used only by the e2e fixtures — never imported by production code.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

# `const RELAY_URL = "ws://127.0.0.1:19989/";`  (background.js)
RELAY_URL_RE = re.compile(r'(const\s+RELAY_URL\s*=\s*")ws://127\.0\.0\.1:\d+(/?")')


def patch_extension_dir(src_dir: Path, *, relay_port: int) -> Path:
    """Copy `src_dir` to a fresh tmpdir and rewrite RELAY_URL in background.js.

    Returns the path to the patched copy. Caller is responsible for cleanup
    (use `tempfile.mkdtemp` + rmtree in the fixture teardown).
    """
    if not src_dir.is_dir():
        raise FileNotFoundError(f"extension source not a directory: {src_dir}")
    dst = Path(tempfile.mkdtemp(prefix="bd-e2e-ext-"))
    # Copy contents into dst (not into a sub-dir) so --load-extension=dst works.
    for child in src_dir.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)

    bg = dst / "background.js"
    text = bg.read_text(encoding="utf-8")
    new_text, n = RELAY_URL_RE.subn(rf'\g<1>ws://127.0.0.1:{relay_port}\g<2>', text)
    if n != 1:
        raise RuntimeError(
            f"expected exactly one RELAY_URL constant in {bg}, found {n}"
        )
    bg.write_text(new_text, encoding="utf-8")
    return dst
