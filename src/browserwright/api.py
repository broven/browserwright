"""Canonical primitive surface for ``from browserwright import *``.

The inline / repl / task entry points all assemble their exec globals from
this module. Keeping the list in one place means an agent who imports
``browserwright`` directly from a saved task gets the same names the REPL
gave them.

v0.5.1 (F-4 catch-up): EXPORTS grew from 23 → 36 with 13 primitives ported
from ``browser-harness`` (input / waiting / iframe / http) plus three
Layer-3 re-exports. design.md §A.2 footnotes 3 still-deferred primitives
for v0.6.
"""
from .errors import (
    AuthWall,
    BrowserwrightError,
    Captcha,
    CDPError,
    DaemonUnavailable,
    ElementNotFound,
    NeedsUserConfirm,
    NetworkError,
    PageLoadFailed,
)
from .multitask import run_tasks_concurrent
from .primitives import (
    attach_active,
    attach_readonly,
    bootstrap_site,
    capture_screenshot,
    cdp,
    click_at_xy,
    close_tab,
    current_page,
    current_tab,
    describe_page,
    diff_snapshot,
    dispatch_key,
    drain_events,
    ensure_real_tab,
    fill_input,
    goto_url,
    http_get,
    iframe_target,
    js,
    list_site_skills,
    list_tabs,
    load_site_skill,
    memory_read,
    new_tab,
    open_background,
    page_info,
    reload,
    press_key,
    remember,
    remember_global,
    remember_preference,
    run_task,
    scroll,
    snapshot,
    switch_tab,
    type_text,
    upload_file,
    wait,
    wait_for_element,
    wait_for_load,
    wait_for_network_idle,
)

EXPORTS = [
    # navigation / tabs
    "goto_url", "new_tab", "reload", "switch_tab", "list_tabs", "current_tab",
    "current_page", "ensure_real_tab", "iframe_target",
    "attach_readonly", "attach_active",
    "open_background", "close_tab",
    # input
    "click_at_xy", "type_text", "press_key", "fill_input", "scroll",
    "dispatch_key", "upload_file",
    # JS + visual + raw CDP
    "js", "cdp", "page_info", "capture_screenshot",
    # perception (read-only: what can I act on + where / what paints this page)
    "snapshot", "describe_page",
    # verification (did my action change the page? — diff two snapshots)
    "diff_snapshot",
    # waiting + events
    "wait", "wait_for_load", "wait_for_element", "wait_for_network_idle",
    "drain_events",
    # http (escape hatch — no browser)
    "http_get",
    # memory + site
    "bootstrap_site", "remember", "remember_global", "remember_preference",
    "memory_read",
    # task / fan-out
    "list_site_skills", "load_site_skill", "run_task",
    "run_tasks_concurrent",
    # errors
    "BrowserwrightError", "PageLoadFailed", "ElementNotFound", "AuthWall",
    "Captcha", "NetworkError", "DaemonUnavailable", "CDPError",
    "NeedsUserConfirm",
]

__all__ = EXPORTS
