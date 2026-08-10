/**
 * Coercing an arbitrary JSON list into SearchResult rows.
 *
 * Every hosted search API returns the same three facts under different names
 * (`link` vs `url` vs `href`, `snippet` vs `description` vs `content`). Doing
 * the aliasing here is what keeps a new search provider a pure JSON drop-in
 * instead of a code change — the same promise `kind: "http"` already makes for
 * reader APIs.
 */

import type { AnswerBox, KnowledgeGraph, SearchPayload, SearchResult } from "./types.ts";

const TITLE_KEYS = ["title", "name", "heading"] as const;
const URL_KEYS = ["url", "link", "href"] as const;
const SNIPPET_KEYS = ["snippet", "description", "content", "text", "excerpt"] as const;
const DATE_KEYS = ["date", "published", "publishedDate", "published_date"] as const;

const RESULTS_KEYS = ["results", "organic", "organic_results", "items", "webPages"] as const;
const ANSWER_KEYS = ["answerBox", "answer_box", "answer", "featured_snippet"] as const;
const KG_KEYS = ["knowledgeGraph", "knowledge_graph"] as const;
const PAA_KEYS = ["peopleAlsoAsk", "people_also_ask", "relatedQuestions"] as const;
const RELATED_KEYS = ["relatedSearches", "related_searches", "relatedQueries"] as const;

function firstValue(row: Record<string, unknown>, keys: readonly string[]): unknown {
	for (const key of keys) {
		if (row[key] !== undefined && row[key] !== null) return row[key];
	}
	return undefined;
}

/** Pull a list of plain strings out of whatever shape the API used for it. */
function stringList(value: unknown): string[] | undefined {
	if (!Array.isArray(value)) return undefined;
	const out: string[] = [];
	for (const item of value) {
		if (typeof item === "string" && item.trim()) {
			out.push(item.trim());
			continue;
		}
		if (item && typeof item === "object") {
			// Serper-style rows: {question: "..."} / {query: "..."}
			const row = item as Record<string, unknown>;
			const text = firstString(row, ["question", "query", "title", "text", "name"]);
			if (text) out.push(text);
		}
	}
	return out.length > 0 ? [...new Set(out)] : undefined;
}

function firstString(row: Record<string, unknown>, keys: readonly string[]): string | undefined {
	for (const key of keys) {
		const value = row[key];
		if (typeof value === "string" && value.trim()) return value.trim();
	}
	return undefined;
}

/**
 * Keep only rows that carry at least a URL — a row without one is not a search
 * result the model can act on, and silently keeping it would inflate the count
 * that `minResults` guards.
 */
export function normalizeResults(value: unknown): SearchResult[] {
	if (!Array.isArray(value)) return [];

	const out: SearchResult[] = [];
	for (const item of value) {
		if (!item || typeof item !== "object") continue;
		const row = item as Record<string, unknown>;
		const url = firstString(row, URL_KEYS);
		if (!url) continue;
		out.push({
			position: out.length + 1,
			title: firstString(row, TITLE_KEYS) ?? url,
			url,
			snippet: firstString(row, SNIPPET_KEYS),
			date: firstString(row, DATE_KEYS),
		});
	}
	return out;
}

function normalizeAnswerBox(value: unknown): AnswerBox | undefined {
	if (typeof value === "string") {
		return value.trim() ? { kind: "featured-snippet", text: value.trim() } : undefined;
	}
	if (!value || typeof value !== "object") return undefined;
	const row = value as Record<string, unknown>;
	const text = firstString(row, ["text", "answer", "snippet", "description", "content"]);
	if (!text) return undefined;
	const kind = firstString(row, ["kind", "type"]);
	return { kind: kind === "ai-overview" ? "ai-overview" : "featured-snippet", text };
}

function normalizeKnowledgeGraph(value: unknown): KnowledgeGraph | undefined {
	if (!value || typeof value !== "object") return undefined;
	const row = value as Record<string, unknown>;
	const attributes = row.attributes;
	const graph: KnowledgeGraph = {
		title: firstString(row, ["title", "name"]),
		subtitle: firstString(row, ["subtitle", "type", "category"]),
		description: firstString(row, ["description", "snippet"]),
	};
	if (attributes && typeof attributes === "object" && !Array.isArray(attributes)) {
		const flat: Record<string, string> = {};
		for (const [key, raw] of Object.entries(attributes as Record<string, unknown>)) {
			if (typeof raw === "string" && raw.trim()) flat[key] = raw.trim();
		}
		if (Object.keys(flat).length > 0) graph.attributes = flat;
	}
	return graph.title || graph.description || graph.attributes ? graph : undefined;
}

/**
 * Coerce a whole search response into a SearchPayload.
 *
 * Accepts a bare array (just organic rows) or an object keyed the way hosted
 * APIs key it. That aliasing is what keeps a hosted search provider a pure JSON
 * drop-in: a Serper response, for instance, maps field-for-field with no code.
 */
export function normalizeSearchPayload(value: unknown): SearchPayload {
	if (Array.isArray(value)) return { results: normalizeResults(value) };
	if (!value || typeof value !== "object") return { results: [] };

	const body = value as Record<string, unknown>;
	const payload: SearchPayload = { results: normalizeResults(firstValue(body, RESULTS_KEYS)) };

	const answerBox = normalizeAnswerBox(firstValue(body, ANSWER_KEYS));
	if (answerBox) payload.answerBox = answerBox;

	const knowledgeGraph = normalizeKnowledgeGraph(firstValue(body, KG_KEYS));
	if (knowledgeGraph) payload.knowledgeGraph = knowledgeGraph;

	const peopleAlsoAsk = stringList(firstValue(body, PAA_KEYS));
	if (peopleAlsoAsk) payload.peopleAlsoAsk = peopleAlsoAsk;

	const relatedSearches = stringList(firstValue(body, RELATED_KEYS));
	if (relatedSearches) payload.relatedSearches = relatedSearches;

	return payload;
}
