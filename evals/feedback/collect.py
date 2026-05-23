#!/usr/bin/env python3
"""Harvest real Claude Code transcripts that actually USED the browserwright,
so the friction in those sessions can be fed back into the skill docs/CLI.

Companion to evals/run.py (synthetic, text-only scoring). This one mines the
real world: it walks ~/.claude/projects/**/*.jsonl, keeps only sessions where
an agent really ran the `browserwright` command (not just sessions that mention
it because SKILL.md was loaded as context), and is STATEFUL — each run emits
only what is new or has grown since the last run.

Usage:
    python3 evals/feedback/collect.py            # collect new/changed sessions
    python3 evals/feedback/collect.py --list     # show what WOULD be collected, no writes
    python3 evals/feedback/collect.py --all       # ignore state, collect everything qualifying
    python3 evals/feedback/collect.py --no-distill # raw copies only, skip the .md extracts
    python3 evals/feedback/collect.py --reset      # forget all state (next run re-collects)
    python3 evals/feedback/collect.py --root DIR   # override projects root

Each run writes a batch under inbox/<runid>/ containing raw jsonl copies, a
token-cheap distilled .md per session (the part you hand to Claude), and a
manifest.json. State lives in .state.json next to this script.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / ".claude" / "projects"
STATE_PATH = HERE / ".state.json"
INBOX = HERE / "inbox"

# A `browserwright` token is a real *invocation* when it sits in command
# position: at the start, after a shell separator/subshell/backtick, or after
# zero or more `VAR=val` env assignments. It is NOT an invocation when it is
# part of a path (~/.browserwright/...), a longer word (browserwright-foo), or
# a flag value — i.e. when preceded by /, ., -, or an alphanumeric.
_INVOKE = re.compile(
    r"""
    (?:^|[\n;&|(`])          # line start or a command boundary
    \s*
    (?:\w+=(?:"[^"]*"|'[^']*'|\S+)\s+)*   # optional leading FOO=bar env assignments
    browserwright            # the command itself
    (?![\w./-])              # not the prefix of a longer token / path
    """,
    re.VERBOSE,
)


def count_invocations(cmd: str) -> int:
    return len(_INVOKE.findall(cmd or ""))


def _selftest() -> None:
    """Guard the detector against the false positives we actually saw."""
    yes = [
        "browserwright <<'PY'\nattach_active()\nPY",
        "BD_SESSION=$sid browserwright <<'PY'\nprint(page_info())\nPY",
        "sid=$(browserwright session new --backend=extension --name=research)",
        "browserwright whoami --session=$sid",
        'BU_NAME="work" browserwright list-tasks',
        "cd /tmp && browserwright session end --session=$sid",
    ]
    no = [
        "ls -la /Users/metajs/gitRepos/labs/browser-harness/",   # path
        "cat ~/.browserwright/site-skills/foo.py",               # path under dotdir
        "echo browserwright-helper",                              # longer token
        "grep browserwright README.md",                          # an argument, not cmd position
        "browserwright-daemon serve --backend extension",              # different command
    ]
    bad = [c for c in yes if count_invocations(c) == 0] + \
          [c for c in no if count_invocations(c) > 0]
    if bad:
        print("DETECTOR SELFTEST FAILED on:", file=sys.stderr)
        for c in bad:
            print("   ", repr(c), "->", count_invocations(c), file=sys.stderr)
        sys.exit(2)


# --------------------------------------------------------------------------- #
# jsonl parsing helpers
# --------------------------------------------------------------------------- #

def _iter_events(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _content_blocks(ev):
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    if isinstance(c, list):
        return c
    if isinstance(c, str):
        return [{"type": "text", "text": c, "_role": ev.get("type")}]
    return []


def scan_invocations(path: Path) -> int:
    """How many real browserwright Bash invocations are in this transcript."""
    n = 0
    for ev in _iter_events(path):
        for b in _content_blocks(ev):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                n += count_invocations((b.get("input") or {}).get("command", ""))
    return n


def project_name(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    # first path component is the encoded project dir
    return rel.parts[0] if rel.parts else path.parent.name


# --------------------------------------------------------------------------- #
# distillation: turn a 2000-line transcript into the friction-relevant story
# --------------------------------------------------------------------------- #

# Genuine failure signatures only — NOT bare words like "error"/"failed" that
# appear constantly inside scraped page content. We flag a result as friction
# when its output carries a real Python/shell/CDP/skill failure marker.
ERR_HINT = re.compile(
    r"""
    Traceback\ \(most\ recent\ call\ last\)   # python traceback
  | ^\s*\w*(?:Error|Exception):              # FooError: / Exception: at line start
  | command\ not\ found
  | Permission\ denied
  | non-zero\ exit | exit\ code\s*[12]
  | No\ session | BD_SESSION                 # browserwright loud refusal
  | \-3260[01] | \-32000                     # CDP method-not-found / generic
  | Could\ not\ connect | Connection\ refused | daemon\ not\ running
  | Timeout(?:Error)? | timed\ out\ waiting
  """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)
RESULT_CAP = 1800   # chars of a tool result to keep (errors get the full cap)
TEXT_CAP = 1200     # chars of an assistant/user text block to keep


def _as_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(b.get("text") or b.get("content") or "")
            else:
                out.append(str(b))
        return "\n".join(str(x) for x in out)
    return str(content)


def _clip(s: str, cap: int) -> str:
    s = s or ""
    if len(s) <= cap:
        return s
    head = s[: cap // 2]
    tail = s[-cap // 2:]
    return f"{head}\n… [clipped {len(s) - cap} chars] …\n{tail}"


CTX_BUFFER = 6   # max recent text blocks kept as context before an emitted call


def _call_hash(cmd: str, res: str) -> str:
    return hashlib.sha1((cmd.strip() + "\x00" + res.strip()).encode("utf-8", "replace")).hexdigest()


def distill(path: Path, root: Path, seen_calls: set[str]) -> tuple[str, dict]:
    """Return (markdown, stats). Emits ONLY browserwright calls whose
    sha1(command+result) is not already in `seen_calls` (which it mutates), so a
    given call is distilled at most once ever — even across a grown file's
    re-scan or a forked/resumed session saved under a new uuid. Each emitted
    call carries its immediately preceding human/assistant context; unrelated
    tool noise (Read/grep/Edit) is dropped. Real failures are tagged FRICTION."""
    # First pass: map tool_use_id -> result text (results live in later events).
    results: dict[str, str] = {}
    for ev in _iter_events(path):
        for b in _content_blocks(ev):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    results[tid] = _as_text(b.get("content"))

    lines: list[str] = []
    pending: list[str] = []   # recent context since the last decision point
    n_total = n_new = n_err = n_retry = n_dup = 0
    last_cmd = None

    for ev in _iter_events(path):
        role = ev.get("type")
        for b in _content_blocks(ev):
            if not isinstance(b, dict):
                continue
            btype = b.get("type")

            # human prompts (plain string user messages, not tool results)
            if btype == "text" and b.get("_role") == "user":
                txt = b.get("text", "").strip()
                if txt and not txt.startswith("<"):  # skip system-reminder noise
                    pending.append(f"\n### 👤 user\n{_clip(txt, TEXT_CAP)}")
                    pending[:] = pending[-CTX_BUFFER:]
                continue

            # assistant reasoning
            if btype == "text" and role == "assistant":
                txt = b.get("text", "").strip()
                if txt:
                    pending.append(f"\n**assistant:** {_clip(txt, TEXT_CAP)}")
                    pending[:] = pending[-CTX_BUFFER:]
                continue

            # the browserwright calls
            if btype == "tool_use" and b.get("name") == "Bash":
                cmd = (b.get("input") or {}).get("command", "")
                if count_invocations(cmd) == 0:
                    continue
                n_total += 1
                res = results.get(b.get("id"), "")
                h = _call_hash(cmd, res)
                if h in seen_calls:        # already distilled in some earlier batch
                    n_dup += 1
                    pending.clear()        # its context belongs to a seen call too
                    continue
                seen_calls.add(h)
                n_new += 1
                if cmd.strip() == (last_cmd or "").strip():
                    n_retry += 1
                last_cmd = cmd
                is_err = bool(ERR_HINT.search(res))
                if is_err:
                    n_err += 1
                flag = " ⚠️ FRICTION" if is_err else ""
                lines.extend(pending)      # flush the immediate context, then the call
                pending.clear()
                lines.append(f"\n#### ▶ browserwright call #{n_new}{flag}")
                lines.append("```bash\n" + cmd.strip() + "\n```")
                cap = RESULT_CAP * 2 if is_err else RESULT_CAP
                lines.append("_result:_\n```\n" + _clip(res.strip(), cap) + "\n```")

    header = [
        f"# {project_name(path, root)} :: {path.stem[:8]}",
        f"- source: `{path}`",
        f"- NEW browserwright calls: **{n_new}**  ·  friction-flagged: **{n_err}**  ·  "
        f"identical retries: **{n_retry}**  ·  duplicates suppressed: **{n_dup}** / {n_total} total",
        "",
        "> Distilled for skill-improvement review: only calls not seen in an "
        "earlier batch, each with its immediate human/assistant context. "
        "Unrelated tool calls (Read/grep/Edit) are dropped. ⚠️ marks results "
        "with a real failure marker — start there.",
    ]
    stats = {"invocations": n_new, "invocations_total": n_total,
             "duplicates": n_dup, "friction": n_err, "retries": n_retry}
    return "\n".join(header + lines) + "\n", stats


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"files": {}}


def save_state(state: dict) -> None:
    state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def find_jsonl(root: Path):
    yield from root.rglob("*.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                    help=f"Claude Code projects root (default: {DEFAULT_ROOT})")
    ap.add_argument("--list", action="store_true", help="show what would be collected; write nothing")
    ap.add_argument("--all", action="store_true", help="ignore state; (re)collect everything qualifying")
    ap.add_argument("--keep-raw", action="store_true",
                    help="also copy full raw jsonl (default: distilled + manifest only; "
                         "raw originals stay in the projects root, recorded in manifest)")
    ap.add_argument("--reset", action="store_true", help="clear state and exit")
    args = ap.parse_args()

    _selftest()

    if args.reset:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        print("state cleared:", STATE_PATH)
        return 0

    if not args.root.exists():
        print(f"projects root not found: {args.root}", file=sys.stderr)
        return 1

    state = {"files": {}} if args.all else load_state()
    seen = state.setdefault("files", {})

    qualifying = []   # (path, invocations, size, changed)
    for path in find_jsonl(args.root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        key = str(path)
        prev = seen.get(key)
        # cheap skip: known file, unchanged size -> nothing new
        if prev and prev.get("size") == size and not args.all:
            continue
        inv = scan_invocations(path)
        if inv == 0:
            # remember non-qualifying size too, so we don't rescan until it grows
            seen[key] = {"size": size, "invocations": 0,
                         "first_seen": (prev or {}).get("first_seen") or _now()}
            continue
        changed = bool(prev) and prev.get("size") != size
        qualifying.append((path, inv, size, changed))

    qualifying.sort(key=lambda t: t[1], reverse=True)

    if not qualifying:
        if args.list:
            print("nothing new to collect.")
        else:
            save_state(state)
            print("nothing new to collect. state updated.")
        return 0

    new_n = sum(1 for _, _, _, ch in qualifying if not ch)
    grown_n = len(qualifying) - new_n
    print(f"{len(qualifying)} qualifying session(s): {new_n} new, {grown_n} grown since last run")
    for path, inv, size, changed in qualifying:
        tag = "GROWN" if changed else "new  "
        print(f"  [{tag}] {inv:>3} calls  {project_name(path, args.root)[:46]:46}  {path.name}")

    if args.list:
        print("\n(--list: no files written)")
        return 0

    runid = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = INBOX / runid
    dist_dir = batch / "distilled"
    dist_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = batch / "raw"
    if args.keep_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)

    seen_calls = set(state.get("seen_calls", []))
    manifest = {"runid": runid, "root": str(args.root), "sessions": []}
    n_emitted = n_skipped = 0
    for path, inv, size, changed in qualifying:
        proj = project_name(path, args.root)
        stem = f"{proj}__{path.stem[:8]}"
        md, stats = distill(path, args.root, seen_calls)
        # always record the size so an unchanged file is never rescanned
        seen[str(path)] = {"size": size, "invocations": inv,
                           "first_seen": (seen.get(str(path)) or {}).get("first_seen") or _now(),
                           "last_collected": runid}
        if stats["invocations"] == 0:   # grown/forked, but every call already seen
            n_skipped += 1
            continue
        if args.keep_raw:
            (raw_dir / f"{stem}.jsonl").write_bytes(path.read_bytes())
        (dist_dir / f"{stem}.md").write_text(md, encoding="utf-8")
        manifest["sessions"].append(
            {"project": proj, "session": path.stem, "source": str(path),
             "bytes": size, "status": "grown" if changed else "new", **stats})
        n_emitted += 1
    state["seen_calls"] = sorted(seen_calls)

    # rank the index by friction so the review starts where it hurts
    manifest["sessions"].sort(key=lambda s: (s.get("friction", 0), s["invocations"]), reverse=True)
    (batch / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    save_state(state)

    if n_emitted == 0:
        # everything qualifying was already-seen calls (grown/forked, no new content)
        import shutil
        shutil.rmtree(batch, ignore_errors=True)
        print(f"\nno NEW browserwright calls ({n_skipped} session(s) grown/forked but "
              f"fully seen before). nothing written; state updated.")
        return 0

    total_friction = sum(s.get("friction", 0) for s in manifest["sessions"])
    total_dup = sum(s.get("duplicates", 0) for s in manifest["sessions"])
    print(f"\nwrote batch → {batch}")
    print(f"  {n_emitted} session(s) with new calls; {n_skipped} skipped (all calls already seen)")
    print(f"  distilled md: {dist_dir}   (⚠️ friction-flagged: {total_friction}, "
          f"duplicate calls suppressed: {total_dup})")
    if args.keep_raw:
        print(f"  raw copies:   {raw_dir}")
    print(f"  manifest:     {batch / 'manifest.json'}   (sessions ranked by friction)")
    print("\nFeed to Claude:  point it at the distilled/ dir, or the top-friction .md files.")
    return 0


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
