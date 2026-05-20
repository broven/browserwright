"""P3: the cross-process REPL daemon is gone — only in-process heredoc remains."""
import importlib

import pytest


def test_repl_daemon_modules_deleted():
    for mod in ("browser_skill.repl.server",
                "browser_skill.repl.client",
                "browser_skill.repl._proto"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod)


def test_inline_does_not_reference_repl_socket():
    """inline.run must not consult a REPL socket — no is_repl_running/send_exec."""
    from browser_skill.repl import inline
    assert not hasattr(inline, "is_repl_running")
    assert not hasattr(inline, "send_exec")


def test_repl_and_exec_commands_removed(tmp_bs_home, capsys):
    from browser_skill import cli

    for argv in (["repl", "start"], ["exec", "print(1)"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 1  # unknown command


def test_help_no_longer_mentions_repl(capsys):
    from browser_skill import cli
    assert "repl start" not in cli.HELP
    assert "repl stop" not in cli.HELP
    assert "repl status" not in cli.HELP
    assert "exec '<python>'" not in cli.HELP
