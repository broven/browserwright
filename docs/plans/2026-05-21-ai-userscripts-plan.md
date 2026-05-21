# AI-Authored Resident Userscripts — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a parallel capability where a Code Agent authors Tampermonkey-style
`.user.js` scripts that the Chrome extension runs autonomously and persistently
on matching sites via `chrome.userScripts`, driven through the existing
daemon↔extension relay.

**Architecture:** Source of truth = local `.user.js` files the agent edits.
Flow on `push`: `browser-skill userscript` (thin shim) → `browser-daemon
userscript` CLI (reads file, parses `==UserScript==` header) → daemon control
verb `BrowserDaemon.userscript.*` in `proxy.py` → `RelayServer._request` →
extension `userscript.*` message → `chrome.storage.local` + `chrome.userScripts`
registration in the `USER_SCRIPT` world. Full-auto activation, `<all_urls>` host
permission, plain page JS only (no `GM_*`). Existing CDP session-driven control
is untouched.

**Tech Stack:** Python 3.12 (daemon + skill, pytest, custom argv parsing,
`websockets`), MV3 extension JS (`chrome.userScripts`, `chrome.storage.local`),
existing Chrome-for-Testing e2e harness on port 29989.

**Design reference:** `docs/plans/2026-05-21-ai-userscripts-design.md`

---

## Conventions & Key Facts (read before starting)

- **Identity** of a script = `@namespace + "/" + @name` (Tampermonkey
  convention). Re-pushing the same identity **updates**, never duplicates. A
  short stable `id` is `sha1(identity)[:12]` (used as the `chrome.userScripts`
  registration id, which must match `^[A-Za-z0-9_]` — so derive id from the hash
  hex, not raw identity).
- **Capability surface v1:** plain page JS in `USER_SCRIPT` world. No `GM_*`.
- **`chrome.userScripts` requires developer mode** to be enabled for the
  extension (already true for unpacked load). The API object is `undefined`
  otherwise — always guard with `if (!chrome.userScripts)`.
- **`chrome.userScripts` API shape** (verify against Chrome docs while
  implementing; this is the expected shape):
  - `register([{ id, matches, excludeMatches, js:[{code}], runAt, world:'USER_SCRIPT', allFrames:false }])`
  - `update([{ id, ... }])`, `unregister({ ids:[...] })`, `getScripts({ ids })`
  - `configureWorld({ messaging:true })` — enables `chrome.runtime.sendMessage`
    from user scripts (used for the injection audit log).
  - `runAt` ∈ `'document_start' | 'document_end' | 'document_idle'`.
- **Existing relay RPC:** `RelayServer._request(ext, body, *, timeout)` at
  `browser-daemon/src/browser_daemon/server/relay.py:429` allocates an id, sends
  `body` to the extension ws, awaits the matching `{type:"response", id, result|error}`.
  Reuse it; do **not** invent a new transport.
- **Extension dispatcher:** `handleDaemonMessage(msg)` switch at
  `browser-daemon/chrome-extension/background.js:200`. Responses go back via
  `safeSend({ type:"response", id, result })` or `{ ..., error:{ code, message }}`.
- **Daemon control dispatch:** `_handle_browserdaemon()` at
  `browser-daemon/src/browser_daemon/server/proxy.py:672`, verbs named
  `BrowserDaemon.*`, results sent with `_send_to_client(client_id, _result_response(req_id, {...}))`.
- **Daemon CLI ws shim pattern:** `_disconnect_via_ws()` at
  `browser-daemon/src/browser_daemon/cli.py:555` shows how to open a transient ws
  to the daemon control socket (`_ipc.sock_path(cfg.name)` on POSIX) and send a
  `BrowserDaemon.*` method.
- **browser-skill CLI dispatch:** `main()` at
  `browser-skill/src/browser_skill/cli.py:470`; subcommand groups like
  `_cmd_session` at `:388`; kv-arg parser `_parse_kv_args` at `:61`.
- **Commit after every task.** Tests are pytest. Run daemon tests with
  `cd browser-daemon && uv run pytest <path> -v`; skill tests with
  `cd browser-skill && uv run pytest <path> -v`.

---

## Phase 0 — Manifest & permissions

### Task 0: Grant `userScripts` + `<all_urls>` in the manifest

**Files:**
- Modify: `browser-daemon/chrome-extension/manifest.json`

**Step 1:** Add `"userScripts"` to the `permissions` array and `"<all_urls>"` to
`host_permissions`. Result:

```json
  "permissions": [
    "debugger",
    "tabs",
    "tabGroups",
    "activeTab",
    "storage",
    "alarms",
    "userScripts"
  ],
  "host_permissions": [
    "ws://127.0.0.1/*",
    "http://127.0.0.1/*",
    "<all_urls>"
  ],
```

Also bump `"version"` to `"0.5.0"`.

**Step 2: Verify** the JSON parses:
Run: `python -c "import json; json.load(open('browser-daemon/chrome-extension/manifest.json'))"`
Expected: no output, exit 0.

