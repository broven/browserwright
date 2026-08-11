/**
 * Live verification harness. Runs the real chains against real inputs without
 * pi and without an LLM, so "does each rung actually work" is answerable
 * cheaply — including the search rung, which no unit test can exercise because
 * it needs a browser and the user's Chrome.
 *
 *   node verify.ts                              # fetch example.com + a canned search
 *   node verify.ts https://example.com          # fetch a specific URL
 *   node verify.ts --search "playwright aria"   # search a specific query
 */

import { makeExecutor, runChain } from "./core/chain.ts";
import { EXTENSION_DIR, loadConfig, loadProviders } from "./core/config.ts";
import { renderFailure, renderResults, renderSuccess } from "./core/format.ts";
import { inspectSearch, inspectText, providersForRole } from "./core/predicates.ts";
import type { SearchPayload } from "./core/types.ts";

const config = loadConfig();
const providers = loadProviders();

const args = process.argv.slice(2);
const searchAt = args.indexOf("--search");
const query = searchAt >= 0 ? (args[searchAt + 1] ?? "playwright aria snapshot") : undefined;
const url = searchAt >= 0 ? undefined : (args[0] ?? "https://example.com");

console.log(`providers loaded: ${[...providers.keys()].join(", ")}`);
console.log(`fetch order:  ${config.order.fetch.join(" → ")}`);
console.log(`search order: ${config.order.search.join(" → ")}\n`);

if (url !== undefined) {
	console.log(`── fetch chain: ${url} ──`);
	const chained = await runChain<string>({
		providers,
		config,
		role: "fetch",
		subject: url,
		inspect: inspectText,
		executor: makeExecutor<string>(config, { dir: EXTENSION_DIR, role: "fetch" }),
		onAttempt: (provider) => console.log(`   trying ${provider.name}…`),
	});
	console.log(
		chained.ok
			? `${renderSuccess(chained, { url, maxBytes: config.maxBytes, maxLines: config.maxLines }).slice(0, 600)}\n`
			: `${renderFailure(chained, url, { tool: "bw_web_fetch", alternatives: [...providersForRole(providers, "fetch").keys()] })}\n`,
	);

	console.log("── each fetch provider in isolation ──");
	for (const name of providersForRole(providers, "fetch").keys()) {
		const forced = await runChain<string>({
			providers,
			config,
			role: "fetch",
			subject: url,
			inspect: inspectText,
			forced: name,
			executor: makeExecutor<string>(config, { dir: EXTENSION_DIR, role: "fetch" }),
		});
		const attempt = forced.attempts.at(-1);
		const ms = attempt ? `${(attempt.ms / 1000).toFixed(1)}s` : "-";
		console.log(
			forced.ok
				? `✓ ${name.padEnd(22)} ${ms.padStart(6)}  ${forced.content?.length} chars (${forced.format})`
				: `✗ ${name.padEnd(22)} ${ms.padStart(6)}  ${attempt?.reason}`,
		);
	}
}

if (query !== undefined) {
	console.log(`── search chain: ${JSON.stringify(query)} ──`);
	const searched = await runChain<SearchPayload>({
		providers,
		config,
		role: "search",
		subject: query,
		inspect: inspectSearch,
		executor: makeExecutor<SearchPayload>(config, {
			dir: EXTENSION_DIR,
			role: "search",
			onProgress: (text) => console.log(`   ${text}`),
		}),
		onAttempt: (provider) => console.log(`   trying ${provider.name}…`),
	});
	console.log(
		searched.ok
			? renderResults(searched, query)
			: renderFailure(searched, query, {
					tool: "bw_web_search",
					alternatives: [...providersForRole(providers, "search").keys()],
				}),
	);
}
