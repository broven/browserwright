"""L2 -- multiple browserwright sessions through extension backend.

This exercises the real extension-backed path: a real Chrome extension is
connected to the test daemon, then two independent browserwright Session
objects open and operate on separate background tabs at the same time.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from browserwright.cdp import CDPSession

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


def _extension_id_from_path(ext_dir: Path) -> str:
    digest = hashlib.sha256(str(ext_dir.resolve()).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def _extension_worker_target_id(cdp: CDPSession, extension_id: str) -> str:
    targets = cdp.send("Target.getTargets")
    prefix = f"chrome-extension://{extension_id}/"
    for info in targets.get("targetInfos", []):
        if (info.get("type") == "service_worker"
                and info.get("url", "").startswith(prefix)):
            return info["targetId"]
    raise AssertionError(f"extension service worker not found: {targets!r}")


def _chrome_tab_group_titles(
    chrome,
    extension_id: str,
    group_ids: list[int],
) -> dict[int, str]:
    cdp = CDPSession(chrome.ws_url)
    try:
        worker = _extension_worker_target_id(cdp, extension_id)
        session_id = cdp.attach(worker)
        expression = (
            "(async () => {"
            f"const ids = {json.dumps(group_ids)};"
            "const entries = await Promise.all(ids.map(async (id) => {"
            "  const group = await chrome.tabGroups.get(id);"
            "  return [String(id), group.title || ''];"
            "}));"
            "return Object.fromEntries(entries);"
            "})()"
        )
        result = cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
    finally:
        cdp.close()
    if "exceptionDetails" in result:
        raise AssertionError(f"tab group title query failed: {result!r}")
    values = result.get("result", {}).get("value", {})
    return {int(group_id): title for group_id, title in values.items()}


def _chrome_close_tabs(chrome, extension_id: str, tab_ids: list[int]) -> None:
    if not tab_ids:
        return
    cdp = CDPSession(chrome.ws_url)
    try:
        worker = _extension_worker_target_id(cdp, extension_id)
        session_id = cdp.attach(worker)
        expression = (
            "(async () => {"
            f"const ids = {json.dumps(tab_ids)};"
            "await Promise.all(ids.map(async (id) => {"
            "  try { await chrome.tabs.remove(id); } catch (_e) {}"
            "}));"
            "return true;"
            "})()"
        )
        cdp.send(
            "Runtime.evaluate",
            session=session_id,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
    finally:
        cdp.close()


def _bs_home() -> Path:
    return Path(__file__).resolve().parent / "_bs_home" / "extension"


def _seed_sessions(records: dict[str, str]) -> list[tuple[Path, bool]]:
    seeded = []
    now = time.time()
    ledger = _bs_home() / "sessions" / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    existed = ledger.exists()
    data = json.loads(ledger.read_text()) if existed else {
        "next_id": 1,
        "sessions": {},
    }
    for sid, name in records.items():
        data["sessions"][sid] = {
            "id": sid,
            "backend": "extension",
            "workspace": None,
            "owner": "attach",
            "name": name,
            "created_at": now,
            "last_seen": now,
        }
    ledger.write_text(json.dumps(data), encoding="utf-8")
    seeded.append((ledger, existed))
    return seeded


def _cleanup_seeded_sessions(
    seeded: list[tuple[Path, bool]],
    session_ids: set[str],
) -> None:
    for ledger, existed in seeded:
        if not ledger.exists():
            continue
        data = json.loads(ledger.read_text())
        for sid in session_ids:
            data.get("sessions", {}).pop(sid, None)
        if not existed and not data.get("sessions"):
            ledger.unlink(missing_ok=True)
        else:
            ledger.write_text(json.dumps(data), encoding="utf-8")


def test_extension_backend_multiple_sessions_operate_concurrently(ext_ready, e2e_daemon):
    """Two independent skill Sessions can drive extension-backed Chrome at once.

    Regression target: extension backend session/attacher bookkeeping must be
    per daemon client + per target. One session operating one tab must not
    steal, corrupt, or block another session operating a different tab.
    """
    script = r'''
import json
import os
import threading
import traceback
from urllib.parse import quote

# The legacy CDP primitives are deleted (the agent surface is Playwright
# `page`/`context` now). This test exercises the DAEMON's concurrent
# multi-session bookkeeping across several in-process Session objects/threads
# — not the single-process injected `page` — so it drives the daemon directly
# via the internal session_runtime helpers (explicit `sess` per thread).
from browserwright.session import Session, with_session
from browserwright.session_ctx import resolve_session
from browserwright.session_runtime import (
    close_session_tab, eval_js, open_session_tab, wait_for_ready,
)

# browserwright inline requires an explicit ledger session. The harness injects
# BD_SESSION and the two Session objects below intentionally share that
# extension-daemon endpoint while keeping their CDP connections/current_target_id
# state independent.
root_record = resolve_session(os.environ.get("BD_SESSION"))

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
            tab = open_session_tab(
                sess, "data:text/html;charset=utf-8," + quote(html),
            )
            target_id = tab["targetId"]
            wait_for_ready(sess)

            before = eval_js(sess, "document.getElementById('value').textContent")

            # Do not let the first worker finish and close before the second
            # has a live tab + attached daemon session. This makes the test a
            # real concurrent multi-session check rather than two sequential
            # one-session smoke tests.
            both_open.wait(timeout=20)

            eval_js(
                sess,
                "document.body.dataset.session = " + json.dumps(name) + ";"
                "document.getElementById('value').textContent = "
                "document.getElementById('value').textContent + ' / operated';"
            )
            after = eval_js(sess, "document.getElementById('value').textContent")
            marker = eval_js(sess, "document.body.dataset.session")
            title = eval_js(sess, "document.title")

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
                close_session_tab(sess, target_id=target_id)
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


def test_extension_backend_three_sessions_get_named_groups_and_scoped_tabs(
    ext_ready,
    e2e_daemon,
    e2e_chrome,
    patched_ext_dir,
):
    """Three named sessions share one extension Chrome but see only their tabs."""
    sessions = {
        "e2e-three-a": "e2e-alpha-group",
        "e2e-three-b": "e2e-bravo-group",
        "e2e-three-c": "e2e-charlie-group",
    }
    seeded = _seed_sessions(sessions)
    script = r'''
import json
import threading
import traceback
from urllib.parse import quote

# Daemon multi-session coverage via the internal session_runtime helpers (the
# agent surface is Playwright now — see the note in the two-session test).
from browserwright.session import Session, with_session
from browserwright.session_ctx import resolve_session
from browserwright.session_runtime import (
    close_session_tab,
    eval_js,
    open_session_tab,
    session_tabs,
    wait_for_ready,
)

SESSION_IDS = [
    "e2e-three-a",
    "e2e-three-b",
    "e2e-three-c",
]
RECORDS = {
    session_id: resolve_session(session_id)
    for session_id in SESSION_IDS
}

start = threading.Barrier(4)
all_open = threading.Barrier(3)
lock = threading.Lock()
results = {}
errors = {}

def worker(session_id):
    record = RECORDS[session_id]
    sess = Session(record=record)
    target_id = None
    failed = False
    try:
        with with_session(sess):
            start.wait(timeout=15)
            name = record["name"]
            html = (
                "<!doctype html>"
                f"<title>{name}</title>"
                f"<main id='value'>{session_id}</main>"
                "<script>window.e2eReady = true</script>"
            )
            tab = open_session_tab(sess, "data:text/html;charset=utf-8," + quote(html))
            target_id = tab["targetId"]
            wait_for_ready(sess)
            before = eval_js(sess, "document.getElementById('value').textContent")
            visible_before = session_tabs(sess, include_internal=False)

            all_open.wait(timeout=30)

            eval_js(
                sess,
                "document.body.dataset.session = " + json.dumps(session_id) + ";"
                "document.getElementById('value').textContent = "
                "document.getElementById('value').textContent + ' / operated';"
            )
            after = eval_js(sess, "document.getElementById('value').textContent")
            marker = eval_js(sess, "document.body.dataset.session")
            visible_after = session_tabs(sess, include_internal=False)

            with lock:
                results[session_id] = {
                    "name": name,
                    "targetId": target_id,
                    "tabId": tab["tabId"],
                    "groupId": tab["groupId"],
                    "title": eval_js(sess, "document.title"),
                    "before": before,
                    "after": after,
                    "marker": marker,
                    "visibleBefore": visible_before,
                    "visibleAfter": visible_after,
                }
    except BaseException:
        failed = True
        try:
            all_open.abort()
        except BaseException:
            pass
        with lock:
            errors[session_id] = traceback.format_exc()
    finally:
        if failed and target_id:
            try:
                close_session_tab(sess, target_id=target_id)
            except BaseException:
                pass
        sess.close()

threads = [
    threading.Thread(target=worker, args=(session_id,), daemon=True)
    for session_id in SESSION_IDS
]
for thread in threads:
    thread.start()
try:
    start.wait(timeout=15)
except BaseException:
    start.abort()
    all_open.abort()
    raise
for thread in threads:
    thread.join(timeout=90)

for thread in threads:
    if thread.is_alive():
        all_open.abort()
        errors[thread.name] = "thread did not finish within 90s"

print(json.dumps({"results": results, "errors": errors}, sort_keys=True))
if errors:
    raise SystemExit(1)
'''
    extension_id = _extension_id_from_path(patched_ext_dir)
    tab_ids: list[int] = []
    try:
        result = run_skill(
            script=script,
            backend="extension",
            timeout=150,
            runtime_dir=e2e_daemon.runtime_dir,
            extra_env={"BD_SESSION": next(iter(sessions))},
        )
        assert result.returncode == 0, (
            f"skill exited {result.returncode};\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

        payload = _extract_payload(result)
        assert payload["errors"] == {}
        results = payload["results"]
        assert set(results) == set(sessions)

        group_ids = [entry["groupId"] for entry in results.values()]
        assert len(set(group_ids)) == 3
        assert all(group_id >= 0 for group_id in group_ids)

        tab_ids = [entry["tabId"] for entry in results.values()]

        titles = _chrome_tab_group_titles(e2e_chrome, extension_id, group_ids)
        assert titles == {
            entry["groupId"]: sessions[session_id]
            for session_id, entry in results.items()
        }

        for session_id, entry in results.items():
            assert entry["name"] == sessions[session_id]
            assert entry["before"] == session_id
            assert entry["after"] == f"{session_id} / operated"
            assert entry["marker"] == session_id
            assert sessions[session_id] in entry["title"]

            before_targets = {tab["targetId"] for tab in entry["visibleBefore"]}
            after_targets = {tab["targetId"] for tab in entry["visibleAfter"]}
            assert before_targets == {entry["targetId"]}
            assert after_targets == {entry["targetId"]}
    finally:
        _chrome_close_tabs(e2e_chrome, extension_id, tab_ids)
        _cleanup_seeded_sessions(seeded, set(sessions))
