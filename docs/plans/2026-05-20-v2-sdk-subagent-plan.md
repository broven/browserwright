# v2 SDK sub-agent E2E Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a promptfoo-driven E2E suite that runs a `claude-agent-sdk` sub-agent (seeing only `skill/*.md`) against a real daemon + Chrome, to test whether the skill docs + code let a real Code Agent use the browser fluently.

**Architecture:** promptfoo `extensions:` hooks start a session-scoped isolated daemon + Chrome for Testing; a Python custom provider spawns a Claude sub-agent with restricted tools and returns `{output, metadata{trace}}`; Python scorers verify daemon state + filesystem side-effects + trace independently. Cases are a **north-star spec** — when red, fix the skill (docs/code) to green; never overfit.

**Tech Stack:** promptfoo (npm), `claude-agent-sdk` (Python), Chrome for Testing, the existing `browser-skill` / `browser-daemon` CLIs.

**Design doc:** `docs/plans/2026-05-20-v2-sdk-subagent-design.md` (read it first — it has the case forms, isolation matrix, and the anti-overfit work-mode).

---

## Reference (so you don't re-research)

### claude-agent-sdk (Python) — confirmed API
- Install: `pip install claude-agent-sdk`. Requires the `claude` CLI on PATH (SDK spawns it). Auth: `ANTHROPIC_API_KEY` env, else falls back to logged-in Claude Code (`~/.claude/.credentials.json`). On this machine `claude` is a shell function `command claude --dangerously-skip-permissions`; no API key is set, so the SDK uses the logged-in session.
- One-shot run: `query(prompt=..., options=ClaudeAgentOptions(...))` — async generator, iterate to drain.
- Multi-turn (needed for mock-user): `async with ClaudeSDKClient(options=...) as c: await c.query("..."); async for m in c.receive_response(): ...; await c.query("yes")`.
- `ClaudeAgentOptions(cwd, allowed_tools=[...], model=<sonnet id>, system_prompt, env={...}, max_turns, permission_mode, hooks={...})`.
  - `allowed_tools` only auto-approves; it does NOT remove other tools. Real restriction comes from a `PreToolUse` hook that denies.
  - For non-interactive Bash, run with `permission_mode="bypassPermissions"` AND keep a `PreToolUse` deny hook for the guard.
  - Confirm the exact sonnet model id at execution time (e.g. `claude-sonnet-4-6`); the field exists, the value wasn't in docs.
- Tool guard (documented): `hooks={"PreToolUse":[HookMatcher(matcher="Bash", hooks=[guard])]}`. `guard(input_data, tool_use_id, context)` inspects `input_data["tool_name"]` / `input_data["tool_input"]`; deny via `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`, allow via `{}`.
- Trace: every yielded message is the trace. `AssistantMessage.content` = list of `TextBlock`/`ToolUseBlock`; tool outputs come as `ToolResultBlock` inside `UserMessage`; run ends with one `ResultMessage` (read usage / num_turns / result text — confirm exact attrs against installed `claude_agent_sdk/types.py`).
- Key URL: https://github.com/anthropics/claude-agent-sdk-python

### promptfoo — confirmed API
- Custom Python provider: file `provider.py` exporting `def call_api(prompt, options, context) -> dict` returning `{"output": str, "tokenUsage": {...}, "metadata": {...}}`. Referenced in YAML as `providers: [{ id: "file://provider.py", config: {...} }]`. `options["config"]` carries the YAML `config:` block.
- Lifecycle: top-level `extensions: ["file://hooks.py:run_hook"]`; hook fn receives `(hook_name, context)` where `hook_name ∈ {beforeAll, afterAll, beforeEach, afterEach}`. Stash daemon PID / CDP info in env vars or a module singleton the provider re-reads.
- Filesystem assertions: `assert: [{type: python, value: "file://scorer.py:get_assert"}]`. `get_assert(output, context) -> bool | float | dict` (dict form: `{"pass": bool, "score": float, "reason": str, "componentResults": [...]}`). Plain Python — `import os, json, pathlib` to read files.
- Mock user: do it inside the provider (own the agent loop), not via `simulated-user`.
- Key URLs: https://www.promptfoo.dev/docs/providers/python , https://www.promptfoo.dev/docs/configuration/reference , https://www.promptfoo.dev/docs/configuration/expected-outputs/python

