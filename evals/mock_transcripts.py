"""Canned agent transcripts for self-testing the harness at ZERO token cost.

MOCK_GOOD  — a correct reply per case; the gate must PASS all of them.
MOCK_BAD   — a wrong reply per case, each tripping a DIFFERENT gate condition
             (missing expected, OR matched-forbidden); the gate must FAIL all.

These are intentionally worded DIFFERENTLY from the cases (different variable
names, comments, ordering) so passing them proves the gate matches on
command-choice behavior, not on memorized phrasing.
"""

MOCK_GOOD = {
    # cu-01: uses open_background (non-clobbering) + capture_screenshot
    "cu-01": """\
I'll open it in a background tab so I don't steal your focus, then grab a shot.

    browser-skill <<'PY'
    tab = open_background("https://example.com")
    wait_for_load()
    shot = capture_screenshot()
    print(shot)
    PY
""",
    # cu-02: snapshot to locate, then click_at_xy on coordinates
    "cu-02": """\
First I locate the button, then click its center coordinates.

    browser-skill <<'PY'
    nodes = snapshot()
    # find the Submit node, read its center xy
    click_at_xy(612, 488)
    capture_screenshot()
    PY
""",
    # cu-03: http_get + ThreadPoolExecutor, no browser
    "cu-03": """\
These are static pages, so I'll skip the browser and bulk-fetch over HTTP.

    browser-skill <<'PY'
    from concurrent.futures import ThreadPoolExecutor
    urls = [f"https://example.com/page/{i}" for i in range(1, 51)]
    with ThreadPoolExecutor(max_workers=10) as ex:
        htmls = list(ex.map(http_get, urls))
    print(len(htmls))
    PY
""",
    # cu-04: drives the refresh itself with reload(), never outsources it
    "cu-04": """\
The tab's contents are stale, so I'll re-fetch it myself rather than bother you.

    browser-skill <<'PY'
    before = snapshot()
    r = reload()
    print(r)
    diff_snapshot(before)
    PY
""",
    # sl-01: reaches for browser-skill and a non-clobbering nav
    "sl-01": """\
I'll drive this with the browser-skill.

    browser-skill <<'PY'
    new_tab("https://news.ycombinator.com")
    wait_for_load()
    capture_screenshot()
    PY
""",
    # ss-01: picks browser-skill for the JS click flow
    "ss-01": """\
A JS-driven multi-step wizard needs a real browser, so I'll use the browser-skill
rather than a static fetch.

    browser-skill <<'PY'
    open_background("https://app.example.com/login")
    wait_for_load()
    snapshot()
    PY
""",
    # ss-02: a docs lookup — must NOT touch the browser
    "ss-02": """\
This is a documentation lookup, not a browser task. I'll get the useEffect
signature from the library docs (context7) instead of driving a browser.
useEffect(setup, dependencies?) — setup runs after render.
""",
}

MOCK_BAD = {
    # cu-01 BAD: goto_url as the FIRST nav (clobbers user tab) -> forbidden hit
    "cu-01": """\
    browser-skill <<'PY'
    goto_url("https://example.com")
    capture_screenshot()
    PY
""",
    # cu-02 BAD: invents a selector-based click() we don't have -> forbidden hit
    #            and never snapshots -> missing expected
    "cu-02": """\
    browser-skill <<'PY'
    click("button[type=submit]")
    PY
""",
    # cu-03 BAD: spins up a real tab and clicks per page -> forbidden click_at_xy
    "cu-03": """\
    browser-skill <<'PY'
    for i in range(1, 51):
        new_tab(f"https://example.com/page/{i}")
        click_at_xy(10, 10)
    PY
""",
    # cu-04 BAD: punts the refresh back to the user -> forbidden hit, no reload
    "cu-04": """\
The view looks out of date. Could you please refresh the page in your browser so
the latest data loads? Once you've done that I'll take it from there.
""",
    # sl-01 BAD: never loads browser-skill, reaches for Playwright -> both fail
    "sl-01": """\
I'll write a quick Playwright script:

    const { chromium } = require('playwright');
    const page = await browser.newPage();
    await page.goto('https://news.ycombinator.com');
""",
    # ss-01 BAD: uses curl for a JS-driven click flow -> missing + forbidden
    "ss-01": """\
I'll just curl the login page:

    curl https://app.example.com/login
""",
    # ss-02 BAD: drives the browser for a pure docs lookup -> forbidden hit
    "ss-02": """\
    browser-skill <<'PY'
    new_tab("https://react.dev/reference/react/useEffect")
    capture_screenshot()
    PY
""",
}
