/**
 * Probe: learn what a provider's failures actually look like.
 *
 * A provider's failWhen rules are supposed to come from evidence, not from
 * guesses, so this runs one provider against a fixed set of real URLs chosen
 * to sit on its capability boundary (SPA shell, bot wall, login wall, PDF,
 * huge page, localhost) and reports what came back.
 *
 * Two deliberate constraints:
 *   - Probe is manual only. It hits real sites, and the browserwright rung
 *     opens tabs in the user's own browser.
 *   - Evidence files store summaries only, never whole pages. Real pages can
 *     carry the user's logged-in content.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { makeExecutor } from "./chain.ts";
import { EXTENSION_DIR } from "./config.ts";
import { failureReason, inspectText, resolveFailWhen } from "./predicates.ts";
import type { PiConfig, Provider } from "./types.ts";

export interface ProbeCase {
	name: string;
	url: string;
	/** What this case is meant to expose. Kept in the evidence file. */
	expose: string;
}

export interface ProbeRow {
	case: string;
	url: string;
	expose: string;
	/** Transport-level success, before failWhen is applied. */
	fetched: boolean;
	ms: number;
	chars: number;
	/** Why the transport failed. */
	error?: string;
	/** Why the current failWhen rule would reject the content, if it would. */
	wouldReject?: string;
	/** First 200 characters, whitespace collapsed. The signature to write rules from. */
	head?: string;
}

const DEFAULT_CASES: ProbeCase[] = [
	{ name: "article", url: "https://en.wikipedia.org/wiki/Markdown", expose: "normal long article" },
	{ name: "spa-shell", url: "https://web.telegram.org/", expose: "client-rendered shell, no server HTML" },
	{ name: "bot-wall", url: "https://www.g2.com/", expose: "bot challenge / interstitial" },
	{ name: "login-wall", url: "https://x.com/elonmusk", expose: "needs the user's session cookies" },
	{ name: "not-found", url: "https://example.com/definitely-not-here", expose: "404 handling" },
	{
		name: "huge",
		url: "https://en.wikipedia.org/wiki/List_of_Latin_phrases_(full)",
		expose: "page far past the 50KB truncation limit",
	},
	{
		name: "pdf",
		url: "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
		expose: "non-HTML content type",
	},
	{ name: "localhost", url: "http://localhost:3000/", expose: "local dev server (remote providers cannot reach it)" },
];

export function loadProbeCases(dir: string = EXTENSION_DIR): ProbeCase[] {
	const path = join(dir, "probe-cases.json");
	if (!existsSync(path)) return DEFAULT_CASES;
	try {
		const parsed = JSON.parse(readFileSync(path, "utf8")) as ProbeCase[];
		return Array.isArray(parsed) && parsed.length > 0 ? parsed : DEFAULT_CASES;
	} catch (error) {
		console.error(`[webfetch] probe-cases.json unreadable (${(error as Error).message}), using defaults`);
		return DEFAULT_CASES;
	}
}

export async function runProbe(
	provider: Provider,
	cases: ProbeCase[],
	options: {
		config: PiConfig;
		dir: string;
		onCase?: (probeCase: ProbeCase, index: number) => void;
	},
): Promise<ProbeRow[]> {
	// Bypass the chain on purpose: probing is about one provider's raw
	// behaviour, including on URLs its `when` predicate would normally skip.
	// Probe cases are URLs, so a probe run is always a fetch-role call.
	const executor = makeExecutor<string>(options.config, { dir: options.dir, role: "fetch" });
	const rule = resolveFailWhen(options.config.defaultFailWhen, provider.failWhen);
	const rows: ProbeRow[] = [];

	for (const [index, probeCase] of cases.entries()) {
		// Respect a provider's declared rate limit, or its own limiter turns the
		// probe into a page of 429s that read like "this provider cannot do it".
		if (index > 0 && provider.probeDelayMs) {
			await new Promise((resolve) => setTimeout(resolve, provider.probeDelayMs));
		}
		options.onCase?.(probeCase, index);
		const startedAt = performance.now();
		let outcome: Awaited<ReturnType<typeof executor>>;
		try {
			outcome = await executor(provider, probeCase.url);
		} catch (error) {
			outcome = { ok: false, reason: (error as Error).message };
		}
		const ms = Math.round(performance.now() - startedAt);

		if (!outcome.ok || outcome.content === undefined) {
			rows.push({
				case: probeCase.name,
				url: probeCase.url,
				expose: probeCase.expose,
				fetched: false,
				ms,
				chars: 0,
				error: outcome.reason ?? "failed",
			});
			continue;
		}

		rows.push({
			case: probeCase.name,
			url: probeCase.url,
			expose: probeCase.expose,
			fetched: true,
			ms,
			chars: outcome.content.length,
			wouldReject: failureReason(outcome.content, rule, inspectText),
			head: outcome.content.replace(/\s+/g, " ").trim().slice(0, 200),
		});
	}

	return rows;
}

export function formatProbeReport(providerName: string, rows: ProbeRow[]): string {
	const lines = [`webfetch probe — ${providerName}`, ""];
	for (const row of rows) {
		const verdict = !row.fetched
			? `ERROR ${row.error}`
			: row.wouldReject
				? `REJECTED (${row.wouldReject})`
				: "accepted";
		lines.push(`${row.case.padEnd(11)} ${String(row.chars).padStart(7)} chars  ${String(row.ms).padStart(5)}ms  ${verdict}`);
		if (row.head) lines.push(`            head: ${row.head.slice(0, 140)}`);
	}
	lines.push(
		"",
		"Write failWhen rules for this provider from the signatures above:",
		"a `matches` entry for each wall/shell phrase, and minChars only if a",
		"legitimately short page cannot be confused with an empty one.",
	);
	return lines.join("\n");
}

export function saveProbeEvidence(providerName: string, rows: ProbeRow[], dir: string = EXTENSION_DIR): string {
	const payload = {
		provider: providerName,
		// Stamped by the caller's clock; probe results drift as sites change,
		// so a rule traced back to this file needs to know how old it is.
		probedAt: new Date().toISOString(),
		note: "Summaries only — never whole pages, which may contain the user's logged-in content.",
		rows,
	};
	const body = `${JSON.stringify(payload, null, 2)}\n`;
	const path = join(dir, "providers", `${providerName}.probe.json`);

	// Evidence belongs next to the declaration it justifies — but when this
	// package is installed from npm it lives under node_modules, which is both
	// read-only in some setups and wiped on the next update. Fall back rather
	// than losing a probe run that just spent a minute opening real tabs.
	try {
		writeFileSync(path, body, "utf8");
		return path;
	} catch {
		const fallback = join(tmpdir(), `${providerName}.probe.json`);
		writeFileSync(fallback, body, "utf8");
		return fallback;
	}
}
