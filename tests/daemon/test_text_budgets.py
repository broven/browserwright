"""Invariants of the two truncators in `browserwright._text` (#54 / #55).

The whole point of having two is that they resolve the same conflict in
opposite directions, so each one's fallback is tested explicitly here — a soft
truncator silently substituted for the hard one is exactly the regression that
would reopen the runaway-output hole.
"""
from __future__ import annotations

import pytest

from browserwright._text import (
    MAX_TEXT_CHARS,
    MID_LINE_MARKER,
    PRODUCER_BUDGET,
    TRUNC_MARKER,
    truncate_hard,
    truncate_lines,
)


def test_producer_budget_leaves_headroom_under_the_transport_bound():
    assert PRODUCER_BUDGET < MAX_TEXT_CHARS


# ---- hard: the bound is never exceeded ------------------------------------


@pytest.mark.parametrize("budget", [0, 1, 5, 14, 15, 21, 22, 50, 500, 10000])
@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "x" * 5_000_000,  # one runaway line, no newlines at all
        "\n".join(f"line {i}" for i in range(500)),
        "\n".join("y" * 300 for _ in range(200)),
        "a\n\n\nb",
    ],
)
def test_truncate_hard_never_exceeds_its_budget(text, budget):
    assert len(truncate_hard(text, budget)) <= budget


def test_truncate_hard_bounds_the_single_line_runaway():
    """The case a whole-line truncator CANNOT serve: `print('x' * 5_000_000)`
    is one line, so 'keep the first line whole' would return 5,000,014 chars
    for a 10,000 budget."""
    out = truncate_hard("x" * 5_000_000, MAX_TEXT_CHARS)
    assert len(out) <= MAX_TEXT_CHARS
    assert out.endswith(MID_LINE_MARKER)


def test_truncate_hard_prefers_whole_lines_and_says_so():
    text = "\n".join(f'  - button "Item {i}" [ref=e{i}]' for i in range(1, 900))
    out = truncate_hard(text, MAX_TEXT_CHARS)

    assert len(out) <= MAX_TEXT_CHARS
    assert out.endswith(TRUNC_MARKER)
    assert not out.endswith(MID_LINE_MARKER)
    body = out.rsplit("\n" + TRUNC_MARKER, 1)[0]
    for ln in body.splitlines():
        assert ln.rstrip().endswith("]"), f"severed line: {ln!r}"
    # Every kept line is a verbatim prefix line of the input.
    assert text.startswith(body)


def test_truncate_hard_is_a_noop_under_budget():
    assert truncate_hard("hello", 100) == "hello"
    exact = "x" * 100
    assert truncate_hard(exact, 100) == exact


def test_truncate_hard_marks_mid_line_distinctly_from_whole_line():
    """The two markers must stay distinguishable: one says 'you are missing
    whole lines', the other says 'the last line you see is itself incomplete —
    do not trust a token at its end'."""
    assert MID_LINE_MARKER != TRUNC_MARKER
    assert truncate_hard("\n".join(["ab"] * 500), 100).endswith(TRUNC_MARKER)
    assert truncate_hard("z" * 500, 100).endswith(MID_LINE_MARKER)


# ---- soft: line integrity wins, by design ---------------------------------


def test_truncate_lines_keeps_the_first_line_whole_even_over_budget():
    """Producer semantics, deliberately unchanged: a partial `[ref=eN]` the
    agent might act on is worse than overflowing a SOFT cap."""
    line = 'a "very long single node" [ref=e42]' + "!" * 200
    out = truncate_lines(line + "\ntail", 50)

    assert out.startswith(line)  # emitted whole despite the 50-char budget
    assert len(out) > 50
    assert out.endswith(TRUNC_MARKER)


def test_truncate_lines_drops_whole_lines_from_the_tail():
    text = "\n".join(f"node-{i} [ref=e{i}]" for i in range(200))
    out = truncate_lines(text, 200)

    assert len(out) <= 200
    body = out.rsplit("\n" + TRUNC_MARKER, 1)[0]
    assert text.startswith(body)
    for ln in body.splitlines():
        assert ln.endswith("]")


# ---- spill: a bound shortens what is read, it does not destroy -------------


def test_spill_text_round_trips_the_untruncated_payload():
    from pathlib import Path

    from browserwright._text import spill_text

    text = "line\n" * 50_000
    path = spill_text(text, prefix="unit")

    assert path is not None
    assert Path(path).read_text(encoding="utf-8") == text


def test_spill_text_does_not_collide():
    from browserwright._text import spill_text

    a = spill_text("first", prefix="unit-collide")
    b = spill_text("second", prefix="unit-collide")
    assert a != b


def test_spill_text_returns_none_instead_of_raising(monkeypatch):
    """A failed spill must shorten the result, never fail the call."""
    import browserwright._text as text_mod

    def _boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(text_mod.Path, "write_text", _boom)
    assert text_mod.spill_text("x", prefix="unit-fail") is None


def test_spills_self_prune_to_the_keep_limit():
    from pathlib import Path

    from browserwright._text import SPILL_KEEP, spill_text

    paths = [
        spill_text(f"payload {i}", prefix="unit-prune")
        for i in range(SPILL_KEEP + 10)
    ]
    alive = [p for p in paths if Path(p).exists()]

    assert len(alive) == SPILL_KEEP
    # It is the NEWEST that survive, and the newest is always readable.
    assert alive == paths[-SPILL_KEEP:]
    assert Path(paths[-1]).read_text(encoding="utf-8") == (
        f"payload {SPILL_KEEP + 9}"
    )


def test_pruning_never_recycles_a_path():
    """A recycled index would point an already-issued path at different
    content, so an agent holding an older path would silently read the wrong
    payload — worse than the file being gone."""
    from browserwright._text import SPILL_KEEP, spill_text

    paths = [
        spill_text(f"p{i}", prefix="unit-recycle")
        for i in range(SPILL_KEEP * 2 + 5)
    ]
    assert len(set(paths)) == len(paths)


def test_pruning_only_touches_this_process(tmp_path, monkeypatch):
    """A session must never delete another session's or user's files."""
    import os
    from pathlib import Path

    from browserwright._text import SPILL_KEEP, spill_text

    other_pid = os.getpid() + 1
    stranger = Path("/tmp") / f"browserwright-unit-other-{other_pid}-0.txt"
    stranger.write_text("not mine", encoding="utf-8")
    try:
        for i in range(SPILL_KEEP + 5):
            spill_text(f"p{i}", prefix="unit-other")
        assert stranger.exists(), "pruned a file belonging to another process"
        assert stranger.read_text(encoding="utf-8") == "not mine"
    finally:
        stranger.unlink(missing_ok=True)


def test_a_failed_prune_does_not_fail_the_spill(monkeypatch):
    import browserwright._text as text_mod

    def _boom(self, *a, **k):
        raise OSError("cannot unlink")

    monkeypatch.setattr(text_mod.Path, "unlink", _boom)
    for i in range(text_mod.SPILL_KEEP + 3):
        path = text_mod.spill_text(f"p{i}", prefix="unit-prune-fail")
        assert path is not None


def test_the_two_truncators_agree_whenever_lines_fit():
    """Where a whole-line cut is possible at all, hard and soft must produce
    the SAME body — the hard variant only diverges in the fallback."""
    text = "\n".join(f"n{i} [ref=e{i}]" for i in range(300))
    assert truncate_hard(text, 500) == truncate_lines(text, 500)
