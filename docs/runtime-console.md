# Console capture on the extension backend

The extension CDP surface keeps Chrome's Runtime event subscription off by
default. Subscribing makes Chrome synchronously build console object previews;
large `console.table()` calls can slow down ordinary page execution even when
the agent never asks for console messages.

`ExtensionFacadeBridge` supplies Playwright with real execution-context IDs
through `extension_contexts.py`. It resolves native Document handles with
`DOM.getDocument` / `DOM.getFrameOwner` / `DOM.describeNode` / `DOM.resolveNode`,
validates the context component of Chrome's V8 object ID, and releases each
handle. This works for scriptless local frames without a page preload. An
unfamiliar object ID format fails explicitly; discovery never enables Runtime
events as a fallback. Explicit isolated-world creation supplies each utility
world's name and context ID.

Frame navigation and removal retire old contexts. Discovery retries provisional
documents and discards results from superseded navigations. Native Runtime
evaluation commands remain available, so `page.evaluate()` reads the page's
own globals as usual. Debugger stays disabled during ordinary automation:
even skipped pauses can make repeated `debugger` statements expensive.
The legacy Console domain supplies text messages without Runtime's object
previews. Startup text waits for its default realm to be published, so navigation
does not drop messages while context discovery is in flight. Translating those messages preserves Playwright's internal debug
marker used by `page.set_content()` to coordinate document lifecycle events.
Full console arguments and exception events require the capture opt-in below.

Set `BW_CAPTURE_CONSOLE=1` in the daemon environment before starting the daemon
to retain native Runtime events, including Playwright console and exception
events. An explicit CDP session's `Runtime.enable` also subscribes normally;
its `Runtime.disable` turns the subscription off again. Console capture carries
Chrome's normal serialization cost.

Repeatable verification: `uv run pytest
tests/daemon/e2e/test_runtime_console_cost.py -v`. Evidence is saved in
`tests/daemon/e2e/_artifacts/runtime-console-cost.json`: console timing with
capture off/on, main-world globals across navigation, and scriptless iframe
evaluation.

Extension-only verification without a Chrome debugging port: `uv run pytest
tests/daemon/e2e/test_debugger_pause_extension.py -v`. Its artifact
`tests/daemon/e2e/_artifacts/debugger-pause-extension.json` records native pause
events, repeated debugger-statement timing, renderer swaps, scriptless child
realms, startup console text and levels, and executor rebind.
