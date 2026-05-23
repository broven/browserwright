"""L2 -- multiple browserwright sessions through extension backend.

This exercises the real extension-backed path: a real Chrome extension is
connected to the test daemon, then two independent browserwright Session
objects open and operate on separate background tabs at the same time.
"""
from __future__ import annotations

import json

from .helpers import SkillResult, run_skill


def _extract_payload(result: SkillResult) -> dict:
    line = next(
        (ln for ln in reversed(result.stdout.strip().splitlines()) if ln.startswith("{")),
        None,
    )
    assert line is not None, (
        "skill output did not contain a JSON payload; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return json.loads(line)


def test_extension_backend_multiple_sessions_operate_concurrently(ext_ready, e2e_daemon):
    """Two independent skill Sessions can drive extension-backed Chrome at once.

    Regression target: extension backend session/attacher bookkeeping must be
    per daemon client + per target. One session operating one tab must not
    steal, corrupt, or block another session operating a different tab.
    """
    script = r'''
import json
import threading
import traceback
from urllib.parse import quote

from browserwright import close_tab, js, open, wait_for_load
from browserwright.session import Session, with_session
from browserwright.session_ctx import resolve_session

# browserwright inline requires an explicit ledger session. run_skill() injects
# BD_SESSION and the two Session objects below intentionally share that
# extension-daemon endpoint while keeping their CDP connections/current_target_id
# state independent.
root_record = resolve_session()

start = threading.Barrier(3)
both_open = threading.Barrier(2)
lock = threading.Lock()
results = {}
errors = {}

def worker(name, initial_text):
    sess = Session(record=root_record)
    target_id = None
    try:
        with with_session(sess):
            # Start both sessions at roughly the same time.
            start.wait(timeout=10)

            html = (
                "<!doctype html>"
                f"<title>{name}</title>"
                f"<main id='value'>{initial_text}</main>"
                "<script>window.e2eReady = true</script>"
            )
            tab = open(
                "data:text/html;charset=utf-8," + quote(html),
            )
            target_id = tab["targetId"]
            wait_for_load()

            before = js("document.getElementById('value').textContent")

            # Do not let the first worker finish and close before the second
            # has a live tab + attached daemon session. This makes the test a
            # real concurrent multi-session check rather than two sequential
            # one-session smoke tests.
            both_open.wait(timeout=20)

            js(
                "document.body.dataset.session = " + json.dumps(name) + ";"
                "document.getElementById('value').textContent = "
                "document.getElementById('value').textContent + ' / operated';"
            )
            after = js("document.getElementById('value').textContent")
            marker = js("document.body.dataset.session")
            title = js("document.title")

            with lock:
                results[name] = {
                    "targetId": target_id,
                    "before": before,
                    "after": after,
                    "marker": marker,
                    "title": title,
                }
    except BaseException:
        try:
            both_open.abort()
        except BaseException:
            pass
        with lock:
            errors[name] = traceback.format_exc()
    finally:
        if target_id:
            try:
                with with_session(sess):
                    close_tab(target_id=target_id)
            except BaseException:
                pass
        sess.close()

threads = [
    threading.Thread(target=worker, args=("session-a", "alpha"), daemon=True),
    threading.Thread(target=worker, args=("session-b", "bravo"), daemon=True),
]
for t in threads:
    t.start()
try:
    start.wait(timeout=10)
except BaseException:
    start.abort()
    both_open.abort()
    raise
for t in threads:
    t.join(timeout=60)

for t in threads:
    if t.is_alive():
        both_open.abort()
        errors[f"thread-{t.name}"] = "thread did not finish within 60s"

print(json.dumps({"results": results, "errors": errors}, sort_keys=True))
if errors:
    raise SystemExit(1)
'''
    result = run_skill(script=script, backend="extension", timeout=90,
                       runtime_dir=e2e_daemon.runtime_dir)
    assert result.returncode == 0, (
        f"skill exited {result.returncode};\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    payload = _extract_payload(result)
    assert payload["errors"] == {}
    results = payload["results"]
    assert set(results) == {"session-a", "session-b"}

    a = results["session-a"]
    b = results["session-b"]
    assert a["targetId"] != b["targetId"]
    assert a["before"] == "alpha"
    assert b["before"] == "bravo"
    assert a["after"] == "alpha / operated"
    assert b["after"] == "bravo / operated"
    assert a["marker"] == "session-a"
    assert b["marker"] == "session-b"
    assert "session-a" in a["title"]
    assert "session-b" in b["title"]
