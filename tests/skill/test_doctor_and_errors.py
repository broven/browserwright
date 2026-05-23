"""S5 gate: actionable errors (A3) + doctor check table (A4).

Deterministic, no live browser. A3 asserts every error can carry a
concrete recovery action (``fix``) surfaced in its serialized form,
modeled on the existing ``NeedsUserConfirm.proposal``. A4 asserts
``doctor`` returns a ``{status, message, fix}`` check table where every
``fail`` check carries a non-empty ``fix``, and the CLI exits nonzero
when any check fails.

Assertions are by SHAPE (field present + non-empty), never by exact
wording — anti-overfit.
"""
from __future__ import annotations

import json

import pytest

from browserwright import cli
from browserwright.daemon_client import DaemonClient
from browserwright.errors import (
    BrowserwrightError,
    CDPError,
    DaemonUnavailable,
    NeedsUserConfirm,
    NoSession,
    serialize,
)


# ---- A3: errors carry a concrete next-action / fix --------------------


def test_base_error_accepts_fix():
    """The convention is generic: ANY BrowserwrightError can carry a fix."""
    e = BrowserwrightError("something broke", fix="run `browserwright doctor`")
    assert e.fix
    assert e.fix in str(e)  # surfaced in __str__


def test_explicit_fix_surfaces_in_serialize():
    e = DaemonUnavailable("connection refused", fix="start it: `browserwright-daemon serve`")
    d = serialize(e)
    assert d.get("fix")
    assert isinstance(d["fix"], str) and d["fix"].strip()


def test_high_value_errors_have_default_fix():
    """The common error sites an agent actually hits ship a default fix
    even when the raise site doesn't pass one — so a bare raise is still
    actionable."""
    for e in (
        DaemonUnavailable("boom"),
        NoSession(),
        CDPError(method="Page.navigate", cdp_message="-32601 unknown"),
    ):
        d = serialize(e)
        assert d.get("fix"), f"{type(e).__name__} should carry a default fix"
        assert d["fix"].strip()


def test_fix_is_json_serializable():
    e = CDPError(method="X", cdp_message="y", fix="do z")
    blob = json.dumps(serialize(e))
    assert "fix" in json.loads(blob)


def test_needs_user_confirm_proposal_still_works():
    """The model we generalize from must keep working unchanged."""
    e = NeedsUserConfirm(what="store preference", proposal={"k": "v"})
    d = serialize(e)
    assert d["proposal"] == {"k": "v"}


# ---- A4: doctor returns a {status, message, fix} check table ----------


def _checks(info: dict) -> list:
    assert "checks" in info, "doctor must expose a checks table"
    return info["checks"]


def test_doctor_checks_shape_and_fail_carries_fix(monkeypatch):
    """Under a broken condition (daemon unreachable) doctor must produce a
    fail check, and EVERY check must have {status, message, fix} keys, and
    EVERY fail check's fix must be non-empty (the discipline)."""
    # Known-broken: synthetic doctor blob like a missing daemon binary.
    monkeypatch.setattr(
        DaemonClient,
        "doctor",
        lambda self: {
            "schema_version": 1,
            "backends": [],
            "error": "browserwright-daemon: not found on PATH",
            "skill_synthetic": True,
        },
    )
    info = DaemonClient().doctor_checks()
    checks = _checks(info)
    assert checks, "expected at least one check"
    for c in checks:
        assert set(("status", "message", "fix")) <= set(c.keys())
        assert c["status"] in ("pass", "warn", "fail")
    fails = [c for c in checks if c["status"] == "fail"]
    assert fails, "broken daemon should yield at least one fail"
    for c in fails:
        assert c["fix"] and c["fix"].strip(), "every fail must carry a fix"


def test_doctor_checks_pass_when_backend_available(monkeypatch):
    # schema_version=2 mirrors the CURRENT daemon contract (daemon v0.5.3). A
    # prior version of this test used 1, which masked a false-positive warn.
    monkeypatch.setattr(
        DaemonClient,
        "doctor",
        lambda self: {
            "schema_version": 2,
            "backends": [
                {"name": "extension", "available": True, "ux_cost": "none",
                 "ws_url": "ws://127.0.0.1:1/x"},
            ],
        },
    )
    info = DaemonClient().doctor_checks()
    checks = _checks(info)
    # All checks still shaped correctly even on the happy path.
    for c in checks:
        assert set(("status", "message", "fix")) <= set(c.keys())
    assert any(c["status"] == "pass" for c in checks)
    # Regression guard: the current daemon schema must NOT trip a drift warn.
    schema = next((c for c in checks if c["name"] == "daemon_schema"), None)
    assert schema is not None and schema["status"] == "pass", schema


def test_cmd_doctor_json_emits_checks_and_exits_nonzero_on_fail(monkeypatch, capsys):
    monkeypatch.setattr(
        DaemonClient,
        "doctor",
        lambda self: {
            "schema_version": 1,
            "backends": [],
            "error": "browserwright-daemon: not found on PATH",
            "skill_synthetic": True,
        },
    )
    rc = cli._cmd_doctor(["--json"])
    out = capsys.readouterr().out
    info = json.loads(out)
    assert "checks" in info
    assert any(c["status"] == "fail" for c in info["checks"])
    for c in info["checks"]:
        assert set(("status", "message", "fix")) <= set(c.keys())
    assert rc != 0  # CI-style: nonzero exit when something failed


def test_cmd_doctor_human_prints_fix_for_fail(monkeypatch, capsys):
    monkeypatch.setattr(
        DaemonClient,
        "doctor",
        lambda self: {
            "schema_version": 1,
            "backends": [],
            "error": "browserwright-daemon: not found on PATH",
            "skill_synthetic": True,
        },
    )
    rc = cli._cmd_doctor([])
    out = capsys.readouterr().out
    assert rc != 0
    # The human view must surface a recovery hint for failures (shape, not
    # exact wording): the literal "fix" label appears near a failed check.
    assert "fix" in out.lower()


def test_cmd_doctor_exits_zero_when_all_pass(monkeypatch, capsys):
    monkeypatch.setattr(
        DaemonClient,
        "doctor",
        lambda self: {
            "schema_version": 1,
            "backends": [
                {"name": "extension", "available": True, "ux_cost": "none",
                 "ws_url": "ws://127.0.0.1:1/x"},
            ],
        },
    )
    rc = cli._cmd_doctor(["--json"])
    info = json.loads(capsys.readouterr().out)
    assert all(c["status"] != "fail" for c in info["checks"])
    assert rc == 0
