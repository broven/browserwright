"""Readiness heuristic for solidification (spec §B.4.0)."""
from __future__ import annotations

import re
from typing import Optional


_SIDE_EFFECT_RX = re.compile(r"click_at_xy|press_key\(['\"]Enter|submit|press_key\(['\"](?:Tab|Backspace)", re.IGNORECASE)
_BUY_RX = re.compile(r"\b(buy|submit|send|pay|publish|order|book)\b", re.IGNORECASE)
_AUTH_RX = re.compile(r"\b(login|signin|password|otp)\b", re.IGNORECASE)
_CAPTCHA_RX = re.compile(r"captcha|recaptcha|hcaptcha", re.IGNORECASE)
_MANUAL_RX = re.compile(r"\binput\(|\bawait_human|wait_for_user_confirm")
_PARAM_HINT_RX = re.compile(r"^\s*(?:[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*[\"'])|\bargs\[")
_STRUCTURED_OUT_RX = re.compile(r"\breturn\s+(?:\[|\{|Array|dict)|js\(.*?map\s*\(")


def _flat_code(history: list[dict]) -> str:
    pieces = []
    for h in history:
        if h.get("ok"):
            pieces.append(h.get("code", ""))
    return "\n".join(pieces)


def _suggest_name(history: list[dict], hint: Optional[str]) -> str:
    if hint:
        return _sanitize(hint)
    # Pull last goto_url's host as a fallback hint.
    rx = re.compile(r"goto_url\s*\(\s*[\"']https?://([^/\s'\"]+)")
    last_host = None
    for h in reversed(history):
        m = rx.search(h.get("code", ""))
        if m:
            last_host = m.group(1)
            break
    base = "scrape" if last_host else "task"
    if last_host:
        suffix = last_host.split(".")[0]
        return _sanitize(f"{base}_{suffix}")
    return base