### Reuse from v1 (do NOT rewrite)
- `browser-daemon/tests/e2e/_patch_extension.py` — `patch_extension_dir(src_dir, *, relay_port)` copies `chrome-extension/` to a tmpdir and rewrites `RELAY_URL`. Reuse verbatim.
- `browser-daemon/tests/e2e/conftest.py` — has `_find_cft_binary()`, `_launch_cft_with_extension()`, `_kill_chrome()`, the daemon-spawn block, and `/__status__` polling. Extract the daemon+Chrome launch logic into a standalone importable module so both v1 fixtures and v2 hooks can call it (v1 conftest may keep thin wrappers).
- Isolation deltas vs v1: extension port **39989**, RDP port **39990**, `BD_NAME=bd-agent-e2e`. `BS_HOME` points into the workspace.

### Layout to create
```
browser-skill/tests/agent-e2e/
  promptfooconfig.yaml
  provider.py
  hooks.py
  workspace.py            # build/reset _workspace
  agent_runner.py         # claude-agent-sdk wrapper used by provider.py
  guards.py               # PreToolUse tool-guard
  scorers/
    case_a.py
    ...
  _workspace/             # gitignored, rebuilt each run
  _artifacts/             # gitignored, dumped on failure
  README.md
```

---

## Task 0: Environment & dependency setup

**Files:**
- Create: `browser-skill/tests/agent-e2e/` dir + `.gitignore` (ignore `_workspace/`, `_artifacts/`, `node_modules/`, `.venv-agent-e2e/`)
- Create: `browser-skill/tests/agent-e2e/requirements.txt` (`claude-agent-sdk`)

**Step 1:** Install promptfoo: `npm install -g promptfoo` (or add a local `package.json` with promptfoo as devDependency and use `npx promptfoo`). Verify: `promptfoo --version` (or `npx promptfoo --version`) prints a version.

**Step 2:** Create a venv for the SDK + provider and install: `python3.11 -m venv browser-skill/tests/agent-e2e/.venv-agent-e2e && browser-skill/tests/agent-e2e/.venv-agent-e2e/bin/pip install claude-agent-sdk`. Verify import: `.venv-agent-e2e/bin/python -c "import claude_agent_sdk, inspect; print('ok')"`.

**Step 3:** Inspect installed SDK types to lock exact attrs: `.venv-agent-e2e/bin/python -c "import claude_agent_sdk.types as t; print([n for n in dir(t)])"` and read `ResultMessage` fields. Record the real field names (usage / num_turns / result) in a comment at the top of `agent_runner.py`.

**Step 4:** Confirm CfT present: reuse v1's `_find_cft_binary()` logic — run a one-liner that imports it and prints the path; if `None`, install per its skip message (`npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing`).

**Step 5:** Commit the scaffolding dir + gitignore + requirements.

```bash
git add browser-skill/tests/agent-e2e/.gitignore browser-skill/tests/agent-e2e/requirements.txt
git commit -m "chore(agent-e2e): scaffold v2 sub-agent test dir + deps"
```

---

## Task 1: Workspace prep/reset

**Files:**
- Create: `browser-skill/tests/agent-e2e/workspace.py`
- Test: `browser-skill/tests/agent-e2e/test_workspace.py`

**Step 1: Write the failing test.** A test that calls `build_workspace(root)` and asserts: `root/skill/SKILL.md` is a symlink to the real `skill/SKILL.md`; `root/skill/tasks.md` is a symlink; `root/skill/memory.md` is a real file (copy, not symlink) with the same content as source; `root/.browser-skill/site-skills/` exists and is empty. Then call `reset_workspace(root)` after mutating `memory.md` + dropping a file in `.browser-skill/` and assert it's back to pristine.

