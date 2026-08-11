/**
 * @browserwright/pi — `bw_web_fetch` and `bw_web_search` for pi, backed by
 * declarative providers that drive browserwright.
 *
 * Tool names are `bw_`-prefixed (not bare `web_fetch`/`web_search`) because
 * providers reserve generic tool names: grok rejects a custom function named
 * `web_search` with a 400. The prefix keeps every provider safe.
 *
 * A provider is a JSON file in providers/; adding one needs no code change.
 * This package ships only the browserwright rungs, which are the ones that
 * carry the user's login state. Drop your own JSON in to add a cheaper or
 * anonymous rung ahead of them. See README.md for the contract.
 */

import { Type } from "typebox";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { makeExecutor, runChain } from "./core/chain.ts";
import { EXTENSION_DIR, loadConfig, loadProviders } from "./core/config.ts";
import { renderFailure, renderResults, renderSuccess } from "./core/format.ts";
import { inspectSearch, inspectText, providersForRole } from "./core/predicates.ts";
import { formatProbeReport, loadProbeCases, runProbe, saveProbeEvidence } from "./core/probe.ts";
import type { PiConfig, Provider, Role, SearchPayload } from "./core/types.ts";

/**
 * A chain failure is reported by THROWING, not by returning a flag.
 *
 * `AgentToolResult` has no `isError` field: pi's agent loop hardcodes
 * `isError: false` on the normal return path and only sets it in the catch
 * around `execute`. Returning `{isError: true}` therefore records a failed call
 * as a successful one — the TUI does not mark it, and observers of the
 * `tool_result` event see `isError: false`.
 */
class ToolFailure extends Error {}

