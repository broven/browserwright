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

import pytest

from browserwright.daemon import _ipc
from browserwright.daemon.config import (
    DEFAULT_FACADE_HOST,
    DEFAULT_FACADE_PORT,
    Config,
    load,
)


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


# ---- facade_host config precedence -----------------------------------------


def test_facade_host_defaults_to_loopback():
    # Never exposed by accident: the default bind host stays loopback.
    assert Config().facade_host == DEFAULT_FACADE_HOST == "127.0.0.1"
    assert load(env={}).facade_host == "127.0.0.1"


def test_facade_host_precedence_cli_over_env_over_toml(tmp_path):
    # Mirrors facade_port: CLI > env > toml > default.
    cfg_path = tmp_path / "daemon.toml"
    cfg_path.write_text('facade_host = "10.0.0.1"\n')

    # toml only
    cfg_toml = load(cli_config_path=str(cfg_path), env={})
    assert cfg_toml.facade_host == "10.0.0.1"

    # env tops toml
    cfg_env = load(cli_config_path=str(cfg_path), env={"BD_FACADE_HOST": "10.0.0.2"})
    assert cfg_env.facade_host == "10.0.0.2"

    # CLI tops env + toml
    cfg_cli = load(
        cli_config_path=str(cfg_path),
        cli_facade_host="100.72.20.32",
        env={"BD_FACADE_HOST": "10.0.0.2"},
    )
    assert cfg_cli.facade_host == "100.72.20.32"


def test_session_idle_prune_loads_from_toml_and_env(tmp_path):
    from browserwright.daemon.config import load

    cfg_path = tmp_path / "daemon.toml"
    cfg_path.write_text("session_idle_prune = 12.5\n")

    cfg = load(cli_config_path=str(cfg_path), env={})
    assert cfg.session_idle_prune == 12.5

    disabled = load(env={"BD_SESSION_IDLE_PRUNE": "0"})
    assert disabled.session_idle_prune is None

    overridden = load(
        cli_config_path=str(cfg_path),
        env={"BD_SESSION_IDLE_PRUNE": "33"},
    )
    assert overridden.session_idle_prune == 33.0


# ---- facade discovery: answered live by the daemon, never from a file ------
#
# There is no discovery file any more. The daemon answers `/__ping__` with its
# live facade state, because the file version shipped a silent outage: macOS
# reaped `/tmp/browserwright-daemon.facade` after three days, the facade itself
# stayed bound and healthy on :19990, and every client plus `status` plus
# `doctor` reported the facade as simply absent. These tests pin the three
# answers a client can get.


def _pong(**kw):
    """A pong as `ping_status_sync` would return it."""
    return _ipc.PongInfo(pid=kw.pop("pid", 4242),
                         version=kw.pop("version", "0.15.1"), **kw)


def test_pong_carries_bound_facade_roundtrip():
    body = _ipc.make_pong_body(
        4242, _ipc.FacadeInfo.bound("ws://127.0.0.1:19990/cdp", 19990))
    pong = _ipc.parse_pong(body)
    assert pong.pid == 4242
    assert pong.facade.available
    assert (pong.facade.ws, pong.facade.port) == ("ws://127.0.0.1:19990/cdp", 19990)


def test_pong_carries_unavailability_reason():
    body = _ipc.make_pong_body(
        4242, _ipc.FacadeInfo.unavailable("disabled via --facade-port 0"))
    pong = _ipc.parse_pong(body)
    assert pong.facade is not None
    assert not pong.facade.available
    assert pong.facade.error == "disabled via --facade-port 0"


def test_pong_without_facade_key_is_unknown_not_broken():
    """An OLD daemon omits the key entirely. That must stay distinguishable
    from "facade unavailable", because the fix differs: upgrade+restart vs
    read the daemon's reason."""
    body = json.dumps({"pong": True, "pid": 4242, "version": "0.14.0"}).encode()
    assert _ipc.parse_pong(body).facade is None


def test_facade_ws_url_carries_bound_browserwright_session(monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda timeout=1.0: _pong(
            facade=_ipc.FacadeInfo.bound("ws://127.0.0.1:19990/cdp", 19990)))
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: "cdp 7")

    # The daemon parses the query with parse_qs, which decodes both `+` and
    # `%20` as a space — assert the decoded value, not the exact encoding.
    from urllib.parse import parse_qs, urlsplit

    url = ph._facade_ws_url()
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "ws://127.0.0.1:19990/cdp"
    assert parse_qs(parts.query)["session"] == ["cdp 7"]


