/**
 * Fallback-engine and formatting tests. The executor is injected, so nothing
 * here touches the network or a browser.
 *
 * Provider names below are fictional fixtures, not the rungs this package
 * ships — the engine is what is under test, and it must not care which
 * providers happen to be declared.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { runChain, type Executor } from "./chain.ts";
import { formatChain, renderFailure, renderResults, renderSuccess } from "./format.ts";
import { inspectSearch, inspectText } from "./predicates.ts";
import type { PiConfig, Provider, SearchPayload, SearchResult } from "./types.ts";

const config: PiConfig = {
	order: { fetch: ["remote", "hosted", "browser", "raw"], search: ["finder", "backup"] },
	defaultFailWhen: { minChars: 0, minResults: 0, matches: ["enable javascript"] },
	timeoutMs: 1000,
	maxBytes: 200,
	maxLines: 50,
};

const providers = new Map<string, Provider>([
	["remote", { name: "remote", kind: "http", url: "https://r/{url}", returns: "markdown" } as Provider],
	["hosted", { name: "hosted", kind: "http", url: "https://h", returns: "markdown" } as Provider],
	["browser", { name: "browser", kind: "command", command: ["bw"], returns: "html" } as Provider],
	[
		"raw",
		{ name: "raw", kind: "command", command: ["curl"], returns: "html", failWhen: { matches: [] } } as Provider,
	],
]);

const searchProviders = new Map<string, Provider>([
	[
		"finder",
		{ name: "finder", role: "search", kind: "module", module: "./x.ts", returns: "results" } as Provider,
	],
	[
		"backup",
		{ name: "backup", role: "search", kind: "http", url: "https://s", returns: "results" } as Provider,
	],
]);

const row = (n: number): SearchResult => ({ position: n, title: `Result ${n}`, url: `https://e/${n}` });
const payload = (...rows: SearchResult[]): SearchPayload => ({ results: rows });

/** Executor driven by a per-provider script of outcomes. */
const scripted =
	<T>(script: Record<string, { ok: boolean; content?: T; reason?: string }>): Executor<T> =>
	async (provider) =>
		script[provider.name] ?? { ok: false, reason: "not scripted" };

const fetchRun = (over: Partial<Parameters<typeof runChain<string>>[0]>) =>
	runChain<string>({
		providers,
		config,
		role: "fetch",
		subject: "https://example.com",
		inspect: inspectText,
		executor: scripted<string>({}),
		...over,
	});

const searchRun = (over: Partial<Parameters<typeof runChain<SearchPayload>>[0]>) =>
	runChain<SearchPayload>({
		providers: searchProviders,
		config,
		role: "search",
		subject: "some query",
		inspect: inspectSearch,
		executor: scripted<SearchPayload>({}),
		...over,
	});

