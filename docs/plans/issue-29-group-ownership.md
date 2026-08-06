# Issue #29 — a durable anchor for extension group ownership

**Status:** decided & implemented (2026-08).
**Problem:** `ExtensionUpstream` proves that a tab group belongs to a session
using two anchors — the group title (user-editable, non-unique) and the
numeric `groupId` (only unique *within one browser session*, recycled after a
restart). Neither is sound. Repro tests:
`tests/daemon/test_issue29_group_ownership.py` (6 red against the old code).

## What Chrome actually gives us

| Anchor | Survives daemon restart | Survives SW restart | Survives browser restart | Unique per session |
|---|---|---|---|---|
| `tabGroup.id` (numeric) | yes | yes | **no — recycled** | within one browser session |
| `tab.id` (numeric) | yes | yes | **no — recycled** | within one browser session |
| `tabGroup.title` | yes | yes | yes | **no — user-editable, duplicate** |
| `chrome.storage.session` keyed by tabId | yes | **yes** | **no — wiped by Chrome** | yes (while Chrome runs) |
| `chrome.storage.local` keyed by tabId | yes | yes | survives, but ids are recycled → **points at the wrong tabs** | no |

The load-bearing fact: **nothing survives a browser restart with identity.**
`chrome.storage.session` is wiped by Chrome on restart — which is a feature,
not a bug, for this problem: it guarantees no stale marker can ever point at a
recycled tab.

## The three directions from the issue

### (a) Marker inside the group's tabs; membership derived, not authoritative

Store an extension-owned per-tab marker (`chrome.storage.session`, keyed by
tabId, value = owning **sessionId** + groupId) written when the extension
places a tab in a session group. Ownership of a group is then *derived* from
"does a member tab carry this session's marker" instead of *asserted* from
ledger `group_id` + title coincidence.

- Kills the rename false-negative: the marker lives on the tabs, not in the
  title; a rename changes nothing.
- Kills the recycled-id false-positive *while Chrome runs*: markers are real
  bookkeeping the extension itself wrote, so a group can only be "proven" if
  the extension actually built it. The marker value must be the **sessionId**,
  not the groupId — a groupId-valued marker fails the cross-session case (B
  re-adopts, Chrome recycles A's old id to B's new group, and A would adopt
  B's group on the strength of B's own markers).
- Sound because `chrome.storage.session` is wiped on browser restart: stale
  markers can never attach to recycled tabs. (The tempting alternative —
  `storage.local` keyed by tabId — is *actively dangerous*: after a restart
  the ids point at random new tabs.)
- Cost: marker bookkeeping (write on tab placement, drop on close / drag-out
  / tab-removed), plus protocol plumbing (sessionId in `createTab` /
  `attachActive` relay messages so the extension can stamp tabs). Persistence
  is best-effort (fire-and-forget write-through); a lost marker fails *safe*
  (unproven → explicit error), never *unsafe*.
- Does **not** change teardown semantics: `endSession` still closes the whole
  live group (the DECIDED model — drag a tab out to spare it). Markers answer
  "whose group is this", not "which tabs are borrowed". The retired
  `_owned`/`_borrowed` model stays retired.

### (b) Best-effort recovery, explicit user-visible failure

Right posture, wrong anchor. Making the *failure* explicit (never silently
adopt, never silently refuse) is necessary but not sufficient: with only
title+id evidence there is no way to tell "the group was renamed" (harmless,
should recover) from "the id was recycled onto someone else's group" (must
not). Any heuristic that rescues one case risks the other — that is exactly
the "vanished retry anchor" wedge observed in the wild. Adopted as the
*messaging posture* of (a)+(c), not as the anchor.

### (c) Explicit re-adoption after a browser restart

After a restart no evidence can be sound, so the honest rule is: **an
unproven group is never adopted, and the failure is a hard, visible error
that names the fix.** The explicit adopt verb (`attachActiveTab` — the human
points at a tab) is the escape hatch: it falls back to a *fresh* group rather
than joining the unproven one. A restart with no surviving group (no session
restore, no recycled collision) keeps self-healing: the stale id is gone, the
next open creates a fresh group — no wedge.

## Decided design: (a) as the anchor, (c) as the restart rule, (b) as the messaging

1. **Extension** (`chrome-extension/background.js`): `ownedTabs` map
   `tabId → {s: sessionId, g: groupId}`, persisted in `chrome.storage.session`
   (survives SW/daemon restarts, wiped by Chrome on browser restart). Written
   when a tab is placed in a session group (`createTab`, `attachActive`);
   dropped on tab close, tab removed, and drag-out of the group. `queryGroup`
   responses annotate each member with `ownedSessionId`.
2. **Daemon** (`extension_upstream.py`): ownership validation uses marker
   evidence when present — group owned iff a member tab carries *this*
   session's id; title and last-known-tab-id are no longer consulted (they
   caused both failure directions). Unproven → `GroupOwnershipUnproven`
   (a `RuntimeError`) with a message naming re-adoption. Transport errors and
   validation mismatches still propagate as before.
3. **Legacy degradation**: an old extension's `queryGroup` reply has no
   `ownedSessionId` fields → the old title+membership heuristic applies,
   with the limitation stated at the enforcement point. (Same lesson as
   `groupTitle`: rejecting on absent evidence would brick every persisted
   session for anyone whose daemon upgraded ahead of their browser.)
4. **Restart rule (c)**: open/recover/teardown on an unproven group fail
   explicitly; `attachActiveTab` is the documented re-adoption escape (fresh
   group on the focused tab; the extension still refuses to steal a tab out
   of another group).

## Known limitations (documented at the enforcement point)

- **After a browser restart** the old workspace is unrecoverable by identity:
  markers are wiped, ids recycled. Recovery fails explicitly; the user
  re-adopts (or the session self-heals with a fresh group when the stale id
  is genuinely gone). Session-restored agent tabs that survive in an orphaned
  group are not auto-found (no evidence) — drag them into the new group.
- **Legacy extensions** (no marker fields) fall back to the old heuristic and
  keep both failure directions; only an extension update closes them.
- **Foreign tabs** added to a session's group by the user still close on
  `endSession` — the DECIDED teardown model (markers are ownership evidence,
  not a borrowed/owned set).
- Marker persistence is best-effort; a lost marker fails safe (explicit
  unproven error), never unsafe.

## Files touched

- `chrome-extension/background.js` — `ownedTabs` markers + `sessionId`
  protocol fields + queryGroup annotation.
- `src/browserwright/daemon/server/relay.py` — `session_id` plumbing into
  `createTab` / `attachActive` bodies.
- `src/browserwright/daemon/server/extension_upstream.py` — marker-based
  `_validate_recovered_group_ownership`, `GroupOwnershipUnproven`, the
  attachActive escape, sessionId forwarding.
- `CONTEXT.md` (`binding`), `docs/session-workspaces.md` — the anchor is now
  markers; the ledger `group_id` is a *candidate*, never proof.
- `tests/daemon/test_issue29_group_ownership.py` — the repro/spec tests.
