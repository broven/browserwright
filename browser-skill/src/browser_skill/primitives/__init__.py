"""v0.5.1 primitive surface (spec §A.2).

This module is what the REPL / inline / task entry points pull into
their exec globals. Keep it boring — no decorators, no metaprogramming —
so the agent gets stable, greppable names.

v0.5.1 (F-4 catch-up) added 13 primitives previously documented but not
re-exported: type_text / press_key / scroll / fill_input / dispatch_key
/ upload_file / wait_for_element / wait_for_network_idle / drain_events
/ ensure_real_tab / iframe_target / http_get plus three Layer-3 helpers
(list_site_skills / load_site_skill / run_task). Three primitives remain
deferred to v0.6 with explicit footnotes in design.md §A.2:
handle_dialog, try_recover_from_drift, plus the broader Layer-3 drift
recovery scaffold.
"""
from .discovery_api import (  # noqa: F401
    list_site_skills,
    load_site_skill,
    run_task,
)
from .http import http_get  # noqa: F401
from .inspect import (  # noqa: F401
    capture_screenshot,
    cdp,
    page_info,
)
from .interact import (  # noqa: F401
    click_at_xy,
    dispatch_key,
    drain_events,
    fill_input,
    js,
    press_key,
    scroll,
    type_text,
    upload_file,
    wait_for_element,
    wait_for_network_idle,
)
from .page import (  # noqa: F401
    attach_readonly,
    current_page,
    current_tab,
    ensure_real_tab,
    goto_url,
    iframe_target,
    list_tabs,
    new_tab,
    switch_tab,
    wait,
    wait_for_load,
)
from .site import (  # noqa: F401
    bootstrap_site,
    memory_read,
    propose_solidify,
    remember,
    remember_global,
    remember_preference,
    solidify,
)
