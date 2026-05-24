"""P7.3: SKILL.md teaches the session model + decision-memory flow, and no
longer documents the removed REPL daemon (P3)."""
from pathlib import Path
import re

import pytest

SKILL_MD = Path(__file__).resolve().parents[2] / "skill" / "SKILL.md"


@pytest.mark.skipif(not SKILL_MD.exists(), reason="skill/SKILL.md not present")
def test_skill_md_documents_session_decision_flow():
    text = SKILL_MD.read_text()
    # the three creation modes
    assert "session new --backend=extension" in text
    assert "--backend=rdp --create" in text
    assert "--backend=rdp --attach" in text
    assert "--name=<short-label>" in text
    assert "--backend=extension --name=personal" in text
    assert "--backend=rdp --create --name=smoke" in text
    assert "--backend=rdp --attach=9222 --name=fp-account" in text
    # transparent usage + loud refusal
    assert "BD_SESSION" in text
    assert "BD_PORT=9333 BD_BACKEND=rdp browserwright" not in text
    assert not re.search(r"^browserwright <<'PY'$", text, re.MULTILINE)
    # decision-memory guidance: hit→auto, miss→ask+record
    assert "decision memory" in text.lower()
    assert "session_decisions.record" in text


@pytest.mark.skipif(not SKILL_MD.exists(), reason="skill/SKILL.md not present")
def test_skill_md_no_longer_documents_repl_daemon():
    text = SKILL_MD.read_text()
    assert "repl start" not in text
    assert "repl stop" not in text
