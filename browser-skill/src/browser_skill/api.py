"""Canonical primitive surface for ``from browser_skill import *``.

The inline / repl / task entry points all assemble their exec globals from
this module. Keeping the list in one place means an agent who imports
``browser_skill`` directly from a saved task gets the same names the REPL
gave them.

v0.5.1 (F-4 catch-up): EXPORTS grew from 23 → 36 with 13 primitives ported
from ``browser-harness`` (input / waiting / iframe / http) plus three
Layer-3 re-exports. design.md §A.2 footnotes 3 still-deferred primitives
for v0.6.
"""
from .errors import (
    AuthWall,
    BrowserSkillError,
    Captcha,
    CDPError,
    DaemonUnavailable,
    ElementNotFound,
    NeedsUserConfirm,
    NetworkError,
    PageLoadFailed,
    SiteDrift,
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
    press_key,
    propose_solidify,
    remember,
    remember_global,
    remember_preference,
    run_task,
    scroll,
    solidify,
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
    "goto_url", "new_tab", "switch_tab", "list_tabs", "current_tab",
    "current_page", "ensure_real_tab", "iframe_target",
    "attach_readonly", "attach_active",
    "open_background", "close_tab",
    # input
    "click_at_xy", "type_text", "press_key", "fill_input", "scroll",
    "dispatch_key", "upload_file",
    # JS + visual + raw CDP
    "js", "cdp", "page_info", "capture_screenshot",
    # waiting + events
    "wait", "wait_for_load", "wait_for_element", "wait_for_network_idle",
    "drain_events",
    # http (escape hatch — no browser)
    "http_get",
    # memory + site
    "bootstrap_site", "remember", "remember_global", "remember_preference",
    "memory_read",
    # solidify / task / fan-out
    "propose_solidify", "solidify",
    "list_site_skills", "load_site_skill", "run_task",
    "run_tasks_concurrent",
    # errors
    "BrowserSkillError", "PageLoadFailed", "ElementNotFound", "AuthWall",
    "Captcha", "NetworkError", "DaemonUnavailable", "SiteDrift", "CDPError",
    "NeedsUserConfirm",
]

__all__ = EXPORTS
