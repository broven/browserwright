from __future__ import annotations


def _install_task(tmp_path, monkeypatch, source: str):
    from browserwright import task_runner

    task_file = tmp_path / "task.py"
    task_file.write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        task_runner, "find_task_path", lambda _site, _name: task_file
    )
    return task_runner


def test_executor_surface_is_injected_without_constructing_a_handle(
    tmp_path, monkeypatch
):
    task_runner = _install_task(
        tmp_path,
        monkeypatch,
        """
ARGS = {}
def run(args, ctx=None):
    return {
        "same_page": page is ctx.page,
        "same_context": context is ctx.context,
        "same_snapshot": snapshot is ctx.snapshot,
        "page": page,
        "context": context,
        "snapshot": snapshot(),
    }
""",
    )

    class Page:
        pass

    class Context:
        pass

    page = Page()
    context = Context()
    snapshot_calls = []

    def snapshot():
        snapshot_calls.append(page)
        return "executor snapshot"

    def unexpected_handle():
        raise AssertionError("executor-backed tasks must not construct a handle")

    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.PlaywrightHandle",
        unexpected_handle,
    )

    result = task_runner._run_task_on_surface(
        "site",
        "task",
        browser_surface=task_runner.BrowserSurface(
            page=page,
            context=context,
            snapshot=snapshot,
        ),
    )

    assert result == {
        "same_page": True,
        "same_context": True,
        "same_snapshot": True,
        "page": page,
        "context": context,
        "snapshot": "executor snapshot",
    }
    assert snapshot_calls == [page]


def test_isolated_executor_surface_opens_one_page_in_existing_context(
    tmp_path, monkeypatch
):
    task_runner = _install_task(
        tmp_path,
        monkeypatch,
        """
ARGS = {}
def run(args, ctx=None):
    return {
        "same_page": page is ctx.page,
        "same_context": context is ctx.context,
        "same_snapshot": snapshot is ctx.snapshot,
        "page": page,
        "snapshot": snapshot(interactive_only=False),
    }
""",
    )

    class Page:
        def __init__(self, name):
            self.name = name
            self.close_calls = 0
            self.snapshot_calls = []

        def aria_snapshot(self, **kwargs):
            self.snapshot_calls.append(kwargs)
            return f"- button {self.name} [ref=e1]"

        def close(self):
            self.close_calls += 1

    original_page = Page("original")
    isolated_page = Page("isolated")

    class Context:
        def __init__(self):
            self.new_page_calls = 0

        def new_page(self):
            self.new_page_calls += 1
            return isolated_page

    context = Context()

    def original_snapshot(**_kwargs):
        raise AssertionError("isolated task must rebind snapshot to its new page")

    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.PlaywrightHandle",
        lambda: (_ for _ in ()).throw(
            AssertionError("executor-backed tasks must not construct a handle")
        ),
    )
    session_events = []

    class IsolatedSession:
        def close(self):
            session_events.append("close")

    isolated_session = IsolatedSession()

    class SessionScope:
        def __enter__(self):
            session_events.append("enter")
            return isolated_session

        def __exit__(self, *_args):
            session_events.append("exit")

    monkeypatch.setattr(
        "browserwright.session._borrowed_session",
        lambda: isolated_session,
    )
    parent_session = object()
    monkeypatch.setattr(
        "browserwright.session.current_session",
        lambda: parent_session,
    )
    monkeypatch.setattr(
        "browserwright.session_runtime.session_tabs",
        lambda sess: (
            [{"targetId": "parent-target"}]
            if sess is parent_session
            else []
        ),
    )
    monkeypatch.setattr(
        "browserwright.session.with_session",
        lambda sess: SessionScope(),
    )
    monkeypatch.setattr(
        task_runner,
        "_bind_session_to_page",
        lambda sess, page, **kwargs: session_events.append(
            ("bind", sess, page, kwargs)
        ),
    )

    result = task_runner._run_task_on_surface(
        "site",
        "task",
        isolated=True,
        browser_surface=(original_page, context, original_snapshot),
    )

    assert result == {
        "same_page": True,
        "same_context": True,
        "same_snapshot": True,
        "page": isolated_page,
        "snapshot": "- button isolated [ref=e1]",
    }
    assert context.new_page_calls == 1
    assert isolated_page.snapshot_calls == [{"mode": "ai"}]
    assert original_page.close_calls == 0
    assert isolated_page.close_calls == 0
    assert session_events == [
        (
            "bind",
            isolated_session,
            isolated_page,
            {"exclude_target_ids": {"parent-target"}},
        ),
        "enter",
        "exit",
        "close",
    ]


