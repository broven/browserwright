from browserwright import cli


def test_userscript_delegates_to_daemon(monkeypatch):
    calls = {}

    class R:
        returncode = 0

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run, raising=False)
    rc = cli._cmd_userscript(["push", "f.user.js"])
    assert rc == 0
    assert calls["argv"][:2] == ["browserwright-daemon", "userscript"]
    assert "push" in calls["argv"]


def test_help_mentions_userscript():
    assert "userscript" in cli.HELP
