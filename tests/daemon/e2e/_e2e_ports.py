"""Per-worktree e2e port allocation (issue #44 A).

The e2e harness needs seven TCP ports. They used to be fixed literals
(29989-29994), which made e2e single-tenant per machine: every worktree's run
bound the same numbers, so only one worktree could run at a time and a stray
daemon blocked everyone. Now every port is derived from a **stable hash of the
worktree root path** into a port range, so sibling worktrees on one machine get
disjoint blocks with overwhelming probability — and a rare hash collision is
caught loudly by the fixtures' `_port_free` guard and `run.sh`'s ownership
check, never silently.

Single source of truth: `run.sh` executes this module (NAME=VALUE on stdout)
and the pytest fixtures import :func:`e2e_ports`, so the runner and the tests
can never disagree about which ports this worktree owns.

Port range 30000-48999 (19000 blocks). Deliberately clear of:
  - production: playwriter 19988, relay 19989, facade 19990, opencli 19825;
  - the old fixed e2e block 29989-29994 (worktrees on older checkouts still
    use those — the derivation must not collide with a sibling's old run);
  - macOS's ephemeral range (49152+), which the OS hands to outgoing sockets.

Debug override: `BW_E2E_PORT_BASE` pins the block base for both `run.sh` and
the fixtures (they read the same env), e.g. to reproduce a reported collision.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

#: (logical key, bash var name, offset within the block). Offsets are stable
#: API — never renumber; the mapping is what keeps `run.sh`'s var names and
#: the fixtures' constants aligned.
PORT_SLOTS: tuple[tuple[str, str, int], ...] = (
    ("ext",               "TEST_EXT_PORT",               0),  # extension relay
    ("cdp",               "TEST_CDP_PORT",               1),  # cdp Chrome debug
    ("facade_l1",         "TEST_FACADE_L1_PORT",         2),  # l1 cdp facade daemon
    ("facade_l1_ext",     "TEST_FACADE_L1_EXT_PORT",     3),  # l1 ext facade daemon
    ("facade_ext",        "TEST_FACADE_EXT_PORT",        4),  # session ext daemon facade
    ("facade_cdp",        "TEST_FACADE_CDP_PORT",        5),  # session cdp daemon facade
    ("facade_autofacade", "TEST_FACADE_AUTOFACADE_PORT", 6),  # cdp auto-facade daemon
)

RANGE_LO = 30000
RANGE_N = 19000  # 30000..48999 inclusive — see module docstring


def worktree_root() -> Path:
    """The checkout root this module lives in (tests/daemon/e2e -> up 3)."""
    return Path(__file__).resolve().parents[3]


def e2e_ports(root: Path | None = None) -> dict[str, int]:
    """Return this worktree's e2e port block, keyed by the PORT_SLOTS keys.

    ``root`` defaults to this checkout's root (derived from ``__file__``), so
    pytest and `run.sh` agree without passing anything. Deterministic: same
    root → same ports, on every machine and every run. ``BW_E2E_PORT_BASE``
    overrides the hash (debugging / collision reproduction only).
    """
    root = (root or worktree_root()).resolve()
    override = os.environ.get("BW_E2E_PORT_BASE")
    if override is not None:
        base = int(override)
    else:
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
        base = RANGE_LO + (int(digest[:8], 16) % RANGE_N)
    return {key: base + off for key, _var, off in PORT_SLOTS}


def main() -> None:
    """CLI for `run.sh`: print ``VAR=port`` lines (shell-evaluable).

    stdout carries ONLY ``VAR=port`` lines so `run.sh` can `eval` it; anything
    human-readable goes to stderr.
    """
    ports = e2e_ports()
    for _key, var, _off in PORT_SLOTS:
        print(f"{var}={ports[_key]}")


if __name__ == "__main__":
    main()
