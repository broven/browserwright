"""Unit tests for the per-worktree e2e port derivation (issue #44 A).

No Chrome, no daemon — these run in the fast gate. They pin the properties
that make concurrent worktrees safe:

  - deterministic: same root -> same block, on every machine and run;
  - distinct roots -> distinct blocks (the whole point);
  - block is contiguous and inside 30000-48999, clear of production
    (19988/19989/19990), the old fixed e2e block (29989-29994, still used by
    worktrees on older checkouts) and macOS's ephemeral range (49152+);
  - the CLI (`run.sh` executes this module) agrees with the import path.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ._e2e_ports import PORT_SLOTS, RANGE_LO, RANGE_N, e2e_ports, worktree_root

#: Ports the derivation must NEVER land on: production relay/facade +
#: playwriter/opencli neighbors + the old fixed e2e block.
_FORBIDDEN = {19825, 19988, 19989, 19990, *range(29989, 29995)}


def test_deterministic_same_root():
    root = Path("/worktrees/example")
    assert e2e_ports(root) == e2e_ports(root)


def test_distinct_roots_get_distinct_blocks():
    roots = [Path(f"/worktrees/wt-{i}") for i in range(32)]
    bases = {e2e_ports(r)["ext"] for r in roots}
    assert len(bases) == len(roots)  # no collision in this sample
    # The six test roots we picked are pairwise disjoint across the whole block.
    blocks = [sorted(e2e_ports(r).values()) for r in roots]
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            assert not set(a) & set(b)


def test_default_root_is_this_checkout():
    assert worktree_root() == Path(__file__).resolve().parents[3]


def test_block_shape_and_range():
    ports = e2e_ports(Path("/worktrees/example"))
    # One port per slot, contiguous, in range.
    assert sorted(ports.values()) == list(range(ports["ext"], ports["ext"] + len(PORT_SLOTS)))
    assert RANGE_LO <= ports["ext"] <= RANGE_LO + RANGE_N - len(PORT_SLOTS)
    for key, _var, off in PORT_SLOTS:
        assert ports[key] == ports["ext"] + off
    assert len(set(ports.values())) == len(PORT_SLOTS)


def test_never_collides_with_production_or_old_fixed_block():
    for root in (Path("/worktrees/example"), Path("/main/checkout")):
        for port in e2e_ports(root).values():
            assert port not in _FORBIDDEN, f"{root} -> {port} forbidden"


def test_env_override_pins_the_base(monkeypatch):
    monkeypatch.setenv("BW_E2E_PORT_BASE", "30123")
    ports = e2e_ports(Path("/worktrees/example"))
    assert ports["ext"] == 30123
    assert ports["rdp"] == 30124


def test_cli_output_matches_import(monkeypatch):
    """run.sh `eval`s the module's stdout; it must carry exactly the same
    numbers the fixtures import, with stable var names."""
    out = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "_e2e_ports.py")],
        capture_output=True, text=True, check=True,
    )
    vars_ = dict(line.split("=", 1) for line in out.stdout.splitlines())
    assert vars_ == {
        var: str(e2e_ports()[key])
        for key, var, _off in PORT_SLOTS
    }
    # No stray stdout noise (run.sh evals this — a warning would break it).
    assert out.stdout.strip().splitlines() == [f"{v}={vars_[v]}" for v in vars_]