export default function (pi: ExtensionAPI) {
	const config = loadConfig();
	const providers = loadProviders();

	const namesFor = (role: Role): string[] => [...providersForRole(providers, role).keys()];
	const fetchNames = namesFor("fetch");
	const searchNames = namesFor("search");

	if (fetchNames.length === 0 && searchNames.length === 0) {
		console.error("[browserwright-pi] no provider declarations found in providers/ — both tools will always fail");
	}

	// setStatus is TUI/RPC only; print and json modes have no UI to update.
	const statusReporter =
		(ctx: ExtensionContext, onUpdate?: (partial: { content: Array<{ type: "text"; text: string }> }) => void) =>
		(text: string) => {
			if (ctx.hasUI) ctx.ui.setStatus("browserwright", text);
			onUpdate?.({ content: [{ type: "text", text: `*${text}*` }] });
		};

	// ---- bw_web_fetch ------------------------------------------------------

	pi.registerTool({
		name: "bw_web_fetch",
		label: "Fetch Web Page",
		description:
			"Fetch a URL and return its content as Markdown. " +
			`Tries providers in order until one returns usable content: ${config.order.fetch.join(" → ")}. ` +
			"The response header states which provider answered and what format the body is in. " +
			"Output over 50KB is truncated and the full text written to a temp file whose path is given.",
		promptSnippet: "Fetch a URL as markdown, through the user's real browser",
		promptGuidelines: [
			"Prefer `bw_web_fetch` over curl or a shell HTTP client for reading web pages — it renders JavaScript " +
				"and carries the user's login state, so it can read pages an anonymous request cannot.",
		],
		parameters: Type.Object({
			url: Type.String({ description: "HTTP(S) URL to fetch" }),
			provider: Type.Optional(
				Type.String({
					description:
						`Force one provider instead of the automatic chain (${fetchNames.join(", ") || "none declared"}). ` +
						"Forcing disables fallback.",
				}),
			),
		}),
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			const url = /^https?:\/\//i.test(params.url) ? params.url : `https://${params.url}`;
			const setStatus = statusReporter(ctx, onUpdate);

			const result = await runChain<string>({
				providers,
				config,
				role: "fetch",
				subject: url,
				inspect: inspectText,
				forced: params.provider,
				executor: makeExecutor<string>(config, { dir: EXTENSION_DIR, role: "fetch", signal }),
				onAttempt: (provider, index, total) =>
					setStatus(`🌐 ${provider.label ?? provider.name} (${index + 1}/${total})`),
			});

			if (ctx.hasUI) ctx.ui.setStatus("browserwright", "");

			if (!result.ok) {
				throw new ToolFailure(renderFailure(result, url, { tool: "bw_web_fetch", alternatives: fetchNames }));
			}

			return {
				content: [
					{
						type: "text" as const,
						text: renderSuccess(result, { url, maxBytes: config.maxBytes, maxLines: config.maxLines }),
					},
				],
				details: {
					url,
					provider: result.provider,
					format: result.format,
					chars: result.content?.length ?? 0,
					attempts: result.attempts,
				},
			};
		},
	});

	// ---- bw_web_search -----------------------------------------------------

	pi.registerTool({
		name: "bw_web_search",
		label: "Search the Web",
		description:
			"Search the web and return ranked results as title, URL, snippet and date. " +
			`Providers in order: ${config.order.search.join(" → ")}. ` +
			"Also returns the engine's own AI Overview, knowledge panel, 'people also ask' and " +
			"related searches when that query triggered them. Returns links, never page bodies — " +
			"call bw_web_fetch on the ones worth reading.",
		promptSnippet: "Search the web and get back ranked links",
		promptGuidelines: [
			"`bw_web_search` returns links, not page contents. After searching, call `bw_web_fetch` on the one or two " +
				"results actually worth reading rather than fetching all of them.",
		],
		parameters: Type.Object({
			query: Type.String({ description: "What to search for" }),
			provider: Type.Optional(
				Type.String({
					description:
						`Force one provider instead of the automatic chain (${searchNames.join(", ") || "none declared"}). ` +
						"Forcing disables fallback.",
				}),
			),
		}),
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			const query = params.query.trim();
			if (!query) throw new ToolFailure("bw_web_search needs a non-empty query");
			const setStatus = statusReporter(ctx, onUpdate);

			const result = await runChain<SearchPayload>({
				providers,
				config,
				role: "search",
				subject: query,
				inspect: inspectSearch,
				forced: params.provider,
				executor: makeExecutor<SearchPayload>(config, {
					dir: EXTENSION_DIR,
					role: "search",
					signal,
					// Only module providers stream, and the search rung is why
					// that capability exists: it is slow enough that the user
					// deserves to see which phase it is in.
					onProgress: (text) => setStatus(`🔎 ${text}`),
				}),
				onAttempt: (provider, index, total) =>
					setStatus(`🔎 ${provider.label ?? provider.name} (${index + 1}/${total})`),
			});

			if (ctx.hasUI) ctx.ui.setStatus("browserwright", "");

			if (!result.ok) {
				throw new ToolFailure(renderFailure(result, query, { tool: "bw_web_search", alternatives: searchNames }));
			}

			return {
				content: [{ type: "text" as const, text: renderResults(result, query) }],
				details: {
					query,
					provider: result.provider,
					count: result.content?.results.length ?? 0,
					features: {
						answerBox: Boolean(result.content?.answerBox),
						knowledgeGraph: Boolean(result.content?.knowledgeGraph),
						peopleAlsoAsk: result.content?.peopleAlsoAsk?.length ?? 0,
						relatedSearches: result.content?.relatedSearches?.length ?? 0,
					},
					attempts: result.attempts,
				},
			};
		},
	});

	// ---- /bw ---------------------------------------------------------------
	// Named `/bw` (not `/browserwright`) so it cannot be confused with the
	// `browserwright` skill, which pi exposes as `/skill:browserwright`.

	pi.registerCommand("bw", {
		description:
			"Inspect providers (/bw list) or probe one against real URLs (/bw probe <provider>)",
		handler: async (args, ctx) => {
			const [subcommand, target] = args.trim().split(/\s+/);

			if (!subcommand || subcommand === "list") {
				ctx.ui.notify(describeProviders(config, providers), "info");
				return;
			}

			if (subcommand !== "probe") {
				ctx.ui.notify(`Unknown subcommand "${subcommand}". Use "list" or "probe <provider>".`, "error");
				return;
			}

			// Probing only makes sense for fetch providers — the cases are URLs.
			const chosen = target ? [target] : fetchNames;
			const unknown = chosen.filter((name) => !fetchNames.includes(name));
			if (unknown.length > 0) {
				ctx.ui.notify(`Not a probeable fetch provider: ${unknown.join(", ")}`, "error");
				return;
			}

			// Probe hits real sites and opens tabs in the user's daily Chrome.
			// It never runs without an explicit yes.
			if (!ctx.hasUI) return;
			const cases = loadProbeCases();
			const ok = await ctx.ui.confirm(
				"Run probe?",
				`Hits ${cases.length} real URLs for: ${chosen.join(", ")}.\n\nThis opens tabs in your daily Chrome.`,
			);
			if (!ok) return;

			for (const name of chosen) {
				const provider = providers.get(name) as Provider;
				ctx.ui.setStatus("browserwright", `🔬 probing ${name}…`);
				const rows = await runProbe(provider, cases, {
					config,
					dir: EXTENSION_DIR,
					onCase: (probeCase, index) =>
						ctx.ui.setStatus("browserwright", `🔬 ${name} ${index + 1}/${cases.length}: ${probeCase.name}`),
				});
				const path = saveProbeEvidence(name, rows);
				// deliverAs "steer" so the report renders as soon as the probe
				// finishes. "nextTurn" would queue it invisibly until the user
				// typed something else, which defeats the point of a report.
				pi.sendMessage(
					{
						customType: "browserwright-probe",
						content: `${formatProbeReport(name, rows)}\n\nEvidence written to ${path}`,
						display: true,
					},
					{ deliverAs: "steer" },
				);
			}

			ctx.ui.setStatus("browserwright", "");
		},
	});
}

function describeProviders(config: PiConfig, providers: Map<string, Provider>): string {
	const lines: string[] = [];
	for (const role of ["fetch", "search"] as Role[]) {
		const scoped = providersForRole(providers, role);
		lines.push(`${role}:`);
		if (scoped.size === 0) {
			lines.push("  (none declared)");
			continue;
		}
		const order = config.order[role] ?? [];
		const names = [...order.filter((n) => scoped.has(n)), ...[...scoped.keys()].filter((n) => !order.includes(n))];
		for (const [index, name] of names.entries()) {
			const provider = scoped.get(name);
			if (!provider) continue;
			const flags = [provider.kind, `returns=${provider.returns}`];
			if (provider.enabled === false) flags.push("disabled");
			if (provider.when) flags.push("conditional");
			lines.push(`  ${index + 1}. ${name} (${flags.join(", ")})`);
		}
	}
	return lines.join("\n");
}