def test_facade_ws_url_preserves_existing_query(monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda timeout=1.0: _pong(
            facade=_ipc.FacadeInfo.bound("ws://127.0.0.1:19990/cdp?debug=1", 19990)))
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: "s-1")

    assert ph._facade_ws_url() == "ws://127.0.0.1:19990/cdp?debug=1&session=s-1"


def test_facade_ws_url_quotes_the_daemons_own_reason(monkeypatch):
    """The whole point of the rework: the client's error says WHY, in the
    daemon's words, instead of the old "discovery file absent"."""
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda timeout=1.0: _pong(facade=_ipc.FacadeInfo.unavailable(
            "facade failed to bind 100.72.20.32:19990: OSError(49)")))
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: None)

    with pytest.raises(ph.FacadeUnavailable) as ei:
        ph._facade_ws_url()
    assert "failed to bind 100.72.20.32:19990" in str(ei.value)


def test_facade_ws_url_tells_you_to_restart_an_old_daemon(monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setattr(_ipc, "ping_status_sync",
                        lambda timeout=1.0: _pong(version="0.14.0", facade=None))
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: None)

    with pytest.raises(ph.FacadeUnavailable) as ei:
        ph._facade_ws_url()
    msg = str(ei.value)
    assert "0.14.0" in msg
    assert "restart" in msg


def test_facade_ws_url_reports_a_dead_daemon_as_such(monkeypatch):
    import browserwright.repl.playwright_handle as ph

    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout=1.0: _ipc.NO_PONG)
    monkeypatch.setattr(ph, "_current_browserwright_session_id", lambda: None)

    with pytest.raises(ph.FacadeUnavailable) as ei:
        ph._facade_ws_url()
    assert "no daemon is running" in str(ei.value)


# ---- status --json surfaces facade -----------------------------------------


def test_status_json_includes_facade(monkeypatch, capsys):
    from browserwright.daemon import cli

    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda timeout=1.0: _ipc.PongInfo(
            pid=4242, version="0.15.1",
            facade=_ipc.FacadeInfo.bound("ws://127.0.0.1:19990/cdp", 19990)))

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is True
    assert out["facade"] == {"ws": "ws://127.0.0.1:19990/cdp", "port": 19990}
    assert out["facade_error"] is None


def test_status_json_reports_why_the_facade_is_missing(monkeypatch, capsys):
    """A live daemon with no facade must say why — the failure this whole
    change exists to make impossible was `facade: null` with no reason."""
    from browserwright.daemon import cli

    monkeypatch.setattr(
        _ipc, "ping_status_sync",
        lambda timeout=1.0: _ipc.PongInfo(
            pid=4242, version="0.15.1",
            facade=_ipc.FacadeInfo.unavailable("disabled via --facade-port 0")))

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["facade"] is None
    assert out["facade_error"] == "disabled via --facade-port 0"


def test_status_json_facade_null_when_dead(tmp_path, monkeypatch, capsys):
    from browserwright.daemon import cli

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout=1.0: _ipc.NO_PONG)

    class _Args:
        json = True

    rc = cli._cmd_status(_Args(), Config())
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["alive"] is False
    assert out["facade"] is None
    # A dead daemon's facade needs no separate explanation: `alive: false` is it.
    assert out["facade_error"] is None


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


def test_bind_current_page_waits_for_delayed_target_without_creating_page(
    monkeypatch,
):
    import browserwright.repl.playwright_handle as ph
    from browserwright import session_runtime

    class FakePage:
        url = "https://target.test/"

        def goto(self, *_args, **_kwargs):
            pass

    class FakeContext:
        def __init__(self):
            self.pages = []
            self.new_page_calls = 0

        def new_page(self):
            self.new_page_calls += 1
            raise AssertionError("binding must not create a replacement page")

    context = FakeContext()
    target_page = FakePage()
    monkeypatch.setattr(
        session_runtime,
        "resolve_current_target",
        lambda _sess: {
            "targetId": "target-1",
            "url": "https://target.test/",
        },
    )

    def announce(_sess, *, timeout):
        assert timeout > 0
        context.pages.append(target_page)
        return True

    monkeypatch.setattr(ph, "_wait_for_session_announce", announce)

    bound = ph.bind_current_page(context, object())

    assert bound is target_page
    assert context.pages == [target_page]
    assert context.new_page_calls == 0