**Step 3: Commit**
```bash
git add browser-daemon/chrome-extension/manifest.json
git commit -m "feat(ext): grant userScripts + <all_urls> for resident userscripts"
```

---

## Phase 1 — `==UserScript==` metadata parser (browser-daemon, pure Python, TDD)

### Task 1: Parse the metadata header into a structured record

**Files:**
- Create: `browser-daemon/src/browser_daemon/userscripts.py`
- Test: `browser-daemon/tests/test_userscripts_parse.py`

**Step 1: Write the failing tests**

```python
# browser-daemon/tests/test_userscripts_parse.py
import pytest
from browser_daemon.userscripts import parse_userscript, UserscriptParseError

BASIC = """\
// ==UserScript==
// @name         HN Tidy
// @namespace    bd.userscripts
// @match        https://news.ycombinator.com/*
// @run-at       document-idle
// @version      1.2
// @description  Collapse noise
// ==/UserScript==
(function(){ window.__x = 1; })();
"""

def test_parses_core_fields():
    us = parse_userscript(BASIC)
    assert us.name == "HN Tidy"
    assert us.namespace == "bd.userscripts"
    assert us.matches == ["https://news.ycombinator.com/*"]
    assert us.run_at == "document_idle"
    assert us.version == "1.2"
    assert us.description == "Collapse noise"
    assert us.code.strip().startswith("(function()")

def test_identity_and_id_are_stable():
    us = parse_userscript(BASIC)
    assert us.identity == "bd.userscripts/HN Tidy"
    assert us.id == parse_userscript(BASIC).id  # deterministic
    assert us.id.isalnum() and len(us.id) == 12

def test_multiple_match_include_exclude():
    src = BASIC.replace(
        "// @match        https://news.ycombinator.com/*",
        "// @match        https://a.com/*\n"
        "// @include      https://b.com/*\n"
        "// @exclude      https://a.com/admin/*",
    )
    us = parse_userscript(src)
    assert us.matches == ["https://a.com/*", "https://b.com/*"]
    assert us.exclude_matches == ["https://a.com/admin/*"]

def test_run_at_default_is_document_idle():
    src = BASIC.replace("// @run-at       document-idle\n", "")
    assert parse_userscript(src).run_at == "document_idle"

def test_run_at_normalizes_dashes():
    src = BASIC.replace("document-idle", "document-start")
    assert parse_userscript(src).run_at == "document_start"

def test_namespace_defaults_when_absent():
    src = BASIC.replace("// @namespace    bd.userscripts\n", "")
    us = parse_userscript(src)
    assert us.namespace == "bd.userscripts"  # default namespace

def test_unsupported_directives_warn_not_fail():
    src = BASIC.replace(
        "// @version      1.2",
        "// @version      1.2\n"
        "// @grant        GM_setValue\n"
        "// @require      https://example.com/lib.js",
    )
    us = parse_userscript(src)
    assert any("grant" in w.lower() for w in us.warnings)
    assert any("require" in w.lower() for w in us.warnings)
    assert us.name == "HN Tidy"  # still parsed

def test_missing_header_raises():
    with pytest.raises(UserscriptParseError):
        parse_userscript("just some js without a header")

def test_no_match_raises():
    src = BASIC.replace("// @match        https://news.ycombinator.com/*\n", "")
    with pytest.raises(UserscriptParseError):
        parse_userscript(src)
```

**Step 2: Run to verify they fail**
Run: `cd browser-daemon && uv run pytest tests/test_userscripts_parse.py -v`
Expected: FAIL (module not found).

**Step 3: Implement the parser**