describe("runChain", () => {
	it("stops at the first provider that returns usable content", async () => {
		const result = await fetchRun({ executor: scripted({ remote: { ok: true, content: "# Real content" } }) });
		assert.equal(result.ok, true);
		assert.equal(result.provider, "remote");
		assert.equal(result.format, "markdown");
		assert.equal(result.attempts.length, 1);
	});

	it("falls through transport errors to the next rung", async () => {
		const result = await fetchRun({
			executor: scripted({
				remote: { ok: false, reason: "http 403" },
				hosted: { ok: true, content: "# From hosted" },
			}),
		});
		assert.equal(result.provider, "hosted");
		assert.deepEqual(
			result.attempts.map((a) => `${a.provider}:${a.ok}`),
			["remote:false", "hosted:true"],
		);
		assert.equal(result.attempts[0].reason, "http 403");
	});

	it("treats a 200 that returns a JS shell as a failure — the whole point of the chain", async () => {
		const result = await fetchRun({
			executor: scripted({
				remote: { ok: true, content: "Please enable JavaScript to view this site." },
				hosted: { ok: true, content: "# Rendered properly" },
			}),
		});
		assert.equal(result.provider, "hosted");
		assert.match(result.attempts[0].reason ?? "", /matched/);
	});

	it("honours a provider that opts out of the default match list", async () => {
		// A raw-HTML rung legitimately carries "enable JavaScript" inside a
		// <noscript> block; rejecting that would fail the whole call.
		const html = "<html><noscript>Please enable JavaScript</noscript><body>real page</body></html>";
		const result = await fetchRun({ forced: "raw", executor: scripted({ raw: { ok: true, content: html } }) });
		assert.equal(result.ok, true);
		assert.equal(result.provider, "raw");
	});

	it("fails with every attempt recorded when no rung works", async () => {
		const result = await fetchRun({
			executor: scripted({
				remote: { ok: false, reason: "http 451" },
				hosted: { ok: false, reason: "missing env HOSTED_TOKEN" },
				browser: { ok: false, reason: "not applicable: page yielded 0 bytes" },
				raw: { ok: false, reason: "exit 22: 404" },
			}),
		});
		assert.equal(result.ok, false);
		assert.equal(result.attempts.length, 4);
	});

	it("does not fall back when a provider is forced", async () => {
		const result = await fetchRun({
			forced: "remote",
			executor: scripted({ remote: { ok: false, reason: "http 500" }, hosted: { ok: true, content: "x" } }),
		});
		assert.equal(result.ok, false);
		assert.deepEqual(
			result.attempts.map((a) => a.provider),
			["remote"],
		);
	});

	it("survives an executor that throws", async () => {
		const result = await fetchRun({
			executor: async (provider) => {
				if (provider.name === "remote") throw new Error("socket hang up");
				return { ok: true, content: "# recovered" };
			},
		});
		assert.equal(result.ok, true);
		assert.equal(result.attempts[0].reason, "socket hang up");
	});

	it("reports skipped providers without counting them as attempts in the trace", async () => {
		const localOnly = new Map<string, Provider>([
			[
				"remote",
				{
					name: "remote",
					kind: "http",
					url: "u",
					returns: "markdown",
					when: { not: { hostGlob: ["localhost"] } },
				} as Provider,
			],
			["raw", { name: "raw", kind: "command", command: ["curl"], returns: "html" } as Provider],
		]);
		const result = await runChain<string>({
			providers: localOnly,
			config: { ...config, order: { ...config.order, fetch: ["remote", "raw"] } },
			role: "fetch",
			subject: "http://localhost:3000",
			inspect: inspectText,
			executor: scripted({ raw: { ok: true, content: "<html>dev server</html>" } }),
		});
		assert.equal(result.provider, "raw");
		assert.equal(formatChain(result.attempts), "raw✓0.0s");
	});

	it("retries a transport failure the provider declared worth retrying", async () => {
		const flaky = new Map<string, Provider>([
			["browser", { ...(providers.get("browser") as Provider), retries: 1, retryWhen: ["PageBindTimeout"] }],
		]);
		let calls = 0;
		const result = await fetchRun({
			providers: flaky,
			config: { ...config, order: { ...config.order, fetch: ["browser"] } },
			executor: async () => {
				calls += 1;
				return calls === 1
					? { ok: false, reason: 'exit 3: {"type": "PageBindTimeout"}' }
					: { ok: true, content: "# recovered" };
			},
		});
		assert.equal(result.ok, true);
		assert.equal(calls, 2);
		// The retry collapses into one recorded attempt: the trace reports the
		// rung's verdict, not its internal plumbing.
		assert.equal(result.attempts.length, 1);
	});

	it("does not retry a failure outside retryWhen", async () => {
		const flaky = new Map<string, Provider>([
			["browser", { ...(providers.get("browser") as Provider), retries: 3, retryWhen: ["PageBindTimeout"] }],
		]);
		let calls = 0;
		const result = await fetchRun({
			providers: flaky,
			config: { ...config, order: { ...config.order, fetch: ["browser"] } },
			executor: async () => {
				calls += 1;
				return { ok: false, reason: "exit 56: 404" };
			},
		});
		assert.equal(result.ok, false);
		assert.equal(calls, 1);
	});

	it("never retries content rejected by failWhen", async () => {
		// That verdict is deterministic — a second identical call only costs the
		// user time, and for a browser rung another tab.
		const flaky = new Map<string, Provider>([
			["browser", { ...(providers.get("browser") as Provider), retries: 3 }],
		]);
		let calls = 0;
		const result = await fetchRun({
			providers: flaky,
			config: { ...config, order: { ...config.order, fetch: ["browser"] } },
			executor: async () => {
				calls += 1;
				return { ok: true, content: "Please enable JavaScript" };
			},
		});
		assert.equal(result.ok, false);
		assert.equal(calls, 1);
		assert.match(result.attempts[0].reason ?? "", /matched/);
	});

	it("gives up after the declared number of retries", async () => {
		const flaky = new Map<string, Provider>([
			["browser", { ...(providers.get("browser") as Provider), retries: 2 }],
		]);
		let calls = 0;
		const result = await fetchRun({
			providers: flaky,
			config: { ...config, order: { ...config.order, fetch: ["browser"] } },
			executor: async () => {
				calls += 1;
				return { ok: false, reason: "socket hang up" };
			},
		});
		assert.equal(result.ok, false);
		assert.equal(calls, 3);
	});

	it("never offers one role's providers to the other tool", async () => {
		// A search provider must not be reachable from web_fetch even when it is
		// the only thing declared, or the model gets rows where it asked for prose.
		const mixed = new Map<string, Provider>([...providers, ...searchProviders]);
		const result = await runChain<string>({
			providers: mixed,
			config,
			role: "fetch",
			subject: "https://example.com",
			inspect: inspectText,
			executor: scripted({ finder: { ok: true, content: "should never run" } }),
		});
		assert.equal(result.ok, false);
		assert.equal(
			result.attempts.some((a) => a.provider === "finder"),
			false,
		);
	});
});

