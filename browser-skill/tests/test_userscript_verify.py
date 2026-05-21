"""S4 gate: ``userscript push --verify`` one-step verify orchestration.

This is an **orchestration-level** gate by necessity. The real push reaches a
live Chrome extension backend, which is not available offline, so we mock the
push (the ``subprocess.run`` to ``browser-daemon``) and the reload + screenshot
calls and assert the *flow*:

  (a) with ``--verify``  → after a successful push, reload the live tab then
      capture a screenshot, in that order, and surface the screenshot path;
  (b) without ``--verify`` → only push runs; no reload, no screenshot.

A real end-to-end verify still needs the live extension backend; nothing here
proves the script actually took effect on a page.

Anti-overfit: the flow is generic — no hardcoded URL, script name, or selector.
The assertions are about call order / which calls happen, never about strings.
"""
from __future__ import annotations

from browser_skill import cli


class _Ok:
    returncode = 0


class _Fail:
    returncode = 1


def _patch_push(monkeypatch, result):
    """Mock the daemon push (subprocess.run) and record its argv."""
    calls: dict = {}

    def fake_run(argv, **kw):
        calls["push_argv"] = argv
        return result

    monkeypatch.setattr(cli.subprocess, "run", fake_run, raising=False)
    return calls


def _patch_reload_and_shot(monkeypatch):
    """Mock cdp() + capture_screenshot() and record the call order."""
    order: list[str] = []

    def fake_cdp(method, *a, **kw):
        order.append(("cdp", method))
        return {}

    def fake_shot(path=None, **kw):
        order.append(("screenshot",))
        return "/tmp/verify-shot.png"

    # Patch the names as cli.py resolves them (imported inside the function
    # from browser_skill.api / .primitives — patch on the module cli imports).
    import browser_skill.api as api
    monkeypatch.setattr(api, "cdp", fake_cdp, raising=False)
    monkeypatch.setattr(api, "capture_screenshot", fake_shot, raising=False)
    # Avoid a brief real sleep in the settle step.
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None, raising=False)
    return order


def test_without_verify_only_push_runs(monkeypatch, capsys):
    calls = _patch_push(monkeypatch, _Ok())
    order = _patch_reload_and_shot(monkeypatch)

    rc = cli._cmd_userscript(["push", "f.user.js"])

    assert rc == 0
    # push happened, with the --verify flag NOT leaking to the daemon.
    assert calls["push_argv"][:2] == ["browser-daemon", "userscript"]
    assert "--verify" not in calls["push_argv"]
    # No reload, no screenshot.
    assert order == []


def test_with_verify_reloads_then_screenshots_after_push(monkeypatch, capsys):
    calls = _patch_push(monkeypatch, _Ok())
    order = _patch_reload_and_shot(monkeypatch)

    rc = cli._cmd_userscript(["push", "f.user.js", "--verify"])

    assert rc == 0
    # The --verify flag is a browser-skill concern; it must not be forwarded.
    assert "--verify" not in calls["push_argv"]
    # Flow: reload (a Page.reload cdp call) must happen, then a screenshot,
    # in that order.
    kinds = [c[0] for c in order]
    assert "cdp" in kinds, "expected a reload via cdp()"
    assert "screenshot" in kinds, "expected a verification screenshot"
    assert kinds.index("cdp") < kinds.index("screenshot"), \
        "reload must happen before the screenshot"
    # The reload uses Page.reload (generic, no URL/script/selector baked in).
    reload_methods = [c[1] for c in order if c[0] == "cdp"]
    assert any("Page.reload" == m for m in reload_methods)
    # The screenshot path is surfaced to the agent on stdout.
    out = capsys.readouterr().out
    assert "/tmp/verify-shot.png" in out


def test_with_verify_skips_reload_when_push_fails(monkeypatch, capsys):
    """If the push fails, don't reload/screenshot a stale state — return the
    push failure so the agent fixes the script first."""
    calls = _patch_push(monkeypatch, _Fail())
    order = _patch_reload_and_shot(monkeypatch)

    rc = cli._cmd_userscript(["push", "f.user.js", "--verify"])

    assert rc == 1
    assert order == [], "no reload/screenshot should run after a failed push"