```python
# browser-daemon/src/browser_daemon/userscripts.py
"""Parse Tampermonkey-style ``==UserScript==`` headers into structured records.

v1 capability surface is plain page JS (no GM_* APIs); unsupported metadata
directives are collected as warnings rather than rejected, so existing
userscripts paste in without crashing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

DEFAULT_NAMESPACE = "bd.userscripts"
_RUN_AT = {
    "document-start": "document_start",
    "document-end": "document_end",
    "document-idle": "document_idle",
    "document_start": "document_start",
    "document_end": "document_end",
    "document_idle": "document_idle",
}
_SUPPORTED = {"name", "namespace", "match", "include", "exclude",
              "run-at", "version", "description"}
_HEADER_RE = re.compile(
    r"//\s*==UserScript==\s*\n(.*?)//\s*==/UserScript==", re.DOTALL)
_LINE_RE = re.compile(r"//\s*@(\S+)\s+(.*?)\s*$")


class UserscriptParseError(ValueError):
    """Raised when a userscript has no header or no @match pattern."""


@dataclass
class Userscript:
    name: str
    namespace: str
    matches: list[str]
    exclude_matches: list[str]
    run_at: str
    version: str
    description: str
    code: str
    warnings: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def id(self) -> str:
        return hashlib.sha1(self.identity.encode()).hexdigest()[:12]

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "identity": self.identity,
            "name": self.name,
            "namespace": self.namespace,
            "matches": self.matches,
            "excludeMatches": self.exclude_matches,
            "runAt": self.run_at,
            "version": self.version,
            "description": self.description,
            "code": self.code,
            "warnings": self.warnings,
        }


def parse_userscript(text: str) -> Userscript:
    m = _HEADER_RE.search(text)
    if not m:
        raise UserscriptParseError("missing ==UserScript== metadata block")
    block = m.group(1)
    code = text[m.end():].lstrip("\n")

    name = ""
    namespace = ""
    version = ""
    description = ""
    run_at = "document_idle"
    matches: list[str] = []
    excludes: list[str] = []
    warnings: list[str] = []

    for line in block.splitlines():
        lm = _LINE_RE.match(line.strip())
        if not lm:
            continue
        key, val = lm.group(1).lower(), lm.group(2).strip()
        if key == "name":
            name = val
        elif key == "namespace":
            namespace = val
        elif key in ("match", "include"):
            matches.append(val)
        elif key == "exclude":
            excludes.append(val)
        elif key == "run-at":
            run_at = _RUN_AT.get(val, "document_idle")
        elif key == "version":
            version = val
        elif key == "description":
            description = val
        elif key not in _SUPPORTED:
            warnings.append(f"@{key} not supported in v1 (ignored)")

    if not name:
        raise UserscriptParseError("@name is required")
    if not matches:
        raise UserscriptParseError("at least one @match/@include is required")

    return Userscript(
        name=name,
        namespace=namespace or DEFAULT_NAMESPACE,
        matches=matches,
        exclude_matches=excludes,
        run_at=run_at,
        version=version,
        description=description,
        code=code,
        warnings=warnings,
    )
```

**Step 4: Run to verify pass**
Run: `cd browser-daemon && uv run pytest tests/test_userscripts_parse.py -v`
Expected: all PASS.

**Step 5: Commit**
```bash
git add browser-daemon/src/browser_daemon/userscripts.py browser-daemon/tests/test_userscripts_parse.py
git commit -m "feat(daemon): parse ==UserScript== metadata headers"
```

---

## Phase 2 — Relay + daemon control verbs (browser-daemon, TDD where possible)

### Task 2: Add `userscript_request` to RelayServer

**Files:**
- Modify: `browser-daemon/src/browser_daemon/server/relay.py` (near `send_cdp`, ~line 393)
- Test: `browser-daemon/tests/test_relay_userscript.py`

**Step 1: Write the failing test** — model it on the existing relay tests
(inspect `tests/test_relay.py` for the fake-extension-connection helper and reuse
it). The test should assert that calling `userscript_request(verb, payload)`
sends `{type:"userscript.<verb>", ...payload}` to the extension conn and returns
the `result`.

```python
# browser-daemon/tests/test_relay_userscript.py
import asyncio
import json
import pytest
from browser_daemon.server.relay import RelayServer

class FakeConn:
    def __init__(self): self.sent = []
    async def send(self, data): self.sent.append(json.loads(data))

@pytest.mark.asyncio
async def test_userscript_request_forwards_and_returns_result():
    relay = RelayServer()
    conn = FakeConn()
    ext = relay._register_fake_extension(conn)  # add this test seam (see step 3)

    async def responder():
        await asyncio.sleep(0.01)
        sent = conn.sent[-1]
        relay._resolve_response(sent["id"], {"ok": True, "id": "abc"})
    asyncio.create_task(responder())

    res = await relay.userscript_request("install", {"script": {"id": "abc"}}, timeout=1.0)
    assert res == {"ok": True, "id": "abc"}
    assert conn.sent[-1]["type"] == "userscript.install"
    assert conn.sent[-1]["script"] == {"id": "abc"}
```

> **NOTE for implementer:** `_register_fake_extension` and `_resolve_response`
> are test seams. Inspect how `tests/test_relay.py` already fakes an extension
> connection and resolves `ext.pending[id]` futures; if a helper already exists,
> use it and delete these seams from the test. The real method under test is
> `userscript_request`. Keep `_pick_extension` (below) returning the single
> connected extension — verify the exact attribute holding extension conns
> (e.g. `self._extensions` / `self._ext`) by reading the file; `send_cdp` uses
> `self._extension_for_tab`, so a no-tab "pick any" variant may need adding.

**Step 2: Run → fail.**
Run: `cd browser-daemon && uv run pytest tests/test_relay_userscript.py -v`

**Step 3: Implement** in `relay.py`:

```python
def _pick_extension(self):
    """Return the (single) connected extension, or None. Userscript ops are
    not tab-scoped, unlike send_cdp's _extension_for_tab."""
    # Adapt to the real container of extension connections in this file.
    for ext in self._iter_extensions():   # implement against the real store
        return ext
    return None

async def userscript_request(self, verb: str, payload: dict, *,
                             timeout: float = 5.0) -> dict | None:
    ext = self._pick_extension()
    if ext is None:
        raise RuntimeError("no extension connected")
    return await self._request(ext, {"type": f"userscript.{verb}", **payload},
                               timeout=timeout)
```

