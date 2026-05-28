"""Unit coverage for the Phase C PR1 foundation (no real browser):

  - `Config.resolved_facade_port()` tri-state (auto-enable / disable / override).
  - the `_ipc` facade discovery file round-trip + cleanup.
  - `browserwright-daemon status --json` surfaces the advertised facade ws.
  - the lazy heredoc `page`/`context` proxies don't connect on construction.

The connect+bind path itself (which needs the daemon facade + a browser) is
covered by `tests/daemon/e2e/test_l2_heredoc_playwright_page.py`.
"""
from __future__ import annotations

import json
from datetime import timedelta

from browserwright.daemon import _ipc
from browserwright.daemon.config import DEFAULT_FACADE_PORT, Config


# ---- Config.resolved_facade_port() tri-state -------------------------------


def test_facade_auto_enabled_when_unset():
    # None (unset) → auto-enable on the default port.
    assert Config().resolved_facade_port() == DEFAULT_FACADE_PORT


def test_facade_disabled_when_zero():
    # 0 → explicitly disabled (don't bind).
    assert Config(facade_port=0).resolved_facade_port() is None


def test_facade_explicit_port_override():
    assert Config(facade_port=29991).resolved_facade_port() == 29991


def test_facade_port_load_default_is_none(monkeypatch):
    # `load()` with no flag/env/toml leaves facade_port None, which
    # resolved_facade_port() then auto-enables — the opt-in override still works.
    from browserwright.daemon.config import load
    cfg = load(env={})
    assert cfg.facade_port is None
    assert cfg.resolved_facade_port() == DEFAULT_FACADE_PORT
    cfg_off = load(env={"BD_FACADE_PORT": "0"})
    assert cfg_off.resolved_facade_port() is None
    cfg_ov = load(env={"BD_FACADE_PORT": "29991"})
    assert cfg_ov.resolved_facade_port() == 29991


# ---- _ipc facade discovery file --------------------------------------------


def test_facade_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert _ipc.read_facade_file() == (None, None)  # absent
    _ipc.write_facade_file("ws://127.0.0.1:19990/cdp", 19990)
    assert _ipc.read_facade_file() == ("ws://127.0.0.1:19990/cdp", 19990)
    assert _ipc.facade_path().exists()


def test_facade_file_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.write_facade_file("ws://127.0.0.1:19990/cdp", 19990)
    _ipc.cleanup_endpoint()
    assert not _ipc.facade_path().exists()
    assert _ipc.read_facade_file() == (None, None)


def test_facade_file_bad_json(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.facade_path().write_text("not json")
    assert _ipc.read_facade_file() == (None, None)


def test_facade_ws_url_carries_bound_browserwright_session(tmp_path, monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _ipc.write_facade_file("ws://127.0.0.1:19990/cdp", 19990)
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: "rdp 7")

    assert ph._facade_ws_url() == "ws://127.0.0.1:19990/cdp?session=rdp%207"


def test_facade_ws_url_preserves_existing_query(tmp_path, monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setenv("BD_FACADE_WS", "ws://127.0.0.1:19990/cdp?debug=1")
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: "s-1")

    assert ph._facade_ws_url() == "ws://127.0.0.1:19990/cdp?debug=1&session=s-1"


# ---- status --json surfaces facade -----------------------------------------


def test_status_json_includes_facade(tmp_path, monkeypatch, capsys):
    from browserwright.daemon import cli

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # Pretend a daemon is alive and advertising a facade.
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout=1.0: (4242, "0.6.0"))
    _ipc.write_facade_file("ws://127.0.0.1:19990/cdp", 19990)

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is True
    assert out["facade"] == {"ws": "ws://127.0.0.1:19990/cdp", "port": 19990}


def test_status_json_facade_null_when_dead(tmp_path, monkeypatch, capsys):
    from browserwright.daemon import cli

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout=1.0: (None, None))
    # Even if a stale facade file lingers, a dead daemon reports facade=None.
    _ipc.write_facade_file("ws://127.0.0.1:19990/cdp", 19990)

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is False
    assert out["facade"] is None


# ---- lazy heredoc page/context: no connect on construction -----------------


