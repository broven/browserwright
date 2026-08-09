"""Wiring contracts for the markdown surfaces (ADR-0006).

These are the "it exists and is reachable" tests. They are cheap and they guard
the two things about a **view** that are easy to get wrong precisely because the
mechanism is unusual: it is injected rather than exported, and therefore invisible
to the generated skill doc unless the prose carries it.
"""
from __future__ import annotations

import browserwright
from browserwright.cli import _cmd_markdown
from browserwright.repl._namespace import build_globals


def test_read_markdown_is_injected_next_to_snapshot():
    g = build_globals()
    assert callable(g["read_markdown"])
    assert callable(g["snapshot"]), "the other view should still be there"


def test_views_are_not_in_exports():
    """A view needs the live `page`; `EXPORTS` holds module-level functions that
    cannot have one. If a view ever appears here, it is bound to the wrong
    thing — see the `view` trap in CONTEXT.md."""
    assert "read_markdown" not in browserwright.EXPORTS
    assert "snapshot" not in browserwright.EXPORTS


def test_unsupported_content_type_is_exported():
    """Errors DO belong in EXPORTS — that is what puts them in the generated
    skill doc so an agent can catch them by name."""
    assert "UnsupportedContentType" in browserwright.EXPORTS
    assert browserwright.UnsupportedContentType.default_fix


def test_skill_doc_prose_carries_the_views():
    """`--print-skill` generates its list from `EXPORTS`, so it can never show a
    view. The prose is the only place they exist for the agent; adding a third
    view without touching this file would ship it invisible.
    """
    from browserwright.skill_doc import render

    doc = render()
    assert "read_markdown()" in doc
    assert "snapshot()" in doc
    # The guidance it replaced must be gone. `inner_text()` may still be
    # *mentioned* — the new prose warns against it — so this pins the removed
    # recommendation, not the identifier.
    assert "For bulk text extraction, use Playwright text APIs" not in doc


def _run(*args) -> int:
    """Call the command and return its exit code without touching a browser —
    every case below must fail during validation, before a session is minted."""
    return _cmd_markdown(list(args))


def test_help_is_available():
    assert _run("--help") == 0


def test_url_is_required():
    assert _run("--mode=full") == 1
    assert _run() == 1


def test_bad_mode_is_rejected_before_a_session_is_created():
    assert _run("https://e.com", "--mode=bogus") == 1


def test_bad_backend_is_rejected():
    """`rdp`/`env` are the retired names people still have in scripts."""
    assert _run("https://e.com", "--backend=rdp") == 1


def test_bad_max_chars_is_rejected():
    assert _run("https://e.com", "--max-chars=lots") == 1


def test_markdown_command_is_registered_in_the_help_banner():
    from browserwright.cli import HELP

    assert "browserwright markdown <url>" in HELP


def test_markdown_command_accepts_no_session():
    """ADR-0006 calls this out as deliberate: it is the only browser-driving
    command that owns its session instead of receiving one.

    Structural, not textual: every other browser-driving handler takes a
    `session_id` keyword, and this one must not grow it — that would quietly
    turn it back into an ordinary `-s` command.
    """
    import inspect

    from browserwright.cli import _cmd_session, _cmd_task, _cmd_userscript

    for handler in (_cmd_task, _cmd_userscript, _cmd_session):
        assert "session_id" in inspect.signature(handler).parameters
    assert "session_id" not in inspect.signature(_cmd_markdown).parameters
