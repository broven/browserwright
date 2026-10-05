/**
 * @browserwright/pi — `bw_web_fetch` and `bw_web_search` for pi.
 *
 * Each tool is one browserwright CLI call (`markdown` / `search`); this file
 * only declares the tools and relays what the CLI says. The browser is the
 * user's own Chrome, or the remote one named by `BW_REMOTE_CDP` — never both,
 * and nothing falls back to anything else.
 *
 * Tool names are `bw_`-prefixed (not bare `web_fetch`/`web_search`) because
 * providers reserve generic tool names: grok rejects a custom function named
 * `web_search` with a 400.
 */

import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { browserArgs, browserLabel, runBrowserwright } from "./browserwright.ts";

/** What fetch prints before cutting; the full text stays in the file the CLI names. */
const FETCH_MAX_CHARS = 50_000;

/**
 * The call deadline for the browserwright call behind a tool (ADR-0014): the
 * same knob as `browserwright -e --timeout`.
 */
const CALL_TIMEOUT_PARAM = Type.Optional(
	Type.Number({
		minimum: 1,
		description:
			"Call deadline in seconds for the browserwright call (default 90). Raise it for a page " +
			"known to be slow; when it runs out the call fails with DeadlineExceeded.",
	}),
);

/**
 * Failures are THROWN: `AgentToolResult` has no `isError`, and pi only marks a
 * call failed when `execute` throws. A thrown `BrowserwrightFailure` carries
 * the CLI's own sentence.
 */
export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "bw_web_fetch",
		label: "Fetch Web Page",
		description:
			"Fetch a URL in a real browser and return its main content as Markdown (absolute links, " +
			`JavaScript rendered). Runs in ${browserLabel()}. HTML only: other content types are refused. ` +
			`Output over ${FETCH_MAX_CHARS} characters is cut on a line boundary and the full text written to a file whose path is given.`,
		promptSnippet: "Fetch a URL as Markdown through a real browser",
		promptGuidelines: [
			"Prefer `bw_web_fetch` over curl or a shell HTTP client for reading web pages — it renders JavaScript.",
		],
		parameters: Type.Object({
			url: Type.String({ description: "HTTP(S) URL to fetch" }),
			timeout: CALL_TIMEOUT_PARAM,
		}),
		async execute(_toolCallId, params, signal) {
			const url = /^https?:\/\//i.test(params.url) ? params.url : `https://${params.url}`;
			const { stdout, stderr } = await runBrowserwright(
				["markdown", url, `--max-chars=${FETCH_MAX_CHARS}`, "--name=pi-webfetch", ...browserArgs()],
				{ signal, callTimeoutS: params.timeout },
			);
			// The CLI announces a cut (and where the rest is) on stderr.
			const cut = stderr.split("\n").find((line) => line.startsWith("[markdown] truncated"));
			const header = [url, ...(cut ? [cut.replace(/^\[markdown\] /, "")] : [])].join("\n");
			return {
				content: [{ type: "text" as const, text: `${header}\n\n${stdout}` }],
				details: { url, chars: stdout.length, truncated: Boolean(cut) },
			};
		},
	});

	pi.registerTool({
		name: "bw_web_search",
		label: "Search the Web",
		description:
			"Search the web (Google, in a real browser) and return ranked results as title, URL, date and snippet, " +
			"plus the engine's AI Overview, knowledge panel, 'people also ask' and related searches when the query " +
			`triggered them. Runs in ${browserLabel()}. Returns links, never page bodies — call bw_web_fetch on the ones worth reading.`,
		promptSnippet: "Search the web and get back ranked links",
		promptGuidelines: [
			"`bw_web_search` returns links, not page contents. After searching, call `bw_web_fetch` on the one or two " +
				"results actually worth reading rather than fetching all of them.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "What to search for" }),
			timeout: CALL_TIMEOUT_PARAM,
		}),
		async execute(_toolCallId, params, signal) {
			const query = params.query.trim();
			if (!query) throw new Error("bw_web_search needs a non-empty query");
			const { stdout } = await runBrowserwright(["search", query, "--name=pi-websearch", ...browserArgs()], {
				signal,
				callTimeoutS: params.timeout,
			});
			return {
				content: [{ type: "text" as const, text: stdout }],
				details: { query },
			};
		},
	});
}