**Step 4: Run → pass. Step 5: Commit**
```bash
git add browser-daemon/src/browser_daemon/server/relay.py browser-daemon/tests/test_relay_userscript.py
git commit -m "feat(daemon): RelayServer.userscript_request bridges control verbs to extension"
```

### Task 3: Delegate from ExtensionUpstream

**Files:**
- Modify: `browser-daemon/src/browser_daemon/server/extension_upstream.py` (~line 250)

**Step 1:** Add a thin delegate (no new test; covered by Task 4 + e2e):
```python
async def userscript_request(self, verb: str, payload: dict, **kw):
    return await self._relay.userscript_request(verb, payload, **kw)
```
Verify the relay attribute name (`self._relay` vs `self.relay`) by reading the file.

**Step 2: Commit**
```bash
git add browser-daemon/src/browser_daemon/server/extension_upstream.py
git commit -m "feat(daemon): ExtensionUpstream delegates userscript_request to relay"
```

### Task 4: Add `BrowserDaemon.userscript.*` control verbs in proxy.py

**Files:**
- Modify: `browser-daemon/src/browser_daemon/server/proxy.py` (`_handle_browserdaemon`, ~line 672)
- Test: `browser-daemon/tests/test_proxy_userscript.py`

**Step 1: Write the failing test** — inspect `tests/` for how the Router is
constructed with a fake upstream/client in existing proxy tests, and assert that
a client message `{"method":"BrowserDaemon.userscript.install","params":{...}}`
results in `upstream.userscript_request("install", {...})` being awaited and the
result sent back via `_result_response`.

> **NOTE for implementer:** Reuse the existing proxy-test harness (fake client +
> fake upstream). If none exists, a minimal `FakeUpstream` with an async
> `userscript_request` recording calls is sufficient.

**Step 2: Run → fail.**

**Step 3: Implement** — add to `_handle_browserdaemon()` before the fallthrough:
```python
if method.startswith("BrowserDaemon.userscript."):
    verb = method.split(".", 2)[2]            # install|push|list|remove|logs|toggle
    if self._ensure_upstream is not None:
        await self._ensure_upstream()         # lazy-open extension upstream
    try:
        result = await self.upstream.userscript_request(verb, params)
    except Exception as e:                      # noqa: BLE001 - surface to client
        await self._send_to_client(
            client.client_id,
            _error_response(req_id, -32000, f"userscript {verb} failed: {e}"))
        return
    await self._send_to_client(
        client.client_id, _result_response(req_id, result or {}))
    return
```
Verify the exact names: `self._ensure_upstream`, `self.upstream`, `_error_response`,
`_result_response`, `_send_to_client`, and how `params`/`req_id`/`client` are
obtained in this method (read the surrounding code at line 672+).

**Step 4: Run → pass. Step 5: Commit**
```bash
git add browser-daemon/src/browser_daemon/server/proxy.py browser-daemon/tests/test_proxy_userscript.py
git commit -m "feat(daemon): BrowserDaemon.userscript.* control verbs"
```

---

## Phase 3 — Daemon CLI `browser-daemon userscript ...`

### Task 5: Implement the daemon CLI subcommand

**Files:**
- Modify: `browser-daemon/src/browser_daemon/cli.py` (add subcommand + ws shim near `_disconnect_via_ws` ~line 555 and the arg dispatch)
- Test: `browser-daemon/tests/test_cli_userscript.py`

**Subcommands:**
- `push <file>` — read file → `parse_userscript` → send `BrowserDaemon.userscript.install` with `{"script": us.to_payload()}`. Print `{id, identity, warnings}` as JSON. (`install` is an alias of `push` that also accepts `-`/stdin.)
- `list [--site=<url>]` — send `BrowserDaemon.userscript.list` with `{"site": url}`; print rows.
- `remove <identity-or-id>` — send `BrowserDaemon.userscript.remove` with `{"key": ...}`.
- `toggle <identity-or-id> --enabled=<true|false>` — send `BrowserDaemon.userscript.toggle`.
- `logs [--id=<id>] [--limit=N]` — send `BrowserDaemon.userscript.logs`.

**Step 1: Write a failing test** for the pure parts: that `push` of a temp
`.user.js` file calls the ws shim with method `BrowserDaemon.userscript.install`
and a payload whose `script.id` equals the parsed id. Monkeypatch the ws-send
helper so no real daemon is needed:

```python
# browser-daemon/tests/test_cli_userscript.py
import json
from browser_daemon import cli

def test_push_parses_and_sends_install(tmp_path, monkeypatch):
    f = tmp_path / "x.user.js"
    f.write_text(
        "// ==UserScript==\n// @name X\n// @namespace n\n"
        "// @match https://e.com/*\n// ==/UserScript==\nwindow.x=1;\n")
    captured = {}
    async def fake_call(cfg, method, params, timeout=5.0):
        captured["method"] = method
        captured["params"] = params
        return {"ok": True, "id": params["script"]["id"]}
    monkeypatch.setattr(cli, "_userscript_call_ws", fake_call, raising=False)
    rc = cli._cmd_userscript(["push", str(f)])  # adapt to real signature
    assert rc == 0
    assert captured["method"] == "BrowserDaemon.userscript.install"
    assert captured["params"]["script"]["name"] == "X"
```

