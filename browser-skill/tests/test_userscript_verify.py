"""S4 gate: ``userscript push --verify`` one-step verify orchestration.

This is an **orchestration-level** gate by necessity. The real push reaches a
live Chrome extension backend, which is not available offline, so we mock the
push (the ``subprocess.run`` to ``browser-daemon``) and the reload + screenshot
calls and assert the *flow*:

  (a) with ``--verify``  → after a successful push, reload the live tab then
      capture a screenshot, in that order, and surface the screenshot path;
  (b) without ``--verify`` → only push runs; no reload, no screenshot;
  (c) push fails → no reload/screenshot, push returncode surfaced;
  (d) reload fails (no drivable tab) → push still reported as succeeded.

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


def _patch_reload_and_shot(monkeypatch, reload_exc=None):
    """Mock reload() + capture_screenshot() and record the call order. If
    ``reload_exc`` is set, reload() raises it (simulating no drivable tab)."""
    order: list = []

    def fake_reload(*a, **kw):
        order.append(("reload",))
        if reload_exc is not None:
            raise reload_exc
        return {"url": "https://example.test/"}

    def fake_shot(path=None, **kw):
        order.append(("screenshot",))
        return "/tmp/verify-shot.png"

    # Patch the names as cli.py resolves them (imported inside the function
    # from browser_skill.api — patch on that module).
    import browser_skill.api as api
    monkeypatch.setattr(api, "reload", fake_reload, raising=False)
    monkeypatch.setattr(api, "capture_screenshot", fake_shot, raising=False)
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
    # Flow: reload must happen, then a screenshot, in that order.
    kinds = [c[0] for c in order]
    assert "reload" in kinds, "expected a reload"
    assert "screenshot" in kinds, "expected a verification screenshot"
    assert kinds.index("reload") < kinds.index("screenshot"), \
        "reload must happen before the screenshot"
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


def test_with_verify_degrades_gracefully_when_no_drivable_tab(monkeypatch, capsys):
    """A successful push followed by a failed reload (no drivable tab) must NOT
    look like a push failure: rc stays the push's 0, no screenshot is taken, and
    the message makes clear the push succeeded and only --verify was skipped."""
    _patch_push(monkeypatch, _Ok())
    order = _patch_reload_and_shot(monkeypatch, reload_exc=RuntimeError("no tab"))

    rc = cli._cmd_userscript(["push", "f.user.js", "--verify"])

    assert rc == 0, "push succeeded; a verify-only failure must not flip the rc"
    kinds = [c[0] for c in order]
    assert "reload" in kinds and "screenshot" not in kinds, \
        "reload was attempted; screenshot must be skipped on reload failure"
    err = capsys.readouterr().err
    assert "pushed OK" in err and "skipped" in err