**Step 2: Run, expect fail** (`build_workspace` undefined). `cd browser-skill && .venv-agent-e2e/bin/python -m pytest tests/agent-e2e/test_workspace.py -v` → FAIL.

**Step 3: Implement `workspace.py`.** `build_workspace(root: Path)`: resolve repo `skill/` dir; `os.symlink` SKILL.md + tasks.md; `shutil.copy2` memory.md; `mkdir -p .browser-skill/site-skills`. `reset_workspace(root)`: rmtree + rebuild (idempotent). Expose `SKILL_SRC` path constant.

**Step 4: Run, expect pass.**

**Step 5: Commit.** `feat(agent-e2e): isolated workspace build/reset`

---

## Task 2: Daemon + Chrome launch module (extract from v1) + promptfoo hooks

**Files:**
- Create: `browser-daemon/tests/e2e/_real_browser.py` (extracted launch logic) — or a shared module importable by both; keep v1 conftest working by importing from it.
- Create: `browser-skill/tests/agent-e2e/hooks.py`
- Test: `browser-skill/tests/agent-e2e/test_hooks_smoke.py`

**Step 1: Write the failing test.** A pytest that calls the hook's `beforeAll` equivalent (a `start_session()` fn in `hooks.py`), then polls `http://127.0.0.1:39989/__status__` and asserts daemon up + (after Chrome) `extensions >= 1`, then `stop_session()` and asserts the port is free. Mark it `real_chrome` so the inner loop skips it.

**Step 2: Run, expect fail.**

**Step 3: Implement.** Extract from v1 conftest into `_real_browser.py`: `find_cft_binary()`, `launch_cft_with_extension()`, `kill_chrome()`, `spawn_daemon(ext_port, rdp_port, name, log_path)`, `poll_status(port)`. In `hooks.py`: `start_session()` → patch ext via `patch_extension_dir(..., relay_port=39989)`, `spawn_daemon(39989, 39990, "bd-agent-e2e", ...)`, launch CfT, poll until `extensions>=1`; stash handles in a module singleton + write CDP/PID to a temp state file. `stop_session()` → kill Chrome, terminate daemon, rmtree patched ext. Expose `run_hook(hook_name, context)` dispatching beforeAll→start_session, afterAll→stop_session, beforeEach→`reset_workspace`.

**Step 4: Run, expect pass** (`-m real_chrome`).

**Step 5: Commit.** `feat(agent-e2e): session daemon+Chrome hooks (reuse v1 launch)`

---

## Task 3: Tool guard + agent runner + provider (skeleton, no assertions)

**Files:**
- Create: `browser-skill/tests/agent-e2e/guards.py`
- Create: `browser-skill/tests/agent-e2e/agent_runner.py`
- Create: `browser-skill/tests/agent-e2e/provider.py`
- Test: `browser-skill/tests/agent-e2e/test_guard.py`, `test_agent_runner_offline.py`

**Step 1 (guard test, no network): Write failing test.** `guards.pre_tool_use` denies `Read` of a `.py` path, denies `Bash` whose command doesn't start with `browser-skill`/`browser-daemon`, allows `Read` of `SKILL.md`, allows `Bash` `browser-skill <<…`. Assert the returned dict shapes (deny vs `{}`).

**Step 2: Run, expect fail. Step 3: Implement `guards.py`** per the PreToolUse signature in Reference. **Step 4: pass. Step 5: commit** `feat(agent-e2e): PreToolUse tool guard`.