**Step 2: Run → fail.**

**Step 3: Implement** `_cmd_userscript(args)` + a `_userscript_call_ws(cfg, method,
params, timeout)` helper that mirrors `_disconnect_via_ws` (open ws to
`_ipc.sock_path(cfg.name)`, send `{"id":1,"method":method,"params":params}`, await
one reply, return `result` or raise on `error`). Wire `userscript` into the
daemon CLI's argument dispatch. Read the file's existing arg-dispatch structure
first and match it.

**Step 4: Run → pass. Step 5: Commit**
```bash
git add browser-daemon/src/browser_daemon/cli.py browser-daemon/tests/test_cli_userscript.py
git commit -m "feat(daemon): browser-daemon userscript {push,list,remove,toggle,logs} CLI"
```

---

## Phase 4 — Extension runtime (background.js)

> JS has no unit harness in this repo; these tasks are verified by the Phase 7
> e2e test. Implement carefully and keep functions small.

### Task 6: Userscript store + registration engine

**Files:**
- Modify: `browser-daemon/chrome-extension/background.js`

**Step 1:** Add a storage module section. Records live under
`chrome.storage.local` key `userscripts` (object keyed by `id`), plus a master
flag `userscriptsMasterEnabled` (default `true`) and an audit ring under
`userscriptLog` (array, cap 500).

```javascript
// ---- userscripts: store + chrome.userScripts registration ------------------
const US_KEY = "userscripts";
const US_MASTER = "userscriptsMasterEnabled";
const US_LOG = "userscriptLog";
const US_LOG_CAP = 500;

async function usGetAll() {
  const v = await chrome.storage.local.get([US_KEY, US_MASTER]);
  return { scripts: v[US_KEY] || {}, master: v[US_MASTER] !== false };
}
async function usPutRecord(rec) {
  const { scripts } = await usGetAll();
  scripts[rec.id] = { ...rec, updatedAt: Date.now(),
                      createdAt: scripts[rec.id]?.createdAt || Date.now() };
  await chrome.storage.local.set({ [US_KEY]: scripts });
  return scripts[rec.id];
}
async function usDelete(key) {
  const { scripts } = await usGetAll();
  const id = scripts[key] ? key
    : Object.values(scripts).find(s => s.identity === key)?.id;
  if (id) { delete scripts[id]; await chrome.storage.local.set({ [US_KEY]: scripts }); }
  return id || null;
}
async function usAppendLog(entry) {
  const v = await chrome.storage.local.get([US_LOG]);
  const log = v[US_LOG] || [];
  log.push(entry);
  if (log.length > US_LOG_CAP) log.splice(0, log.length - US_LOG_CAP);
  await chrome.storage.local.set({ [US_LOG]: log });
}
```

**Step 2:** Add the registration engine. Wrap each script with a tiny audit
prologue that reports an injection via `chrome.runtime.sendMessage` (enabled by
`configureWorld({messaging:true})`).

```javascript
function usWrapCode(rec) {
  // Report injection (best-effort) then run the user's code in its own scope.
  return (
    "try{chrome.runtime.sendMessage({type:'userscript.injected',id:" +
    JSON.stringify(rec.id) + ",url:location.href});}catch(e){}\n" +
    "(function(){\n" + rec.code + "\n})();"
  );
}

function usToRegistration(rec) {
  return {
    id: rec.id,
    matches: rec.matches,
    excludeMatches: rec.excludeMatches && rec.excludeMatches.length
      ? rec.excludeMatches : undefined,
    js: [{ code: usWrapCode(rec) }],
    runAt: rec.runAt || "document_idle",
    world: "USER_SCRIPT",
    allFrames: false,
  };
}

async function usSyncAll() {
  if (!chrome.userScripts) {
    console.warn("[bd-relay] chrome.userScripts unavailable (enable dev mode)");
    return { ok: false, reason: "userScripts API unavailable" };
  }
  try { await chrome.userScripts.configureWorld({ messaging: true }); } catch (e) {}
  const { scripts, master } = await usGetAll();
  const existing = await chrome.userScripts.getScripts({});
  if (existing.length) {
    await chrome.userScripts.unregister({ ids: existing.map(s => s.id) });
  }
  if (!master) return { ok: true, registered: 0 };
  const regs = Object.values(scripts)
    .filter(s => s.enabled !== false)
    .map(usToRegistration);
  if (regs.length) await chrome.userScripts.register(regs);
  return { ok: true, registered: regs.length };
}
```

**Step 3:** Register on SW startup. Near the existing top-level init, add:
```javascript
usSyncAll().catch(e => console.warn("[bd-relay] usSyncAll on init failed:", e));
```

**Step 4:** Manual sanity (no automated test here) — covered by Task 9 e2e.