def test_lazy_proxies_do_not_connect_on_build_globals(monkeypatch):
    """build_globals() injects lazy page/context proxies that must NOT open a
    Playwright connection — a pure memory heredoc opens no browser."""
    import browserwright.repl.playwright_handle as ph
    from browserwright.repl import _namespace

    called = {"connect": False}

    def _boom(*a, **k):
        called["connect"] = True
        raise AssertionError("lazy proxy connected on construction")

    # Any attempt to discover/connect the facade must be lazy.
    monkeypatch.setattr(ph, "_facade_ws_url", _boom)

    g = _namespace.build_globals()
    assert "page" in g and "context" in g
    assert "__bw_playwright_handle__" in g
    # Constructing + having the proxies in scope must not connect.
    assert called["connect"] is False
    # repr is allowed (doesn't resolve the live object).
    assert "lazy" in repr(g["page"])
    assert called["connect"] is False
    # Tear down cleanly (no connection was made → no-op).
    g["__bw_playwright_handle__"].close()


def test_handle_close_is_noop_without_connect():
    from browserwright.repl.playwright_handle import PlaywrightHandle
    h = PlaywrightHandle()
    # close() before any access is a clean no-op (idempotent).
    h.close()
    h.close()


def test_smart_goto_ignores_explicit_wait_until_and_returns_response():
    from browserwright.repl._smart_goto import patch_page_goto

    class FakePage:
        def __init__(self):
            self.goto_calls = []
            self.load_state_calls = []
            self.evaluates = 0
            self.listeners = {}

        def goto(self, url, *, timeout=None, wait_until=None, referer=None):
            self.goto_calls.append({
                "url": url,
                "timeout": timeout,
                "wait_until": wait_until,
                "referer": referer,
            })
            return {"url": url, "status": 200}

        def wait_for_load_state(self, state, *, timeout=None):
            self.load_state_calls.append((state, timeout))

        def evaluate(self, *_args):
            self.evaluates += 1
            return True

        def wait_for_timeout(self, _ms):
            pass

        def on(self, event, handler):
            self.listeners.setdefault(event, []).append(handler)

        def off(self, event, handler):
            self.listeners[event].remove(handler)

    page = FakePage()
    patch_page_goto(page)

    response = page.goto("https://example.test/", timeout=1234,
                         wait_until="load", referer="https://ref.test/")

    assert response == {"url": "https://example.test/", "status": 200}
    assert page.goto_calls == [{
        "url": "https://example.test/",
        "timeout": 1234,
        "wait_until": "commit",
        "referer": "https://ref.test/",
    }]
    assert page.load_state_calls[0][0] == "domcontentloaded"
    assert 1 <= page.load_state_calls[0][1] <= 1234


def test_smart_goto_accepts_timedelta_timeout():
    from browserwright.repl._smart_goto import patch_page_goto

    class FakePage:
        def __init__(self):
            self.goto_calls = []
            self.load_state_calls = []

        def goto(self, url, *, timeout=None, wait_until=None, referer=None):
            self.goto_calls.append({
                "url": url,
                "timeout": timeout,
                "wait_until": wait_until,
                "referer": referer,
            })
            return {"url": url, "status": 200}

        def wait_for_load_state(self, state, *, timeout=None):
            self.load_state_calls.append((state, timeout))

        def evaluate(self, *_args):
            return True

        def wait_for_timeout(self, _ms):
            pass

        def on(self, *_args):
            pass

        def off(self, *_args):
            pass

    page = FakePage()
    patch_page_goto(page)

    page.goto("https://example.test/", timeout=timedelta(seconds=2))

    assert page.goto_calls[0]["timeout"] == 2000
    assert page.load_state_calls[0][0] == "domcontentloaded"
    assert 1 <= page.load_state_calls[0][1] <= 2000


def test_smart_goto_patches_context_new_page_results():
    from browserwright.repl._smart_goto import patch_context_pages

    class FakePage:
        def __init__(self):
            self.goto_calls = []

        def goto(self, url, *, timeout=None, wait_until=None, referer=None):
            self.goto_calls.append(wait_until)
            return url

        def wait_for_load_state(self, *_args, **_kwargs):
            pass

        def evaluate(self, *_args):
            return True

        def wait_for_timeout(self, _ms):
            pass

        def on(self, *_args):
            pass

        def off(self, *_args):
            pass

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]

        def new_page(self):
            page = FakePage()
            self.pages.append(page)
            return page

    context = FakeContext()
    patch_context_pages(context)

    existing = context.pages[0]
    created = context.new_page()
    existing.goto("https://old.test/", wait_until="networkidle")
    created.goto("https://new.test/", wait_until="load")

    assert existing.goto_calls == ["commit"]
    assert created.goto_calls == ["commit"]


