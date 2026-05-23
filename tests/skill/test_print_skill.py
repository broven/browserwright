"""S7 gate: ``browserwright --print-skill`` emits a skill doc generated from
the running code and stamped with the installed package version.

The whole point of this feature is drift-proofing: the primitive surface in the
printed doc is enumerated from ``browserwright.EXPORTS`` at runtime, so it can
never silently fall out of sync with the binary. These tests therefore assert
by *shape* (every callable EXPORT appears; the package version appears) and
never hardcode the primitive list or the surrounding prose — a hardcoded
expectation would itself be the drift the feature exists to prevent.
"""
from __future__ import annotations

import inspect

import browserwright
from browserwright import skill_doc


def _callable_exports() -> list[str]:
    """The subset of EXPORTS that are callable primitives/functions.

    Enumerated from the live namespace, not a literal list — this mirrors what
    the generator does, so the test stays a real drift check rather than a
    snapshot.
    """
    out = []
    for name in browserwright.EXPORTS:
        obj = getattr(browserwright, name, None)
        if callable(obj) and not (inspect.isclass(obj)
                                  and issubclass(obj, BaseException)):
            out.append(name)
    return out


def test_render_lists_every_callable_export():
    """Every callable primitive in EXPORTS must appear in the generated doc.

    Generated from EXPORTS, so adding/removing a primitive in api.py is
    automatically reflected — this is the load-bearing drift assertion.
    """
    doc = skill_doc.render()
    names = _callable_exports()
    assert names, "expected at least one callable export to enumerate"
    missing = [n for n in names if n not in doc]
    assert not missing, f"primitives absent from generated doc: {missing}"


def test_render_stamps_installed_version():
    """The doc carries the installed package version so a reader knows exactly
    which build these instructions describe. Read from the package, not
    hardcoded."""
    doc = skill_doc.render()
    assert browserwright.__version__ in doc


def test_render_includes_signatures():
    """Each primitive is emitted with its signature (drift-proof surface),
    not just a bare name. Check shape via a couple of representative arrow/paren
    markers rather than exact text."""
    doc = skill_doc.render()
    # signatures are rendered as ``name(...)`` — at least the parens survive.
    assert "new_tab(" in doc


def test_cli_print_skill_flag(capsys):
    """``browserwright --print-skill`` wires the generator into dispatch and
    exits 0, printing the same drift-proof content."""
    from browserwright import cli

    try:
        cli.main(["--print-skill"])
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 0
    out = capsys.readouterr().out

    assert rc == 0
    assert browserwright.__version__ in out
    for name in _callable_exports():
        assert name in out, f"{name} missing from --print-skill output"


def test_cli_print_skill_subcommand(capsys):
    """The ``print-skill`` subcommand spelling works too (agent muscle memory
    is forgiving — mirrors the version/--version pairing)."""
    from browserwright import cli

    try:
        cli.main(["print-skill"])
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 0
    out = capsys.readouterr().out

    assert rc == 0
    assert browserwright.__version__ in out