describe("runChain over list payloads", () => {
	it("returns rows from the first search rung that finds any", async () => {
		const result = await searchRun({ executor: scripted({ finder: { ok: true, content: payload(row(1), row(2)) } }) });
		assert.equal(result.ok, true);
		assert.equal(result.provider, "finder");
		assert.equal(result.content?.results.length, 2);
	});

	it("treats an empty result list as a failure and falls through", async () => {
		// A captcha or consent wall parses perfectly and yields zero rows. That is
		// indistinguishable from an honestly empty search unless we reject it.
		const result = await searchRun({
			executor: scripted({ finder: { ok: true, content: payload() }, backup: { ok: true, content: payload(row(1)) } }),
		});
		assert.equal(result.provider, "backup");
		assert.equal(result.attempts[0].reason, "no results");
	});

	it("enforces minResults per provider", async () => {
		const strict = new Map<string, Provider>([
			["finder", { ...(searchProviders.get("finder") as Provider), failWhen: { minResults: 3 } } as Provider],
		]);
		const result = await runChain<SearchPayload>({
			providers: strict,
			config,
			role: "search",
			subject: "q",
			inspect: inspectSearch,
			executor: scripted({ finder: { ok: true, content: payload(row(1), row(2)) } }),
		});
		assert.equal(result.ok, false);
		assert.equal(result.attempts[0].reason, "too few results (2 < 3)");
	});

	it("matches needles against titles and snippets, not the serialized JSON", async () => {
		const shell: SearchResult[] = [
			{ position: 1, title: "Please enable JavaScript", url: "https://e/1", snippet: "to continue" },
		];
		const result = await searchRun({
			executor: scripted({ finder: { ok: true, content: { results: shell } }, backup: { ok: true, content: payload(row(9)) } }),
		});
		assert.match(result.attempts[0].reason ?? "", /matched/);
		assert.equal(result.provider, "backup");
	});
});

