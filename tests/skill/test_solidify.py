"""Solidify pipeline: propose readiness + scaffold write."""
import time

from browserwright.session import Session


def _hist_entry(code, ok=True):
    return {"code": code, "ok": ok, "stdout": "", "result": None,
            "exception": None, "ts": time.time()}


def test_propose_returns_partial_dict_when_history_empty(tmp_bs_home, fresh_modules):
    # Bug 3 (v0.3.1): empty history must still return a dict with
    # ``ready=False`` and explanatory warnings — not None.
    from browserwright.solidify import propose

    sess = Session()
    out = propose.propose(sess)
    assert out is not None
    assert out["ready"] is False
    assert out["readiness_score"] == 0.0
    assert out["threshold"] == propose.READINESS_THRESHOLD
    assert any("history" in w.lower() for w in out["warnings"])
    # No scaffold seed fields on a not-ready result.
    assert "draft_run_body" not in out
    assert "draft_args_schema" not in out


def test_propose_scores_clean_run(tmp_bs_home, fresh_modules):
    from browserwright.solidify import propose

    sess = Session()
    sess.history = [
        _hist_entry("query = 'iphone'"),
        _hist_entry("new_tab('https://www.google.com/search?q=' + query)"),
        _hist_entry("wait_for_load()"),
        _hist_entry("results = js('return Array.from(document.querySelectorAll(\"a\")).map(a => a.href)')"),
        _hist_entry("print(results)"),
    ]
    out = propose.propose(sess, name_hint="serp_dump")
    assert out is not None
    assert out["ready"] is True
    # Bug 1 (v0.3.1): host_stem now returns eTLD+1 — www.google.com → google.com.
    assert out["site"] == "google.com"
    assert out["suggested_name"] == "serp_dump"
    assert out["name_hint"] == "serp_dump"
    assert "draft_run_body" in out
    # The hardcoded `query = '...'` should have been parameterized.
    assert "query" in out["draft_args_schema"]


def test_propose_warns_on_auth(tmp_bs_home, fresh_modules):
    from browserwright.solidify import propose

    sess = Session()
    sess.history = [
        _hist_entry("goto_url('https://example.com/login')"),
        _hist_entry("fill_input('input[name=password]', 'x')"),
        _hist_entry("press_key('Enter')"),
    ]
    out = propose.propose(sess)
    # Bug 3: always returns dict — auth penalty surfaces in warnings even
    # if score dips below threshold.
    assert out is not None
    assert any("auth" in w.lower() for w in out["warnings"])


def test_scaffold_writes_task_file(tmp_bs_home, fresh_modules):
    from browserwright.solidify import scaffold
    from browserwright.session import Session
    from browserwright.memory.site_mem import site_dir

    sess = Session()
    spec = {
        "site": "example",
        "suggested_name": "demo_task",
        "draft_args_schema": {"q": {"type": "str", "required": True}},
        "draft_run_body": "    return args['q'].upper()\n",
        "host_hint": "example.com",
    }
    result = scaffold.commit(sess, spec)
    assert "path" in result
    target = site_dir("example.com") / "tasks" / "demo_task.py"
    assert target.exists()
    text = target.read_text()
    assert 'ARGS' in text
    assert "args['q']" in text