**Step 5: Commit**
```bash
git add browser-daemon/chrome-extension/background.js
git commit -m "feat(ext): userscript store + chrome.userScripts registration engine"
```

### Task 7: `userscript.*` relay-message dispatch + injection-log listener

**Files:**
- Modify: `browser-daemon/chrome-extension/background.js`

**Step 1:** Add cases in `handleDaemonMessage` switch (before `default`):
```javascript
    case "userscript.install":
      return await doUserscriptInstall(id, msg.script);
    case "userscript.list":
      return await doUserscriptList(id, msg.site);
    case "userscript.remove":
      return await doUserscriptRemove(id, msg.key);
    case "userscript.toggle":
      return await doUserscriptToggle(id, msg.key, msg.enabled);
    case "userscript.logs":
      return await doUserscriptLogs(id, msg);
```

**Step 2:** Implement the handlers. Each responds via `safeSend({type:"response",
id, result})` (match how `doAttach` etc. reply — verify the exact response shape
used by existing handlers and mirror it):
```javascript
async function doUserscriptInstall(id, script) {
  const rec = await usPutRecord({ ...script,
    enabled: script.enabled !== false });
  const sync = await usSyncAll();
  safeSend({ type: "response", id,
    result: { id: rec.id, identity: rec.identity, warnings: rec.warnings || [],
              registered: sync.registered, apiOk: sync.ok } });
}
async function doUserscriptList(id, site) {
  const { scripts } = await usGetAll();
  let rows = Object.values(scripts).map(s => ({
    id: s.id, identity: s.identity, name: s.name, matches: s.matches,
    runAt: s.runAt, enabled: s.enabled !== false }));
  if (site) rows = rows.filter(s => usMatchesUrl(s.matches, s.excludeMatches, site));
  safeSend({ type: "response", id, result: { scripts: rows } });
}
async function doUserscriptRemove(id, key) {
  const removed = await usDelete(key);
  await usSyncAll();
  safeSend({ type: "response", id, result: { removed } });
}
async function doUserscriptToggle(id, key, enabled) {
  const { scripts } = await usGetAll();
  const rec = scripts[key] || Object.values(scripts).find(s => s.identity === key);
  if (rec) { rec.enabled = !!enabled; await chrome.storage.local.set({ [US_KEY]: scripts }); }
  await usSyncAll();
  safeSend({ type: "response", id, result: { ok: !!rec, enabled: !!enabled } });
}
async function doUserscriptLogs(id, msg) {
  const v = await chrome.storage.local.get(["userscriptLog"]);
  let log = v.userscriptLog || [];
  if (msg.id) log = log.filter(e => e.id === msg.id);
  if (msg.limit) log = log.slice(-Number(msg.limit));
  safeSend({ type: "response", id, result: { log } });
}
```

**Step 3:** Add a match helper (Chrome match-pattern → URL test). Implement a
small, correct matcher for `scheme://host/path` patterns incl. `*` host prefix
and `<all_urls>`:
```javascript
function usPatternToRe(p) {
  if (p === "<all_urls>") return /^(https?|file|ftp):\/\/.*/;
  const m = /^(\*|https?|file|ftp):\/\/([^/]*)(\/.*)$/.exec(p);
  if (!m) return null;
  const [, scheme, host, path] = m;
  const esc = s => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
  const schemeRe = scheme === "*" ? "https?" : scheme;
  const hostRe = esc(host).replace(/^\\\*\\\./, "(?:[^/]+\\.)?").replace(/\\\*/g, "[^/]*");
  const pathRe = esc(path).replace(/\\\*/g, ".*");
  return new RegExp("^" + schemeRe + "://" + hostRe + pathRe + "$");
}
function usMatchesUrl(matches, excludes, url) {
  const hit = (matches || []).some(p => { const r = usPatternToRe(p); return r && r.test(url); });
  if (!hit) return false;
  return !(excludes || []).some(p => { const r = usPatternToRe(p); return r && r.test(url); });
}
```

**Step 4:** Add the injection-log listener + popup message handlers in the
existing `chrome.runtime.onMessage` listener (popup uses `sendMessage`):
```javascript
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "userscript.injected") {
    usAppendLog({ ts: Date.now(), id: msg.id, url: msg.url,
                  tabId: sender.tab?.id });
    return; // no response needed
  }
  if (msg?.type === "userscript.popupList") {
    usGetAll().then(({ scripts, master }) => {
      const rows = Object.values(scripts)
        .filter(s => usMatchesUrl(s.matches, s.excludeMatches, msg.url))
        .map(s => ({ id: s.id, name: s.name, enabled: s.enabled !== false }));
      sendResponse({ scripts: rows, master });
    });
    return true; // async
  }
  if (msg?.type === "userscript.popupToggle") {
    doUserscriptToggleLocal(msg.id, msg.enabled).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg?.type === "userscript.popupMaster") {
    chrome.storage.local.set({ [US_MASTER]: !!msg.enabled })
      .then(usSyncAll).then(() => sendResponse({ ok: true }));
    return true;
  }
  // ... existing onMessage cases unchanged ...
});

async function doUserscriptToggleLocal(id, enabled) {
  const { scripts } = await usGetAll();
  if (scripts[id]) { scripts[id].enabled = !!enabled;
    await chrome.storage.local.set({ [US_KEY]: scripts }); }
  await usSyncAll();
}
```
> **NOTE:** verify the existing `chrome.runtime.onMessage` listener location
> (popup currently sends `status`/`attachActive`/`detachActive`). Merge these
> branches into that existing listener rather than adding a second one.

