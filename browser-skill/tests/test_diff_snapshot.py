"""S2 gate: ``diff_snapshot(before, after=None)`` — the cheap post-action
verification primitive built on top of S1's ``snapshot()``.

Two layers of assertion, mirroring ``test_perception.py``:

1. **Surface contract (offline, always runs).** ``diff_snapshot`` is in
   ``browser_skill.EXPORTS``, importable from the top-level namespace, and
   lands in the assembled REPL globals (free name inside an agent heredoc).

2. **Behaviour (live, needs Chromium).** We reuse the exact S1 harness — a
   headless Chromium driven through the skill's own CDP transport, with the
   controlled DOM injected via ``js()`` (the extension backend ignores
   ``goto_url("data:...")``). We take ``before = snapshot()``, then mutate the
   DOM three ways — ADD an interactive node, REMOVE one, CHANGE one (toggle a
   button's ``disabled``) — take ``after = snapshot()``, and assert the diff's
   added/removed/changed buckets contain exactly the mutated nodes. Asserted
   by *shape* (counts + role/name membership), never by id/selector strings.

The behaviour layer skips cleanly with no Chromium binary, so the surface
gate stays runnable offline.
"""
from __future__ import annotations

import pytest

# Reuse the S1 harness verbatim: live_session + the chromium discovery /
# port plumbing live in test_perception. Importing the fixture here makes it
# available to this module (pytest resolves fixtures by name across imports).
from test_perception import live_session  # noqa: F401


# ---------------------------------------------------------------------------
# Surface contract — offline, deterministic.
# ---------------------------------------------------------------------------


def test_diff_snapshot_in_exports():
    import browser_skill

    assert "diff_snapshot" in browser_skill.EXPORTS


def test_diff_snapshot_importable_from_namespace():
    from browser_skill import diff_snapshot  # noqa: F401

    assert callable(diff_snapshot)


def test_diff_snapshot_in_repl_globals():
    from browser_skill.repl._namespace import build_globals

    g = build_globals()
    assert "diff_snapshot" in g and callable(g["diff_snapshot"])


def test_diff_snapshot_signature_before_after():
    """Signature is diff_snapshot(before, after=None): before is required,
    after defaults to a fresh snapshot()."""
    import inspect as _inspect

    from browser_skill import diff_snapshot

    sig = _inspect.signature(diff_snapshot)
    params = list(sig.parameters)
    assert params[:2] == ["before", "after"]
    assert sig.parameters["after"].default is None


# ---------------------------------------------------------------------------
# Behaviour — live headless Chromium, three mutations, exact bucketing.
# ---------------------------------------------------------------------------

# A controlled DOM: three on-screen interactive nodes with stable, distinct
# accessible names. No site/class/selector from any real page — pure
# injection. Names are arbitrary test strings; assertions match on the
# *mutated* node's role/name membership, computed at runtime, not literals.
_BASE = r"""
return (function(){
  document.documentElement.innerHTML = '<head></head><body></body>';
  function mk(tag, attrs){
    var e=document.createElement(tag);
    for(var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  var keep = document.createElement('button');
  keep.textContent='Alpha Keep';
  keep.style.cssText='position:fixed;top:40px;left:40px;width:120px;height:30px';
  document.body.appendChild(keep);

  var rem = document.createElement('button');
  rem.id='to-remove';
  rem.textContent='Beta Remove';
  rem.style.cssText='position:fixed;top:90px;left:40px;width:120px;height:30px';
  document.body.appendChild(rem);

  var chg = document.createElement('button');
  chg.id='to-change';
  chg.textContent='Gamma Toggle';
  chg.style.cssText='position:fixed;top:140px;left:40px;width:120px;height:30px';
  document.body.appendChild(chg);
  return true;
})();
"""

# Mutations applied AFTER the `before` snapshot.
_MUTATE = r"""
return (function(){
  // (a) ADD a new interactive node with a fresh accessible name.
  var added = document.createElement('a');
  added.textContent='Delta Added';
  added.href='https://example.org/added';
  added.style.cssText='position:fixed;top:190px;left:40px;width:120px;height:30px';
  document.body.appendChild(added);

  // (b) REMOVE one of the originals.
  var rem = document.getElementById('to-remove');
  if(rem) rem.remove();

  // (c) CHANGE one: toggle disabled on the third button.
  var chg = document.getElementById('to-change');
  if(chg) chg.disabled = true;
  return true;
})();
"""


def _names(bucket):
    """Accessible names present in a diff bucket (list of node-ish dicts)."""
    out = []
    for item in bucket:
        # changed entries may carry before/after; tolerate either shape.
        n = item.get("name")
        if n is None and isinstance(item.get("after"), dict):
            n = item["after"].get("name")
        if n is None and isinstance(item.get("before"), dict):
            n = item["before"].get("name")
        out.append(n)
    return out


def test_diff_snapshot_buckets_exact_mutations(live_session):
    from browser_skill import diff_snapshot, js, snapshot

    js(_BASE)
    before = snapshot()
    before_names = {n.get("name") for n in before["nodes"]}
    assert {"Alpha Keep", "Beta Remove", "Gamma Toggle"} <= before_names

    js(_MUTATE)
    after = snapshot()

    d = diff_snapshot(before, after)
    assert isinstance(d, dict)
    for key in ("added", "removed", "changed", "unchanged", "summary"):
        assert key in d, f"diff_snapshot output missing {key!r}"

    added_names = _names(d["added"])
    removed_names = _names(d["removed"])
    changed_names = _names(d["changed"])

    # ADD: the new link must be the only added node.
    assert "Delta Added" in added_names
    assert len(d["added"]) == 1
    # The added node should carry its role so the agent knows what appeared.
    assert any(item.get("role") == "link" for item in d["added"])

    # REMOVE: the removed button must be the only removed node.
    assert "Beta Remove" in removed_names
    assert len(d["removed"]) == 1

    # CHANGE: the disabled-toggled button must be in changed, and NOT in
    # added/removed (same identity, differing attrs).
    assert "Gamma Toggle" in changed_names
    assert "Gamma Toggle" not in added_names
    assert "Gamma Toggle" not in removed_names

    # The untouched button must NOT appear in any mutation bucket.
    assert "Alpha Keep" not in added_names
    assert "Alpha Keep" not in removed_names
    assert "Alpha Keep" not in changed_names
    # ... and unchanged must count it (>=1 stable node).
    assert isinstance(d["unchanged"], int) and d["unchanged"] >= 1

    # summary is a compact human string mentioning the three counts.
    assert isinstance(d["summary"], str) and d["summary"]


def test_diff_snapshot_default_after_takes_fresh_snapshot(live_session):
    """diff_snapshot(before) with no `after` must internally take a fresh
    snapshot() and diff against it."""
    from browser_skill import diff_snapshot, js, snapshot

    js(_BASE)
    before = snapshot()

    # Mutate, then call with the single-arg form.
    js(_MUTATE)
    d = diff_snapshot(before)  # after=None -> fresh snapshot internally

    assert isinstance(d, dict)
    assert "Delta Added" in _names(d["added"])
    assert "Beta Remove" in _names(d["removed"])
    assert "Gamma Toggle" in _names(d["changed"])
