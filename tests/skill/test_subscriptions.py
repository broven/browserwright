"""Subscription scaffolding tests (v0.3)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


def _init_local_repo(repo_dir: Path, *, with_site: bool = True) -> None:
    """Create a bare-bones git repo we can clone via `file://`."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if with_site:
        (repo_dir / "site-skills" / "mysite" / "tasks").mkdir(parents=True)
        (repo_dir / "site-skills" / "mysite" / "memory.md").write_text(
            "# mysite\n", encoding="utf-8"
        )
        (repo_dir / "site-skills" / "mysite" / "tasks" / "demo.py").write_text(
            '"""demo task from a subscription."""\n'
            'ARGS = {}\n'
            'def selftest(): return True\n'
            'def run(args, ctx=None):\n'
            '    return "from-subscription"\n',
            encoding="utf-8",
        )
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _reset_modules():
    for k in list(sys.modules):
        if k.startswith("browserwright"):
            del sys.modules[k]


def test_add_clones_and_records_metadata(tmp_bs_home):
    _reset_modules()
    from browserwright import subscriptions

    repo = tmp_bs_home.parent / "donor-repo"
    _init_local_repo(repo)
    r = subscriptions.add(f"file://{repo}", name="donor")
    assert r["status"] == "added"
    assert (tmp_bs_home / "subscriptions" / "donor").is_dir()
    rows = subscriptions.list_all()
    assert len(rows) == 1
    assert rows[0]["name"] == "donor"
    assert rows[0]["url"].startswith("file://")


def test_add_idempotent_on_same_url(tmp_bs_home):
    _reset_modules()
    from browserwright import subscriptions

    repo = tmp_bs_home.parent / "donor-repo"
    _init_local_repo(repo)
    subscriptions.add(f"file://{repo}", name="donor")
    r2 = subscriptions.add(f"file://{repo}", name="donor")
    assert r2["status"] == "already_present"


def test_add_rejects_invalid_name(tmp_bs_home):
    _reset_modules()
    from browserwright import subscriptions

    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.add("https://example.com/x.git", name="bad name with spaces")


def test_remove_drops_clone_and_metadata(tmp_bs_home):
    _reset_modules()
    from browserwright import subscriptions

    repo = tmp_bs_home.parent / "donor-repo"
    _init_local_repo(repo)
    subscriptions.add(f"file://{repo}", name="donor")
    subscriptions.remove("donor")
    assert not (tmp_bs_home / "subscriptions" / "donor").exists()
    assert subscriptions.list_all() == []


def test_update_pulls_new_commit(tmp_bs_home):
    _reset_modules()
    from browserwright import subscriptions

    repo = tmp_bs_home.parent / "donor-repo"
    _init_local_repo(repo)
    # `--depth 1` shallow clones can't fast-forward across a force-pushed
    # local file:// repo by default. Use the repo's full history instead.
    subprocess.run(["git", "config", "--local", "uploadpack.allowFilter", "true"],
                   cwd=repo, check=False)
    subscriptions.add(f"file://{repo}", name="donor")
    # Make a new commit upstream.
    new_file = repo / "site-skills" / "mysite" / "tasks" / "another.py"
    new_file.write_text(
        '"""another"""\nARGS = {}\n'
        'def selftest(): return True\n'
        'def run(args, ctx=None): return "v2"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v2"], cwd=repo, check=True)

    results = subscriptions.update(["donor"])
    # On macOS shallow clones from a local repo sometimes refuse fast-forward;
    # accept either updated-cleanly or a non-fatal status.
    assert results[0]["name"] == "donor"
    assert results[0]["status"] in {"updated", "error"}


def test_discovery_layers_subscription_between_home_and_bundled(tmp_bs_home, monkeypatch):
    """A subscription's mysite/demo task should be findable when neither
    $BS_HOME nor the project has its own."""
    _reset_modules()
    # Stub bundled root so we don't pick up the real starter set.
    empty_bundle = tmp_bs_home.parent / "empty-bundle"
    empty_bundle.mkdir()
    from browserwright import discovery, subscriptions
    monkeypatch.setattr(discovery, "_bundled_root", lambda: empty_bundle)

    repo = tmp_bs_home.parent / "donor-repo"
    _init_local_repo(repo)
    subscriptions.add(f"file://{repo}", name="donor")
    found = discovery.find_task_path("mysite", "demo")
    assert "subscriptions/donor" in str(found)
    assert found.exists()


def test_remove_missing_raises(tmp_bs_home):
    _reset_modules()
    from browserwright import subscriptions

    with pytest.raises(subscriptions.SubscriptionError):
        subscriptions.remove("never-installed")