**Step 5: Commit**
```bash
git add browser-daemon/chrome-extension/background.js
git commit -m "feat(ext): userscript.* relay dispatch, match helper, injection audit log"
```

---

## Phase 5 — Popup UI (per-site list + Switch + master)

### Task 8: Popup shows this-site scripts with toggles

**Files:**
- Modify: `browser-daemon/chrome-extension/popup.html`
- Modify: `browser-daemon/chrome-extension/popup.js`

**Step 1 (popup.html):** Add a section below the attached-tabs list:
```html
<div class="section" id="us-section">
  <div class="row">
    <strong>Scripts on this site</strong>
    <label class="switch"><input type="checkbox" id="us-master"> <span>All</span></label>
  </div>
  <ul id="us-list"></ul>
  <p class="hint" id="us-empty" style="display:none">No userscripts match this site.</p>
</div>
```
Add minimal CSS for `.switch` (a checkbox styled as a toggle is fine for v1).

**Step 2 (popup.js):** After the existing refresh, fetch + render:
```javascript
async function refreshUserscripts() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return;
  const resp = await new Promise(r =>
    chrome.runtime.sendMessage({ type: "userscript.popupList", url: tab.url },
      x => r(x || { scripts: [], master: true })));
  document.getElementById("us-master").checked = resp.master !== false;
  const list = document.getElementById("us-list");
  const empty = document.getElementById("us-empty");
  list.innerHTML = "";
  empty.style.display = resp.scripts.length ? "none" : "block";
  for (const s of resp.scripts) {
    const li = document.createElement("li");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = s.enabled;
    cb.addEventListener("change", () =>
      chrome.runtime.sendMessage({ type: "userscript.popupToggle", id: s.id,
                                   enabled: cb.checked }));
    const label = document.createElement("span");
    label.textContent = " " + s.name;
    li.appendChild(cb); li.appendChild(label); list.appendChild(li);
  }
}
document.getElementById("us-master").addEventListener("change", e =>
  chrome.runtime.sendMessage({ type: "userscript.popupMaster", enabled: e.target.checked }));
refreshUserscripts();
```

**Step 3:** Manual check covered by e2e (Task 9 includes a popup-toggle assertion
via CDP if practical; otherwise toggle is asserted through `userscript.toggle`).

**Step 4: Commit**
```bash
git add browser-daemon/chrome-extension/popup.html browser-daemon/chrome-extension/popup.js
git commit -m "feat(ext): popup lists this-site userscripts with toggles + master switch"
```

---

## Phase 6 — browser-skill CLI shim (agent-facing)

### Task 9: `browser-skill userscript ...` delegates to `browser-daemon userscript`

**Files:**
- Modify: `browser-skill/src/browser_skill/cli.py` (`main()` dispatch ~line 470, HELP ~line 26)
- Test: `browser-skill/tests/test_cli_userscript.py`

**Step 1: Write the failing test** — assert `browser-skill userscript push f.user.js`
subprocesses `browser-daemon userscript push f.user.js` (monkeypatch
`subprocess.run`), and that `userscript` appears in HELP.

```python
# browser-skill/tests/test_cli_userscript.py
from browser_skill import cli

def test_userscript_delegates_to_daemon(monkeypatch):
    calls = {}
    class R: returncode = 0
    def fake_run(argv, **kw):
        calls["argv"] = argv; return R()
    monkeypatch.setattr(cli.subprocess, "run", fake_run, raising=False)
    rc = cli._cmd_userscript(["push", "f.user.js"])
    assert rc == 0
    assert calls["argv"][:2] == ["browser-daemon", "userscript"]
    assert "push" in calls["argv"]

def test_help_mentions_userscript():
    assert "userscript" in cli.HELP
```

**Step 2: Run → fail.**

**Step 3: Implement** `_cmd_userscript(args)` (delegates via `subprocess.run`
`["browser-daemon", "userscript", *args]`, propagating returncode), register
`if cmd == "userscript": sys.exit(_cmd_userscript(rest))` in `main()`, and add a
`userscript` line to `HELP`. Ensure `import subprocess` exists.

**Step 4: Run → pass. Step 5: Commit**
```bash
git add browser-skill/src/browser_skill/cli.py browser-skill/tests/test_cli_userscript.py
git commit -m "feat(skill): browser-skill userscript shim to daemon CLI"
```

---

## Phase 7 — Docs

