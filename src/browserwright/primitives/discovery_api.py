"""Discovery / Layer-3 task primitives surfaced into the REPL namespace.

These are thin re-exports + tiny convenience wrappers around
``browserwright.discovery`` and ``browserwright.task_runner`` so an agent
typing ``list_site_skills()`` / ``run_task("github.com/list_issues")`` /
``load_site_skill("github.com")`` from the REPL or inline execution
doesn't get a NameError. Spec §A.2 v0.5.1 (F-4 catch-up).
"""
from __future__ import annotations

import importlib.util
from typing import Any, Optional

from ..discovery import find_task_path, list_tasks
from ..memory.site_mem import host_stem
from ..task_runner import run_task as _run_task


def list_site_skills(*, site: Optional[str] = None,
                     query: Optional[str] = None) -> list[dict]:
    """List bundled + user-installed tasks (alias of CLI ``list-tasks``).

    Returns dicts with ``site``, ``name``, ``desc``, ``path`` and the
    discovery scoring fields. ``site`` filters by stem (eTLD+1 or
    legacy alias); ``query`` does substring scoring against task
    metadata.
    """
    return list_tasks(site=site, query=query)


def load_site_skill(site: str, name: Optional[str] = None) -> Any:
    """Import a site-skill task module so its ``run()``, ``ARGS``,
    ``OUTPUT_SCHEMA``, etc. are reachable as attributes.

    Two shapes:
      - ``load_site_skill("github.com/list_issues")`` (slash form) →
        load that specific task.
      - ``load_site_skill("github.com", "list_issues")`` → same, split.

    Pure module import; no ``run()`` invocation. Use ``run_task()`` to
    actually execute. Path is resolved via ``find_task_path`` so the
    eTLD+1 stem fallback applies (Bug 1 v0.3.1).
    """
    if name is None and "/" in site:
        site, name = site.split("/", 1)
    if name is None:
        raise ValueError(
            "load_site_skill: missing task name. Pass "
            "'<site>/<name>' or two positional args."
        )
    path = find_task_path(host_stem(site), name)
    mod_name = f"browserwright_task_{host_stem(site).replace('.', '_')}_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build importlib spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_task(site: str, name: Optional[str] = None, **kwargs) -> Any:
    """Execute a site-skill's ``run(args, ctx=None)`` and return its
    value. Two argument shapes:

      - ``run_task("github.com/list_issues", state="open")`` (slash form)
      - ``run_task("github.com", "list_issues", state="open")`` (split)

    Re-exports ``browserwright.task_runner.run_task`` so agents calling
    this through the REPL namespace get the same isolation semantics as
    the CLI ``task`` subcommand.
    """
    if name is None and "/" in site:
        site, name = site.split("/", 1)
    if name is None:
        raise ValueError(
            "run_task: missing task name. Pass '<site>/<name>' or two "
            "positional args."
        )
    return _run_task(site, name, **kwargs)