def test_smart_goto_translates_commit_failure():
    from browserwright.errors import PageLoadFailed
    from browserwright.repl._smart_goto import patch_page_goto

    class FakePage:
        def goto(self, *_args, **_kwargs):
            raise TimeoutError("timed out")

        def on(self, *_args):
            pass

        def off(self, *_args):
            pass

    page = FakePage()
    patch_page_goto(page)

    try:
        page.goto("https://slow.test/")
    except PageLoadFailed as exc:
        assert exc.url == "https://slow.test/"
        assert exc.reason == "commit"
        assert "http_get" in exc.fix
    else:
        raise AssertionError("expected PageLoadFailed")


# ---- PR2: snapshot() injection, filter, truncation ------------------------


def test_snapshot_injected_and_lazy(monkeypatch):
    """build_globals() injects a callable `snapshot` that OVERRIDES the legacy
    coordinate snapshot, and calling nothing leaves the page unconnected."""
    import browserwright.repl.playwright_handle as ph
    from browserwright.repl import _namespace

    def _boom(*a, **k):
        raise AssertionError("snapshot injection triggered a facade connect")

    monkeypatch.setattr(ph, "_facade_ws_url", _boom)
    g = _namespace.build_globals()
    assert callable(g["snapshot"])
    # The injected snapshot is the PR2 one (bound to the heredoc handle), not
    # the legacy EXPORT.
    assert g["snapshot"].__module__ == "browserwright.repl.snapshot"
    # Merely having it in scope must not connect.
    g["__bw_playwright_handle__"].close()


def test_filter_interactive_keeps_refs_and_ancestors():
    from browserwright.repl.snapshot import _filter_interactive

    snap = (
        "- generic [ref=e1]:\n"
        "  - wrapper-no-ref:\n"
        "    - deeper-no-ref:\n"
        "      - button \"A\" [ref=e2]\n"
        "  - junk-subtree:\n"
        "    - text: nothing\n"
        "  - link \"B\" [ref=e3]"
    )
    out = _filter_interactive(snap)
    # Every ref'd line survives.
    assert "[ref=e1]" in out and "[ref=e2]" in out and "[ref=e3]" in out
    # Ancestors of a ref'd node survive so the tree stays coherent.
    assert "wrapper-no-ref" in out and "deeper-no-ref" in out
    # A subtree with NO ref is dropped entirely.
    assert "junk-subtree" not in out and "nothing" not in out
    # No kept line that carries a ref is ever dropped.
    for ln in snap.splitlines():
        if "[ref=" in ln:
            assert ln in out.splitlines()


def test_truncate_never_splits_a_ref():
    """max_chars must cut on a LINE boundary — a severed `[ref=eN]` token would
    be a corrupt ref the agent could act on."""
    from browserwright.repl.snapshot import make_snapshot

    class _Page:
        def __init__(self, t: str) -> None:
            self._t = t

        def aria_snapshot(self, *, mode: str) -> str:
            return self._t

    class _Handle:
        def __init__(self, t: str) -> None:
            self._t = t

        @property
        def page(self):  # type: ignore[no-untyped-def]
            return _Page(self._t)

    # A long line whose [ref=] sits past a naive byte cut.
    text = "A" * 90 + " [ref=e12345]\nnext"
    out = make_snapshot(_Handle(text))(interactive_only=False, max_chars=98)
    assert out.endswith("… [truncated]")
    body = out.rsplit("\n… [truncated]", 1)[0]
    # No partial ref leaked; every surviving ref is intact.
    assert "[ref=e1\n" not in out
    for ln in body.splitlines():
        if "[ref=" in ln:
            assert ln.rstrip().endswith("]")

    # Multi-line: each kept line is whole.
    many = "\n".join(f"  - button [ref=e{i}]" for i in range(1, 60))
    out2 = make_snapshot(_Handle(many))(interactive_only=False, max_chars=200)
    for ln in out2.rsplit("\n… [truncated]", 1)[0].splitlines():
        assert ln.endswith("]")

    # Under budget → no marker.
    out3 = make_snapshot(_Handle("- a [ref=e1]"))(
        interactive_only=False, max_chars=6000)
    assert "truncated" not in out3
