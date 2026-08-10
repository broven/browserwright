/**
 * Turning a ChainResult into the text the model sees.
 *
 * Everything the model needs to act on has to be in `content` — pi's tool
 * `details` field never reaches the LLM, it only feeds the TUI renderer. So the
 * provider name, the format, and the escalation hint all live in the text.
 */

import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Attempt, ChainResult, ReturnFormat, SearchPayload } from "./types.ts";

/** Pull a title out of the content itself rather than fetching one. */
export function extractTitle(content: string, format: ReturnFormat): string | undefined {
	if (format === "html") {
		const match = content.match(/<title[^>]*>([\s\S]{0,300}?)<\/title>/i);
		const title = match?.[1]?.replace(/\s+/g, " ").trim();
		return title || undefined;
	}
	// Providers announce the title before the body and each does it differently:
	// jina writes a "Title: …" preamble, Cloudflare emits YAML frontmatter. Both
	// beat the first heading, which is often a table of contents ("# Contents"
	// on Wikipedia).
	const head = content.slice(0, 400);
	const declared = (head.match(/^Title:\s*(.+)$/m) ?? head.match(/^title:\s*(.+)$/m))?.[1]
		?.trim()
		.replace(/^"(.*)"$/, "$1")
		.trim();
	if (declared) return declared;

	const heading = content.match(/^#{1,2}\s+(.+)$/m);
	const title = heading?.[1]?.trim();
	return title || undefined;
}

/** "browserwright✗1.1s browserwright-search✓2.4s" — only rungs actually called. */
export function formatChain(attempts: Attempt[]): string {
	return attempts
		.filter((attempt) => !attempt.skipped)
		.map((attempt) => `${attempt.provider}${attempt.ok ? "✓" : "✗"}${(attempt.ms / 1000).toFixed(1)}s`)
		.join(" ");
}

function humanSize(bytes: number): string {
	if (bytes < 1024) return `${bytes}B`;
	if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
	return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

export interface RenderOptions {
	url: string;
	maxBytes: number;
	maxLines: number;
	/** Injected so tests do not touch the filesystem. */
	writeOverflow?: (content: string) => string;
}

function defaultWriteOverflow(content: string): string {
	const name = `browserwright-pi-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}.txt`;
	const path = join(tmpdir(), name);
	writeFileSync(path, content, "utf8");
	return path;
}

/** Truncate on a line boundary, keeping the head. */
function truncate(content: string, maxBytes: number, maxLines: number) {
	const lines = content.split("\n");
	const kept: string[] = [];
	let bytes = 0;
	for (const line of lines) {
		const size = Buffer.byteLength(line, "utf8") + 1;
		if (kept.length >= maxLines || bytes + size > maxBytes) break;
		kept.push(line);
		bytes += size;
	}
	return {
		content: kept.join("\n"),
		truncated: kept.length < lines.length,
		keptLines: kept.length,
		totalLines: lines.length,
		keptBytes: bytes,
		totalBytes: Buffer.byteLength(content, "utf8"),
	};
}

/** The fetch success path. Header first, so it survives truncation of the body. */
export function renderSuccess(result: ChainResult<string>, options: RenderOptions): string {
	const content = result.content ?? "";
	const format = result.format ?? "text";
	const cut = truncate(content, options.maxBytes, options.maxLines);

	const header: string[] = [];
	const title = extractTitle(content, format);
	if (title) header.push(`# ${title}`);
	header.push(options.url);

	const meta = [`provider=${result.provider}`, `format=${format}`, humanSize(cut.totalBytes)];
	header.push(meta.join(" · "));

	// Only worth the tokens when more than one rung was actually tried.
	const called = result.attempts.filter((attempt) => !attempt.skipped);
	if (called.length > 1) header.push(`chain: ${formatChain(result.attempts)}`);

	if (cut.truncated) {
		const path = (options.writeOverflow ?? defaultWriteOverflow)(content);
		header.push(
			`truncated: ${cut.keptLines} of ${cut.totalLines} lines (${humanSize(cut.keptBytes)} of ${humanSize(cut.totalBytes)}) · full: ${path}`,
		);
	}

	return `${header.join("\n")}\n\n${cut.content}`;
}

/** The engine's answer can run long; the model asked for links, not an essay. */
const ANSWER_BOX_CAP = 1200;

/**
 * The search success path. Links plus whatever SERP features the query
 * triggered — but never page bodies: `web_fetch` already does that, and the
 * model is in a better position to decide which two of ten links are worth the
 * tokens.
 *
 * Every section below the results is optional and most queries trigger none of
 * them, so each is omitted entirely rather than rendered empty.
 */
export function renderResults(result: ChainResult<SearchPayload>, query: string): string {
	const payload = result.content ?? { results: [] };
	const results = payload.results;
	const out: string[] = [`# ${results.length} results for ${JSON.stringify(query)}`, `provider=${result.provider}`];

	const called = result.attempts.filter((attempt) => !attempt.skipped);
	if (called.length > 1) out.push(`chain: ${formatChain(result.attempts)}`);

	if (payload.answerBox) {
		const label = payload.answerBox.kind === "ai-overview" ? "AI Overview" : "Featured snippet";
		const text = truncateChars(payload.answerBox.text, ANSWER_BOX_CAP);
		// Attributed, because it is the engine's claim and not a source the
		// model can go read — unlike everything else on this page.
		out.push("", `## ${label} (generated by the search engine, unsourced)`, text);
	}

	const graph = payload.knowledgeGraph;
	if (graph) {
		const lines: string[] = [];
		// Each part is labelled rather than run together. An earlier version
		// joined title and subtitle with an em dash and left the description as a
		// bare next line; a model reading that took the whole first line as the
		// title and the description as the subtitle.
		if (graph.title) lines.push(`title: ${graph.title}`);
		if (graph.subtitle) lines.push(`type: ${graph.subtitle}`);
		if (graph.description) lines.push(`description: ${graph.description}`);
		for (const [key, value] of Object.entries(graph.attributes ?? {})) lines.push(`${key}: ${value}`);
		if (lines.length > 0) out.push("", "## Knowledge panel", ...lines);
	}

	if (results.length > 0) {
		out.push("");
		for (const row of results) {
			out.push(`${row.position}. ${row.title}`, `   ${row.url}`);
			const tail = [row.date, row.snippet].filter(Boolean).join(" · ");
			if (tail) out.push(`   ${tail}`);
			out.push("");
		}
	}

	if (payload.peopleAlsoAsk?.length) {
		out.push("## People also ask", ...payload.peopleAlsoAsk.map((q) => `- ${q}`), "");
	}
	if (payload.relatedSearches?.length) {
		out.push("## Related searches", payload.relatedSearches.join(" · "), "");
	}

	out.push("Use web_fetch on a URL above to read it.");
	return out.join("\n");
}

function truncateChars(text: string, cap: number): string {
	if (text.length <= cap) return text;
	return `${text.slice(0, cap).trimEnd()}… [truncated]`;
}

export interface FailureOptions {
	/** Tool name, so the first line names what actually failed. */
	tool: string;
	/**
	 * Provider names the model could force instead. Generated from the loaded
	 * declarations rather than written into this file — hardcoding them here is
	 * what previously kept naming a `curl` rung that no longer existed.
	 */
	alternatives?: string[];
}

/** The failure path. Must tell the model how to escalate, when it can. */
export function renderFailure(result: ChainResult<unknown>, subject: string, options: FailureOptions): string {
	const lines = [`${options.tool} failed for ${subject}`, ""];
	for (const attempt of result.attempts) {
		const marker = attempt.skipped ? "skipped" : `${(attempt.ms / 1000).toFixed(1)}s`;
		lines.push(`  ${attempt.provider}: ${attempt.reason ?? "failed"} (${marker})`);
	}

	const untried = (options.alternatives ?? []).filter(
		(name) => !result.attempts.some((attempt) => attempt.provider === name && !attempt.skipped),
	);
	if (untried.length > 0) {
		const list = untried.map((name) => `provider=${JSON.stringify(name)}`).join(" or ");
		lines.push("", `Retry with ${list} to force a specific rung.`);
	}

	return lines.join("\n");
}