### Task 10: Author `skill/userscripts.md` and link from `SKILL.md`

**Files:**
- Create: `skill/userscripts.md`
- Modify: `skill/SKILL.md` (add one linking line)

**Step 1:** Write `skill/userscripts.md` covering (per design doc §Documentation):
mental model (resident-Tampermonkey leg, parallel to CDP); `==UserScript==`
header spec + v1-supported directives + graceful degradation; capability-a
boundary (plain page JS, no `GM_*`); the **golden workflow**
(`Write/Edit .user.js → browser-skill userscript push → open target site via CDP
heredoc and verify → red ⇒ Edit → push → re-verify`); the **verification menu**
(UI change → DOM snapshot/screenshot; data pull → extract & judge; pure injection
→ read a shared DOM sentinel such as `document.documentElement.dataset.us*` (not `window.__us_*`, which is isolated in `USER_SCRIPT`); "always verify after writing"); command
cheat-sheet (`push / list / remove / toggle / logs`); note the popup shows
this-site scripts + Switch.

**Step 2:** Add to `skill/SKILL.md` a single line linking the new doc, e.g. under
an appropriate section: `- **Userscripts (resident):** see [userscripts.md](./userscripts.md) — author Tampermonkey-style scripts the extension runs on matching sites.`

**Step 3: Commit**
```bash
git add skill/userscripts.md skill/SKILL.md
git commit -m "docs(skill): userscripts authoring guide + link from SKILL.md"
```

---

## Phase 8 — End-to-end verification

### Task 11: e2e — install → inject → toggle → remove

**Files:**
- Create: `browser-daemon/tests/e2e/test_userscripts_e2e.py`
- Inspect first: `browser-daemon/tests/e2e/run.sh` and an existing e2e test to
  reuse the harness fixtures (Chrome-for-Testing on port 29989, extension
  backend, `run_skill`/session helpers).

**Step 1: Write the e2e test** following the existing e2e style:
1. Write a temp `.user.js` whose code sets a shared DOM sentinel such as `document.documentElement.setAttribute('data-us-e2e', 'ok')` and matches a
   stable local/test page served by the harness (reuse whatever page existing
   e2e tests load).
2. `browser-daemon userscript push <file>` (or the skill shim).
3. Open the matching page via the existing CDP session helper; `Runtime.evaluate`
   read `document.documentElement.getAttribute('data-us-e2e')` → expect `'ok'`.
4. `browser-daemon userscript toggle <identity> --enabled=false`; reload page;
   expect the DOM sentinel absent.
5. `browser-daemon userscript logs` → expect at least one entry with the script id.
6. `browser-daemon userscript remove <identity>`; `list` → empty.

> **NOTE:** match patterns must cover the harness's test page origin. If the
> harness serves `http://localhost:<port>/...`, use `http://localhost/*` or the
> page's real origin in `@match`. Confirm the test page URL from existing e2e
> tests before finalizing the pattern.

**Step 2: Run the e2e suite**
Run: `cd browser-daemon && bash tests/e2e/run.sh -k userscripts -v` (adapt to how
run.sh forwards pytest args; if it doesn't, run the full e2e once).
Expected: PASS.

**Step 3: Commit**
```bash
git add browser-daemon/tests/e2e/test_userscripts_e2e.py
git commit -m "test(e2e): resident userscript install/inject/toggle/remove"
```

### Task 12: Full regression — both suites green

**Step 1:** Run daemon unit suite:
`cd browser-daemon && uv run pytest -q`
**Step 2:** Run skill unit suite:
`cd browser-skill && uv run pytest -q`
**Step 3:** Run e2e once: `cd browser-daemon && bash tests/e2e/run.sh`
Expected: all green. If anything fails, fix before final commit. Do not overfit
e2e assertions to exact wording — assert behavior (sentinel present/absent, log
entry exists, list empty after remove).

**Step 4: Final commit (if any fixups)**
```bash
git add -A && git commit -m "chore: green both suites for resident userscripts"
```

---

## Out of scope (do NOT build in v1)

- `GM_*` APIs, `@require`/`@resource`/`@connect` remote code.
- Watch-mode auto-sync (explicit `push` only).
- Per-site permission prompts / activation confirmation (full-auto).
- Popup features beyond this-site list + per-script Switch + master switch.

## Risk notes for the implementer

- **`chrome.userScripts` availability** depends on the extension's developer-mode
  toggle. The e2e harness loads the extension unpacked, so it should be on; if
  `chrome.userScripts` is `undefined` in e2e, check the harness's extension-load
  flags. Guard all calls.
- **Verify every assumed symbol name** in `relay.py` / `proxy.py` /
  `extension_upstream.py` / `cli.py` (e.g. `self.upstream`, `_ensure_upstream`,
  `_result_response`, the extension-connection container) by reading the file
  before editing — the line numbers above are from a snapshot and may drift.
- **Mirror existing response shapes** in `background.js` handlers; if existing
  handlers reply with a shape other than `{type:"response", id, result}`, match
  theirs so `_request`'s future resolves.