def test_absent_executor_surface_keeps_lazy_handle_lifecycle(
    tmp_path, monkeypatch
):
    task_runner = _install_task(
        tmp_path,
        monkeypatch,
        """
ARGS = {}
def run(args, ctx=None):
    return {
        "page": ctx.page,
        "context": ctx.context,
        "snapshot": ctx.snapshot(),
    }
""",
    )
    events = []

    class FakeHandle:
        def __init__(self):
            events.append("construct")

        def close(self):
            events.append("close")

    def fake_proxy(handle, attr):
        return (handle, attr)

    def fake_snapshot(handle):
        return lambda: ("snapshot", handle)

    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.PlaywrightHandle", FakeHandle
    )
    monkeypatch.setattr(
        "browserwright.repl.playwright_handle._LazyHandleProxy", fake_proxy
    )
    monkeypatch.setattr(
        "browserwright.repl.snapshot.make_snapshot", fake_snapshot
    )

    result = task_runner.run_task("site", "task")

    handle = result["page"][0]
    assert result == {
        "page": (handle, "page"),
        "context": (handle, "context"),
        "snapshot": ("snapshot", handle),
    }
    assert events == ["construct", "close"]


def test_public_run_task_does_not_reserve_browser_surface_business_arg(
    tmp_path, monkeypatch
):
    task_runner = _install_task(
        tmp_path,
        monkeypatch,
        """
ARGS = {"_browser_surface": {"required": True}}
def run(args, ctx=None):
    return args["_browser_surface"]
""",
    )

    class FakeHandle:
        def close(self):
            pass

    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.PlaywrightHandle",
        FakeHandle,
    )

    assert task_runner.run_task(
        "site",
        "task",
        _browser_surface="business-value",
    ) == "business-value"


def test_bind_isolated_session_to_exact_page_target(monkeypatch):
    from browserwright import session_runtime, task_runner

    page_events = []

    class Page:
        def evaluate(self, expression, arg):
            page_events.append((expression, arg))

    class CDP:
        def attach(self, target_id):
            return f"session:{target_id}"

        def send(self, method, *, session, **_kwargs):
            assert method == "Runtime.evaluate"
            return {
                "result": {
                    "value": session == "session:target-exact",
                },
            }

    class Session:
        cdp = CDP()
        current_target_id = None

    sess = Session()
    monkeypatch.setattr(
        session_runtime,
        "session_tabs",
        lambda _sess: [
            {"targetId": "target-wrong"},
            {"targetId": "target-exact"},
        ],
    )

    task_runner._bind_session_to_page(sess, Page(), timeout=0.01)

    assert sess.current_target_id == "target-exact"
    assert len(page_events) == 2
    assert "Object.defineProperty" in page_events[0][0]
    assert "delete globalThis" in page_events[1][0]


def test_borrowed_session_does_not_close_parent_cdp(monkeypatch):
    from browserwright import session as session_mod

    class CDP:
        def __init__(self):
            self.close_calls = 0
            self._closed = False

        def close(self):
            self.close_calls += 1

    cdp = CDP()
    parent = type(
        "Parent",
        (),
        {
            "daemon": object(),
            "session_record": {"id": "session-1"},
            "cdp": cdp,
        },
    )()
    monkeypatch.setattr(session_mod, "current_session", lambda: parent)

    borrowed = session_mod._borrowed_session()

    assert borrowed.cdp is cdp
    assert borrowed.current_target_id is None
    borrowed.close()
    assert cdp.close_calls == 0
