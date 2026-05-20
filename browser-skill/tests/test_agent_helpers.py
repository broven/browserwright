"""agent_helpers.py hot-load — the agent-editable primitive layer.

Borrowed from browser-use/browser-harness's ``agent_helpers.py`` idea, but with
a conflict guard: an agent may *extend* the primitive surface, never silently
*redefine* it.
"""
from browser_skill.repl import _namespace


def _write_helpers(home, body: str) -> None:
    (home / "agent_helpers.py").write_text(body)


def test_helper_function_is_injected(tmp_bs_home):
    _write_helpers(tmp_bs_home, "def my_helper(x):\n    return x + 1\n")
    g = _namespace.build_globals()
    assert "my_helper" in g
    assert g["my_helper"](41) == 42


def test_underscore_names_stay_private(tmp_bs_home):
    _write_helpers(tmp_bs_home, "_secret = 1\ndef _hidden():\n    return 1\n")
    g = _namespace.build_globals()
    assert "_secret" not in g
    assert "_hidden" not in g


def test_helper_can_call_core_primitives(tmp_bs_home):
    # The helper closes over a core primitive at call time; loading must happen
    # AFTER core EXPORTS so the name resolves.
    _write_helpers(
        tmp_bs_home,
        "def uses_core():\n    return callable(goto_url)\n",
    )
    g = _namespace.build_globals()
    # goto_url must be visible to the helper's module globals.
    assert g["uses_core"].__globals__.get("goto_url") is not None


def test_core_primitive_cannot_be_shadowed(tmp_bs_home, capsys):
    import browser_skill
    core_goto = browser_skill.goto_url
    _write_helpers(tmp_bs_home, "def goto_url(url):\n    return 'HIJACKED'\n")
    g = _namespace.build_globals()
    # core wins
    assert g["goto_url"] is core_goto
    # and the agent is told why
    err = capsys.readouterr().err
    assert "goto_url" in err and "core" in err.lower()


def test_missing_file_is_a_no_op(tmp_bs_home):
    # no agent_helpers.py written
    g = _namespace.build_globals()
    assert "goto_url" in g  # core surface intact, no crash


def test_broken_helper_file_does_not_crash_namespace(tmp_bs_home, capsys):
    _write_helpers(tmp_bs_home, "def broken(:\n")  # syntax error
    g = _namespace.build_globals()
    assert "goto_url" in g  # core still loads
    assert "agent_helpers" in capsys.readouterr().err