**Step 6: Implement `agent_runner.py`.** `async def run_agent(task, *, workspace, env, model, max_turns, user_replies=None) -> AgentResult`. Uses `ClaudeSDKClient` with `ClaudeAgentOptions(cwd=workspace/'skill', allowed_tools=[Bash,Read,Write,Edit,Grep,Glob], model, system_prompt=MINIMAL, env, max_turns, permission_mode="bypassPermissions", hooks={PreToolUse:[HookMatcher(hooks=[guards.pre_tool_use])]})`. Drain messages into a trace list; if the agent ends a turn with a question and `user_replies` remain, `await client.query(next_reply)` and continue. Return `AgentResult(output, trace, turns, usage, asked_user: bool, failed_bash: int)`. `MINIMAL` system prompt: "You are a coding agent. The user will give you a task. Tools you can use are documented in ./SKILL.md (read it). Do the task." — no examples, no impl detail.

**Step 7: Implement `provider.py`.** `call_api(prompt, options, context)`: read workspace path + model from `options["config"]`; build env (`BS_HOME`, `BD_NAME=bd-agent-e2e`, `BD_EXTENSION_PORT=39989`, no_proxy); `anyio.run(run_agent, ...)`; return `{"output": result.output, "tokenUsage": result.usage_dict, "metadata": {"trace": ..., "turns": ..., "asked_user": ..., "failed_bash": ...}}`.

**Step 8: Offline runner test** — monkeypatch the SDK call to a canned message stream; assert `run_agent` parses trace/turns/asked_user correctly. Run, pass.

**Step 9: Commit.** `feat(agent-e2e): claude-agent-sdk runner + promptfoo provider`

---

## Task 4: Case A — prove-it (connect + open + summarize)

**Files:**
- Create: `browser-skill/tests/agent-e2e/promptfooconfig.yaml`
- Create: `browser-skill/tests/agent-e2e/scorers/case_a.py`

**Step 1: Write the YAML.** `extensions: ["file://hooks.py:run_hook"]`; one provider `file://provider.py` (config: workspace path, model=sonnet, max_turns=25); `tests:` = Case A with 2 phrasings (zh: "用浏览器打开 https://example.com，告诉我这个页面在讲什么。" / en: "Open https://example.com in a browser and tell me what the page is about.") sharing the same `assert: [{type: python, value: "file://scorers/case_a.py:get_assert"}, {type: llm-rubric, value: "mentions the page is an illustrative/example domain"}]`.

**Step 2: Write `scorers/case_a.py:get_assert`.** Independent verification (do NOT trust the agent): read daemon state by shelling `browser-skill` itself against the test daemon (`BD_NAME=bd-agent-e2e BD_EXTENSION_PORT=39989 ... browser-skill <<'PY' print(page_info()) PY`) and assert url contains `example.com`; from `context["providerResponse"]["metadata"]` assert `failed_bash <= 2` (warn, not hard-fail, if exceeded — encode as componentResult). Return dict with componentResults: daemon_ok (hard), wandered (soft warn).

**Step 3: Run the eval.** `cd browser-skill/tests/agent-e2e && promptfoo eval -c promptfooconfig.yaml` (use the venv python as the provider interpreter; set `PROMPTFOO_PYTHON=../.venv-agent-e2e/bin/python` or equivalent). Expected on first run: it MAY be red (north-star). If red, read `_artifacts/` + trace.