def _sanitize(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return name or "task"


def _host_from_history(history: list[dict]) -> Optional[str]:
    rx = re.compile(r"https?://([^/\s'\"]+)")
    for h in reversed(history):
        m = rx.search(h.get("code", ""))
        if m:
            return m.group(1)
    return None


READINESS_THRESHOLD = 0.55


_RETURN_DICT_RX = re.compile(
    r"return\s*\{\s*([^{}]*?)\}", re.MULTILINE | re.DOTALL,
)
_RETURN_LIST_RX = re.compile(r"return\s*(?:\[|list\()", re.MULTILINE)
_DICT_KEY_RX = re.compile(r"""(?:^|,)\s*['"]([a-zA-Z_][a-zA-Z0-9_]*)['"]\s*:""")


def _infer_output_schema(body: str) -> dict | None:
    """Best-effort: scan ``body`` for a single ``return {...}`` literal
    and turn its keys into a JSON-schema-shaped dict. ``return [...]`` /
    ``return list(...)`` becomes an array shape. Returns ``None`` when
    the body's return shape is unknown — scaffold then emits the
    commented placeholder. REVIEW.md F-7."""
    if not body:
        return None
    m = _RETURN_DICT_RX.search(body)
    if m:
        keys = _DICT_KEY_RX.findall(m.group(1))
        if keys:
            return {
                "type": "object",
                "properties": {k: {"type": "Any"} for k in keys},
                "required": keys,
            }
    if _RETURN_LIST_RX.search(body):
        return {"type": "array", "items": {"type": "Any"}}
    return None


def propose(session, *, name_hint: Optional[str] = None,
            like: Optional[str] = None) -> dict:
    """Compute a readiness score and return a structured dict (Bug 3 from
    the v0.3 AI E2E run — pre-fix this returned ``None`` below threshold,
    giving the agent no signal about why).

    Return shape (every call):

      {
        "ready":            bool,
        "readiness_score":  float in [0, 1],
        "threshold":        float,           # READINESS_THRESHOLD
        "reasons":          list[str],       # positive signals that contributed
        "warnings":         list[str],       # anti-signals / diagnostics
        "name_hint":        str,
        "suggested_name":   str,             # alias of name_hint (compat)
        "site":             str,             # eTLD+1 stem or "unknown"
        "host_hint":        str | None,
      }

    When ``ready=True`` the dict also carries the scaffold seed:

        "draft_run_body", "draft_args_schema", (optional) "donor".

    ``like="<site>/<task>"`` (v0.2): seed the draft_run_body from an existing
    task's ``run()``, with URL host substitutions applied so the agent can
    review-and-tweak rather than rewriting from scratch. With ``like`` and
    no history, ``ready`` stays False (we won't clone blind) but the
    returned dict still flags the missing-history reason so the agent can
    explain it to the user.
    """
    from ..memory.site_mem import host_stem, site_dir
    from . import extract

    code = _flat_code(session.history)
    score = 0.5
    reasons: list[str] = []
    warnings: list[str] = []

    if not code.strip():
        if like:
            warnings.append("`like` requested but no REPL history — "
                            "won't clone blind; run a few steps first")
        else:
            warnings.append("no REPL history yet — record a successful run "
                            "before solidifying")
        score = 0.0

    # signals
    if _PARAM_HINT_RX.search(code):
        score += 0.20
        reasons.append("参数化清晰：识别到候选 args")
    if _STRUCTURED_OUT_RX.search(code):
        score += 0.15
        reasons.append("输出结构化：含 return/dict/array")
    if code.strip() and not _BUY_RX.search(code):
        score += 0.10
        reasons.append("无明显外发副作用")
    if code.strip() and not _MANUAL_RX.search(code):
        score += 0.10
        reasons.append("不依赖手动 input()")

    # anti-signals
    if _AUTH_RX.search(code):
        score -= 0.30
        warnings.append("流程中出现 auth/login 字样，固化跑会再次撞墙")
    if _CAPTCHA_RX.search(code):
        score -= 0.20
        warnings.append("命中 captcha 信号")
    success_steps = sum(1 for h in session.history if h.get("ok"))
    if success_steps > 30:
        score -= 0.15
        warnings.append("成功步数 > 30，探查噪音多")

    host = _host_from_history(session.history)
    if host:
        if not site_dir(host).exists():
            score -= 0.10
            warnings.append(f"首次访问 {host}，selftest 需要 agent 补 URL pattern assert")
    else:
        warnings.append("未识别到目标站点")

    score = max(0.0, min(1.0, score))
    ready = score >= READINESS_THRESHOLD
    name = _suggest_name(session.history, name_hint)
    out: dict = {
        "ready": ready,
        "readiness_score": round(score, 2),
        "threshold": READINESS_THRESHOLD,
        "reasons": reasons,
        "warnings": warnings,
        "name_hint": name,
        "suggested_name": name,
        "site": host_stem(host) if host else "unknown",
        "host_hint": host,
    }
    if not ready:
        return out

    body, args_schema = extract.extract_run_body(session.history)
    out["draft_run_body"] = body
    out["draft_args_schema"] = args_schema
    # REVIEW.md F-7: best-effort output-schema inference. Cheap shape
    # heuristic — if the extracted body ends with ``return {...}`` we
    # emit an object skeleton with the literal keys we can see; if it
    # ends with ``return [...]``/``return list(...)`` we emit an array
    # skeleton. Everything else stays None and the scaffold writes the
    # commented-out placeholder. Agent refines after first real run.
    inferred = _infer_output_schema(body)
    if inferred is not None:
        out["draft_output_schema"] = inferred
    if like:
        donor = _load_donor(like, host=host)
        if donor is None:
            out["warnings"].append(f"`like={like}` donor not found; using history-only draft")
        else:
            out["reasons"].append(f"seeded from donor: {like}")
            out["draft_run_body"] = donor["body"]
            # Merge donor args schema as fallback (history-derived takes priority).
            for k, v in donor["args"].items():
                out["draft_args_schema"].setdefault(k, v)
            out["donor"] = like
    return out


def _load_donor(spec: str, *, host: Optional[str]) -> Optional[dict]:
    """Load ``<site>/<task>`` and return its ``run()`` source + ARGS, with
    URL host rewrites applied (donor host → ``host``)."""
    if "/" not in spec:
        return None
    site, name = spec.split("/", 1)
    try:
        from ..discovery import find_task_path
        path = find_task_path(site, name)
    except FileNotFoundError:
        return None
    text = path.read_text(encoding="utf-8")
    # Pull run() body (best-effort textual extraction; no AST to stay zero-dep).
    body = _extract_run_body_text(text)
    if not body:
        return None
    # Pull donor's URL host so we can swap it.
    import re as _re
    donor_host_match = _re.search(r"https?://([^/\"' )]+)", text)
    donor_host = donor_host_match.group(1) if donor_host_match else None
    if donor_host and host and donor_host != host:
        body = body.replace(donor_host, host)
    # Pull donor's ARGS by exec-in-isolation (the module is trusted; same
    # standard ``from browser_skill import *`` header as every task).
    args_schema: dict = {}
    try:
        import importlib.util
        spec_ = importlib.util.spec_from_file_location(
            f"donor_{site}_{name}", path)
        if spec_ and spec_.loader:
            mod = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(mod)
            args_schema = dict(getattr(mod, "ARGS", {}) or {})
    except Exception:
        args_schema = {}
    return {"body": body, "args": args_schema}


def _extract_run_body_text(source: str) -> Optional[str]:
    """Return the textual body of ``def run(args, ctx=None):`` — used as a
    template seed for cross-site analogy. We deliberately do this with
    string ops rather than AST so the formatting stays close to what an
    agent wrote in REPL."""
    import re as _re

    m = _re.search(r"^def run\([^)]*\):\s*\n", source, _re.MULTILINE)
    if not m:
        return None
    rest = source[m.end():]
    out: list[str] = []
    for line in rest.splitlines(keepends=True):
        if line.strip() == "":
            out.append(line)
            continue
        # First non-blank line sets the indent. Stop when we see a line at
        # column 0 (next def / module-level statement).
        if not line.startswith((" ", "\t")):
            break
        out.append(line)
    body = "".join(out).rstrip("\n") + "\n"
    return body