def test_bind_current_page_timeout_never_creates_replacement_page(monkeypatch):
    import pytest

    import browserwright.repl.playwright_handle as ph
    from browserwright import session_runtime
    from browserwright.errors import PageBindTimeout

    class FakeContext:
        def __init__(self):
            self.pages = []
            self.new_page_calls = 0

        def new_page(self):
            self.new_page_calls += 1
            raise AssertionError("binding must not create a replacement page")

    context = FakeContext()
    monkeypatch.setattr(
        session_runtime,
        "resolve_current_target",
        lambda _sess: {
            "targetId": "target-1",
            "url": "https://target.test/",
        },
    )
    monkeypatch.setattr(ph, "_wait_for_session_announce", lambda *_a, **_k: False)
    monkeypatch.setattr(ph, "_PAGE_BIND_TIMEOUT_S", 0.001)

    with pytest.raises(PageBindTimeout) as raised:
        ph.bind_current_page(context, object())

    assert raised.value.target_id == "target-1"
    assert raised.value.timeout == 0.001
    assert raised.value.retryable is True
    assert "browserwright session reset <id>" in raised.value.fix
    assert context.pages == []
    assert context.new_page_calls == 0


def test_page_for_target_rejects_unrelated_singleton_without_guessing():
    import browserwright.repl.playwright_handle as ph

    class Page:
        url = "about:blank"

    class Context:
        pages = [Page()]

    assert ph.page_for_target(
        Context(),
        object(),
        "target-not-this-page",
        "https://wanted.test/",
    ) is None


def test_page_for_target_rejects_ambiguous_duplicate_urls_without_guessing():
    import browserwright.repl.playwright_handle as ph

    class Page:
        url = "about:blank"

    class Context:
        pages = [Page(), Page()]

    assert ph.page_for_target(
        Context(),
        object(),
        "target-one-of-two",
        "about:blank",
    ) is None


def test_page_for_target_uses_exact_agent_marker_for_duplicate_urls(monkeypatch):
    import browserwright.repl.playwright_handle as ph

    class Page:
        url = "https://same.test/"

        def __init__(self, marker: bool):
            self.marker = marker

    wrong = Page(False)
    exact = Page(True)
    cleared = []
    cdp = object()
    monkeypatch.setattr(
        ph,
        "_install_target_marker",
        lambda _sess, _target: (
            True,
            ("marker-key", "marker-value", cdp, "agent-session"),
        ),
    )
    monkeypatch.setattr(
        ph,
        "_page_has_target_marker",
        lambda page, _key, _value: page.marker,
    )
    monkeypatch.setattr(
        ph,
        "_clear_target_marker",
        lambda *args: cleared.append(args),
    )

    page = ph.page_for_target(
        type("Context", (), {"pages": [wrong, exact]})(),
        object(),
        "target-exact",
        "https://same.test/",
    )

    assert page is exact
    assert cleared == [(cdp, "agent-session", "marker-key")]


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
        url = "about:blank"

        def goto(self, *_args, **_kwargs):
            raise TimeoutError("timed out")

        def evaluate(self, *_args):
            return "loading"

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


def test_smart_goto_swallows_commit_error_when_page_actually_loaded():
    from browserwright.repl._smart_goto import patch_page_goto

    class FakePage:
        url = "https://loaded.test/"

        def __init__(self):
            self.load_state_calls = []
            self.evaluates = []
            self.timeout_calls = []

        def goto(self, *_args, **_kwargs):
            raise TimeoutError("timed out")

        def wait_for_load_state(self, state, *, timeout=None):
            self.load_state_calls.append((state, timeout))

        def evaluate(self, script, *_args):
            self.evaluates.append(script)
            if script == "() => document.readyState":
                return "complete"
            return True

        def wait_for_timeout(self, ms):
            self.timeout_calls.append(ms)

        def on(self, *_args):
            pass

        def off(self, *_args):
            pass

    page = FakePage()
    patch_page_goto(page)

    assert page.goto("https://loaded.test/") is None
    assert page.load_state_calls[0][0] == "domcontentloaded"
    assert "() => document.readyState" in page.evaluates
    assert any(script != "() => document.readyState" for script in page.evaluates)


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