**Step 4: TDD loop — fix skill, not the test.** If the agent wandered or failed because `skill/SKILL.md` was unclear (e.g. didn't know to read memory.md for backend, picked wrong attach), improve `skill/SKILL.md` / `skill/memory.md`. **Anti-overfit gate:** every skill edit must help the whole class of "open a page and summarize" tasks, not just example.com — verify BOTH phrasings still pass. No hardcoded URLs/wording.

**Step 5: Green + artifact-on-failure wiring.** Add to `case_a.py` (or a shared `scorers/_artifacts.py`) the dump of `agent_trace.json`, `daemon.log`, `failure.png`, `workspace_snapshot/`, `env.txt` into `_artifacts/case_a/` when the scorer fails.

**Step 6: Commit.** `test(agent-e2e): Case A prove-it green (+ any skill fixes)`

**This is the first milestone.** Skeleton works: promptfoo + SDK + real daemon, sub-agent learns from docs only, harness verifies independently.

---

## Task 5: Mock-user component (shared by B/C)

Extend `agent_runner.run_agent` so `user_replies` is exercised end-to-end: detect "agent asked a question and yielded" (heuristic: turn ended with no tool call + text contains `?` / 确认 / save as task), inject next reply via `client.query(...)`. Add `metadata.asked_user` + the question text to the trace. Offline test with a canned "asks then proceeds" stream. Commit.

---

## Task 6: Case B — save preference to memory.md

YAML case (zh+en variants: "我以后都用 extension backend…记住"). Provider passes `user_replies=["yes, go ahead"]`. `scorers/case_b.py`: [fs] workspace `skill/memory.md` `## User preference` now contains the extension-backend preference; [fs] backend capability table intact; [trace] no-confirm = **warning** (not fail). TDD loop on `skill/SKILL.md` memory section if red. Anti-overfit: both phrasings. Commit.

---

## Task 7: Case C — solidify task (proactive ask = fail)

YAML case (zh+en: "每天早上帮我抓 Hacker News 首页前 5 条标题。"). `user_replies=["yes"]`. `scorers/case_c.py`: [trace] **asked "save as task?" — if not asked, FAIL**; [fs] task file created under `$BS_HOME/site-skills/news.ycombinator.com/tasks/`; [content] valid Python + imports + has scrape skeleton (parse with `ast`, optionally dry-import). TDD loop on `skill/SKILL.md` "When to suggest saving as a task" + `skill/tasks.md` if red. Anti-overfit: vary phrasing (different recurring-need wording) — the "ask" must trigger on the class, not the literal string. Commit.

---

## Task 8: Case D — site memory (explicit write)

YAML case (zh+en, explicit: "…顺便把这个站点需要注意的坑记到 site memory 里"). `scorers/case_d.py`: [fs] `$BS_HOME/site-skills/<host>/memory.md` appended an entry; [content] durable site-level note, append-only (didn't clobber). TDD loop if red. Commit.

---

## Task 9: Case E — skill auto-triggering (recall-only, lightweight)

Separate provider `provider_trigger.py` (NO daemon/Chrome — independent of hooks' session). `call_api`: system prompt lists the real `skill/SKILL.md` frontmatter `description` + a few distractor skill descriptions (find-domain, context7, frontend-design); asks the agent to pick which skill fits; returns the choice. YAML case: several "should-trigger but doesn't say browser-skill" tasks (zh+en variants: "帮我看看 example.com 写了啥并截图", "scrape this page's titles"). `scorers/case_e.py`: agent chose browser-skill. **Recall only** — no negative/over-trigger cases. TDD loop on the `description:` field in `skill/SKILL.md` frontmatter if red; anti-overfit: many phrasings share one assertion. Commit.

---

## Task 10: README + model matrix + CI note

`browser-skill/tests/agent-e2e/README.md`: how to run (`promptfoo eval`), env/auth (ANTHROPIC_API_KEY or logged-in claude), artifacts layout, isolation rationale, the north-star/anti-overfit work-mode. Add a sonnet→opus provider matrix in YAML (commented or as a second provider). Note CI deferred (needs headed Chrome + xvfb), same as v1. Commit.

---

## Notes for the executor
- **Cases are spec.** Red is expected on first run of each case. Fix `skill/*.md` (and `browser-skill/src/` only if a real code gap) to green. Never edit a case to match current behavior.
- **Anti-overfit gate after every skill edit:** does this help the whole task class? Do all phrasing variants still pass? No hardcoded URLs/wording/special-cases. If only one phrasing passes, you overfit — revert and generalize.
- **Don't touch** `browser-daemon/chrome-extension/background.js` RELAY_URL hardcode (the patcher handles it). Don't let the sub-agent start its own daemon/Chrome (the hooks own that). Don't give the sub-agent source-code read (guard blocks `.py`).
- Commit after every green step.
