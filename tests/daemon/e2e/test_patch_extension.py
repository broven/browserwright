"""Unit tests for the extension patcher (no Chrome involved)."""
from __future__ import annotations

from pathlib import Path

import pytest

from daemon.e2e._patch_extension import patch_extension_dir

EXT_SRC = Path(__file__).resolve().parents[3] / "chrome-extension"


def test_patch_extension_rewrites_relay_url(tmp_path):
    out = patch_extension_dir(EXT_SRC, relay_port=29989)
    bg = (out / "background.js").read_text(encoding="utf-8")
    assert 'ws://127.0.0.1:29989/' in bg
    assert 'ws://127.0.0.1:19989/' not in bg
    # manifest.json must be present and untouched
    assert (out / "manifest.json").is_file()


def test_patch_extension_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        patch_extension_dir(tmp_path / "does-not-exist", relay_port=29989)