describe("renderSuccess", () => {
	const base = { url: "https://example.com", maxBytes: 4096, maxLines: 100 };

	it("puts provider and format in the text, because details never reach the model", () => {
		const text = renderSuccess(
			{
				ok: true,
				attempts: [{ provider: "remote", ok: true, ms: 1100 }],
				provider: "remote",
				format: "markdown",
				content: "# Title\n\nBody.",
			},
			base,
		);
		assert.match(text, /provider=remote/);
		assert.match(text, /format=markdown/);
		assert.match(text, /^# Title/m);
	});

	it("omits the chain line when only one rung ran", () => {
		const text = renderSuccess(
			{
				ok: true,
				attempts: [{ provider: "remote", ok: true, ms: 900 }],
				provider: "remote",
				format: "markdown",
				content: "# T\n\nx",
			},
			base,
		);
		assert.equal(/chain:/.test(text), false);
	});

	it("shows the chain once more than one rung ran, so wasted time is visible", () => {
		const text = renderSuccess(
			{
				ok: true,
				attempts: [
					{ provider: "remote", ok: false, ms: 1100, reason: "matched" },
					{ provider: "hosted", ok: true, ms: 2800 },
				],
				provider: "hosted",
				format: "markdown",
				content: "# T\n\nx",
			},
			base,
		);
		assert.match(text, /chain: remote✗1\.1s hosted✓2\.8s/);
	});

	it("prefers a Title: preamble over the first heading", () => {
		// Measured on Wikipedia: a reader API emits "Title: Markdown" and then the
		// page's first heading is "# Contents", which is the table of contents.
		const content = "Title: Markdown\n\nURL Source: https://x\n\nMarkdown Content:\n# Contents\n\nbody";
		const text = renderSuccess(
			{
				ok: true,
				attempts: [{ provider: "remote", ok: true, ms: 10 }],
				provider: "remote",
				format: "markdown",
				content,
			},
			base,
		);
		assert.match(text, /^# Markdown$/m);
	});

	it("unwraps a quoted title out of YAML frontmatter", () => {
		const content = '---\ntitle: "Markdown - Wikipedia"\nmeta:\n  "og:title": "x"\n---\n[Jump to content](#a)';
		const text = renderSuccess(
			{
				ok: true,
				attempts: [{ provider: "hosted", ok: true, ms: 10 }],
				provider: "hosted",
				format: "markdown",
				content,
			},
			base,
		);
		assert.match(text, /^# Markdown - Wikipedia$/m);
	});

	it("extracts an html title", () => {
		const text = renderSuccess(
			{
				ok: true,
				attempts: [{ provider: "raw", ok: true, ms: 10 }],
				provider: "raw",
				format: "html",
				content: "<html><head><title>Doc  Page</title></head><body>x</body></html>",
			},
			base,
		);
		assert.match(text, /^# Doc Page$/m);
	});

	it("truncates and points at the full file", () => {
		const long = Array.from({ length: 200 }, (_, i) => `line ${i}`).join("\n");
		let written: string | undefined;
		const text = renderSuccess(
			{
				ok: true,
				attempts: [{ provider: "raw", ok: true, ms: 10 }],
				provider: "raw",
				format: "html",
				content: long,
			},
			{
				...base,
				maxLines: 10,
				writeOverflow: (content) => {
					written = content;
					return "/tmp/full.txt";
				},
			},
		);
		assert.match(text, /truncated: 10 of 200 lines/);
		assert.match(text, /full: \/tmp\/full\.txt/);
		assert.equal(written, long);
		assert.equal(text.includes("line 150"), false);
	});
});

describe("renderResults", () => {
	const render = (content: SearchPayload, query = "how to") =>
		renderResults(
			{ ok: true, attempts: [{ provider: "finder", ok: true, ms: 900 }], provider: "finder", format: "results", content },
			query,
		);

	it("lists numbered hits and points the model at web_fetch", () => {
		const text = render({ results: [{ position: 1, title: "Docs", url: "https://d", snippet: "how to" }] });
		assert.match(text, /1 results for "how to"/);
		assert.match(text, /1\. Docs/);
		assert.match(text, /https:\/\/d/);
		assert.match(text, /web_fetch/);
	});

	it("puts the date beside the snippet so freshness is visible without parsing prose", () => {
		const text = render({
			results: [{ position: 1, title: "T", url: "https://d", snippet: "body", date: "Mar 5, 2025" }],
		});
		assert.match(text, /Mar 5, 2025 · body/);
	});

	it("labels the answer box as generated and unsourced", () => {
		// It is the engine's own claim, not a page the model can go verify —
		// which is exactly the distinction worth spending tokens on.
		const text = render({ results: [row(1)], answerBox: { kind: "ai-overview", text: "Paris." } });
		assert.match(text, /## AI Overview \(generated by the search engine, unsourced\)/);
		assert.match(text, /Paris\./);
	});

	it("truncates a long answer rather than letting it dominate the result list", () => {
		const text = render({ results: [row(1)], answerBox: { kind: "ai-overview", text: "x".repeat(3000) } });
		assert.match(text, /\[truncated\]/);
		assert.equal(text.length < 2600, true);
	});

	it("renders the knowledge panel with its attributes", () => {
		const text = render({
			results: [row(1)],
			knowledgeGraph: { title: "Playwright", subtitle: "Software", description: "A library", attributes: { Developer: "Microsoft" } },
		});
		assert.match(text, /## Knowledge panel/);
		// Labelled, not run together: an unlabelled "Title — Subtitle" line
		// followed by a bare description was read by a model as one title plus a
		// subtitle that was actually the description.
		assert.match(text, /title: Playwright/);
		assert.match(text, /type: Software/);
		assert.match(text, /description: A library/);
		assert.match(text, /Developer: Microsoft/);
	});

	it("renders people-also-ask and related searches when present", () => {
		const text = render({ results: [row(1)], peopleAlsoAsk: ["Why?"], relatedSearches: ["a", "b"] });
		assert.match(text, /## People also ask\n- Why\?/);
		assert.match(text, /## Related searches\na · b/);
	});

	it("omits every optional section when the query triggered none", () => {
		const text = render({ results: [row(1)] });
		for (const heading of ["AI Overview", "Knowledge panel", "People also ask", "Related searches"]) {
			assert.equal(text.includes(heading), false, `${heading} should be absent`);
		}
	});
});

describe("renderFailure", () => {
	it("lists every attempt", () => {
		const text = renderFailure(
			{
				ok: false,
				attempts: [
					{ provider: "remote", ok: false, ms: 1000, reason: "http 403" },
					{ provider: "hosted", ok: false, ms: 0, reason: "missing env HOSTED_TOKEN" },
				],
			},
			"https://example.com",
			{ tool: "web_fetch", alternatives: ["remote", "hosted"] },
		);
		assert.match(text, /web_fetch failed for https:\/\/example\.com/);
		assert.match(text, /remote: http 403/);
		assert.match(text, /missing env HOSTED_TOKEN/);
	});

	it("names only rungs that were not already tried, so the hint is actionable", () => {
		const text = renderFailure(
			{ ok: false, attempts: [{ provider: "remote", ok: false, ms: 10, reason: "http 403" }] },
			"https://example.com",
			{ tool: "web_fetch", alternatives: ["remote", "browser"] },
		);
		assert.match(text, /provider="browser"/);
		assert.equal(/provider="remote"/.test(text), false);
	});

	it("omits the escalation line when there is nothing left to escalate to", () => {
		// The shipped config has exactly one rung per tool, so this is the normal
		// case — an escalation hint naming a provider that just failed is noise.
		const text = renderFailure(
			{ ok: false, attempts: [{ provider: "browser", ok: false, ms: 10, reason: "exit 3" }] },
			"https://example.com",
			{ tool: "web_fetch", alternatives: ["browser"] },
		);
		assert.equal(/Retry with/.test(text), false);
	});
});
