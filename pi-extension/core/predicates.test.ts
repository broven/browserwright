/**
 * Unit tests for the selection and rejection logic.
 *
 * This is the part of the package that fails silently when it is wrong: a rung
 * that is never tried, or a JS shell accepted as success, produces no error —
 * just quietly worse results. Hence tests here and nowhere else.
 *
 * Run: node --test 'core/*.test.ts'
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
	failureReason,
	globMatchesAny,
	inspectSearch,
	inspectText,
	interpolate,
	interpolateEnv,
	matchesSubject,
	pickPath,
	providersForRole,
	resolveFailWhen,
	roleOf,
	selectProviders,
	subjectTokens,
} from "./predicates.ts";
import { normalizeResults, normalizeSearchPayload } from "./results.ts";
import type { Provider, SearchPayload, SearchResult } from "./types.ts";

const http = (name: string, extra: Partial<Provider> = {}): Provider =>
	({ name, kind: "http", url: "https://x/{url}", returns: "markdown", ...extra }) as Provider;

const row = (n: number, over: Partial<SearchResult> = {}): SearchResult => ({
	position: n,
	title: `Result ${n}`,
	url: `https://e/${n}`,
	...over,
});
const payload = (...rows: SearchResult[]): SearchPayload => ({ results: rows });

describe("globMatchesAny", () => {
	it("anchors at both ends", () => {
		assert.equal(globMatchesAny("localhost", ["localhost"]), true);
		assert.equal(globMatchesAny("notlocalhost", ["localhost"]), false);
	});

	it("treats * as any run of characters and ignores case", () => {
		assert.equal(globMatchesAny("http://localhost:3000/a", ["http://localhost*"]), true);
		assert.equal(globMatchesAny("API.Example.COM", ["*.example.com"]), true);
	});

	it("does not let dots in the glob match arbitrary characters", () => {
		assert.equal(globMatchesAny("axexample.com", ["*.example.com"]), false);
	});
});

describe("matchesSubject", () => {
	it("is applicable when no predicate is declared", () => {
		assert.equal(matchesSubject("https://example.com", undefined), true);
		assert.equal(matchesSubject("https://example.com", {}), true);
	});

	it("matches on host globs", () => {
		const when = { hostGlob: ["localhost", "*.local"] };
		assert.equal(matchesSubject("http://localhost:3000/x", when), true);
		assert.equal(matchesSubject("http://dev.local/x", when), true);
		assert.equal(matchesSubject("https://example.com", when), false);
	});

	it("supports the not form remote providers need", () => {
		// "I am a hosted service, I cannot reach your LAN."
		const when = { not: { hostGlob: ["localhost", "127.*", "192.168.*"] } };
		assert.equal(matchesSubject("https://example.com", when), true);
		assert.equal(matchesSubject("http://localhost:8080", when), false);
		assert.equal(matchesSubject("http://192.168.1.1", when), false);
	});

	it("survives a subject that is not a URL at all", () => {
		// Search subjects are raw queries. hostGlob simply never matches rather
		// than throwing, so a fetch-shaped predicate on a search provider is inert
		// instead of fatal.
		assert.equal(matchesSubject("how to parse yaml", { hostGlob: ["localhost"] }), false);
		assert.equal(matchesSubject("how to parse yaml", undefined), true);
		assert.equal(matchesSubject("how to parse yaml", { urlGlob: ["*yaml*"] }), true);
	});
});

describe("resolveFailWhen", () => {
	const defaults = { minChars: 0, minResults: 0, matches: ["captcha"] };

	it("falls back to the core defaults", () => {
		assert.deepEqual(resolveFailWhen(defaults, undefined), defaults);
	});

	it("replaces a field rather than merging it", () => {
		const merged = resolveFailWhen(defaults, { matches: ["just a moment"] });
		assert.deepEqual(merged.matches, ["just a moment"]);
	});

	it("lets an empty array switch the default matches off — the browser rung relies on this", () => {
		const merged = resolveFailWhen(defaults, { matches: [] });
		assert.deepEqual(merged.matches, []);
	});

	it("carries minResults through for list payloads", () => {
		assert.equal(resolveFailWhen(defaults, { minResults: 3 }).minResults, 3);
	});
});

describe("failureReason over text", () => {
	const rule = (over = {}) => ({ minChars: 0, minResults: 0, matches: [], ...over });

	it("rejects empty and whitespace-only content", () => {
		assert.match(failureReason("", rule(), inspectText) ?? "", /empty/);
		assert.match(failureReason("   \n\t ", rule(), inspectText) ?? "", /empty/);
	});

	it("does not apply a minimum length when minChars is 0", () => {
		assert.equal(failureReason("tiny", rule(), inspectText), undefined);
	});

	it("applies minChars when set", () => {
		assert.match(failureReason("tiny", rule({ minChars: 100 }), inspectText) ?? "", /too short/);
	});

	it("matches wall phrases case-insensitively", () => {
		const r = rule({ matches: ["Enable JavaScript"] });
		assert.match(failureReason("please enable javascript to continue", r, inspectText) ?? "", /matched/);
	});

	it("accepts good content", () => {
		assert.equal(failureReason("# Title\n\nReal content.", rule({ matches: ["captcha"] }), inspectText), undefined);
	});
});

describe("failureReason over result lists", () => {
	const rule = (over = {}) => ({ minChars: 0, minResults: 0, matches: [], ...over });

	it("rejects an empty list even with minResults unset", () => {
		// The interstitial case: valid JSON, zero rows, no error anywhere.
		assert.equal(failureReason(payload(), rule(), inspectSearch), "no results");
	});

	it("applies minResults", () => {
		assert.equal(failureReason(payload(row(1)), rule({ minResults: 3 }), inspectSearch), "too few results (1 < 3)");
	});

	it("accepts a list that clears the floor", () => {
		assert.equal(failureReason(payload(row(1), row(2)), rule({ minResults: 2 }), inspectSearch), undefined);
	});

	it("does not apply minChars to a list", () => {
		// minChars measures a prose blob. Applying it to joined titles would
		// reject a short but perfectly good set of hits — minResults is the
		// floor that means anything for a list.
		assert.equal(failureReason(payload(row(1)), rule({ minChars: 5000 }), inspectSearch), undefined);
	});

	it("searches titles and snippets, not JSON punctuation", () => {
		const inspected = inspectSearch(payload(row(1, { title: "Cats", snippet: "about cats" })));
		assert.equal(inspected.count, 1);
		assert.equal(inspected.text.includes("position"), false);
		assert.equal(inspected.text.includes("https://"), false);
		assert.match(inspected.text, /Cats about cats/);
	});
});

describe("normalizeResults", () => {
	it("accepts the field names hosted search APIs actually use", () => {
		const rows = normalizeResults([
			{ title: "A", link: "https://a", snippet: "sa" },
			{ name: "B", href: "https://b", description: "sb" },
			{ heading: "C", url: "https://c", content: "sc" },
		]);
		assert.deepEqual(
			rows.map((r) => `${r.position}:${r.title}:${r.url}:${r.snippet}`),
			["1:A:https://a:sa", "2:B:https://b:sb", "3:C:https://c:sc"],
		);
	});

	it("drops rows with no URL and renumbers, so minResults counts actionable hits", () => {
		const rows = normalizeResults([{ title: "no link" }, { title: "A", url: "https://a" }]);
		assert.equal(rows.length, 1);
		assert.equal(rows[0].position, 1);
	});

	it("falls back to the URL when a row has no title", () => {
		assert.equal(normalizeResults([{ url: "https://a" }])[0].title, "https://a");
	});

	it("returns an empty list for anything that is not an array", () => {
		assert.deepEqual(normalizeResults(undefined), []);
		assert.deepEqual(normalizeResults({ results: [] }), []);
		assert.deepEqual(normalizeResults("nope"), []);
	});
});

describe("normalizeSearchPayload", () => {
	it("accepts a bare array as organic rows", () => {
		const out = normalizeSearchPayload([{ title: "A", link: "https://a" }]);
		assert.equal(out.results.length, 1);
		assert.equal(out.answerBox, undefined);
	});

	it("maps a hosted API response field-for-field with no code", () => {
		// Serper's shape. This is the whole promise of the declarative provider
		// layer: a new search API should be a JSON file, not a code change.
		const out = normalizeSearchPayload({
			organic: [{ title: "A", link: "https://a", snippet: "sa", date: "Mar 5, 2025" }],
			answerBox: { snippet: "42" },
			knowledgeGraph: { title: "Douglas Adams", type: "Author", description: "Writer" },
			peopleAlsoAsk: [{ question: "Why 42?" }],
			relatedSearches: [{ query: "hitchhiker guide" }],
		});
		assert.equal(out.results[0].url, "https://a");
		assert.equal(out.results[0].date, "Mar 5, 2025");
		assert.equal(out.answerBox?.text, "42");
		assert.equal(out.knowledgeGraph?.title, "Douglas Adams");
		assert.equal(out.knowledgeGraph?.subtitle, "Author");
		assert.deepEqual(out.peopleAlsoAsk, ["Why 42?"]);
		assert.deepEqual(out.relatedSearches, ["hitchhiker guide"]);
	});

	it("keeps snake_case aliases working", () => {
		const out = normalizeSearchPayload({
			organic_results: [{ title: "A", url: "https://a" }],
			answer_box: "yes",
			people_also_ask: ["q1"],
			related_searches: ["r1"],
		});
		assert.equal(out.results.length, 1);
		assert.equal(out.answerBox?.text, "yes");
		assert.deepEqual(out.peopleAlsoAsk, ["q1"]);
		assert.deepEqual(out.relatedSearches, ["r1"]);
	});

	it("omits optional features entirely rather than emitting empty ones", () => {
		// Every consumer branches on presence; an empty array would render a
		// heading with nothing under it.
		const out = normalizeSearchPayload({ organic: [{ url: "https://a" }], peopleAlsoAsk: [] });
		assert.equal("answerBox" in out, false);
		assert.equal("peopleAlsoAsk" in out, false);
		assert.equal("relatedSearches" in out, false);
	});

	it("survives junk without throwing", () => {
		assert.deepEqual(normalizeSearchPayload(null).results, []);
		assert.deepEqual(normalizeSearchPayload("nope").results, []);
		assert.deepEqual(normalizeSearchPayload({ organic: "not a list" }).results, []);
	});
});

describe("roles", () => {
	const mixed = new Map<string, Provider>([
		["f", http("f")],
		["s", http("s", { role: "search" })],
	]);

	it("defaults an undeclared role to fetch", () => {
		assert.equal(roleOf(http("f")), "fetch");
		assert.equal(roleOf(http("s", { role: "search" })), "search");
	});

	it("partitions providers so neither tool can reach the other's rungs", () => {
		assert.deepEqual([...providersForRole(mixed, "fetch").keys()], ["f"]);
		assert.deepEqual([...providersForRole(mixed, "search").keys()], ["s"]);
	});
});

describe("selectProviders", () => {
	const providers = new Map<string, Provider>([
		["remote", http("remote", { when: { not: { hostGlob: ["localhost"] } } })],
		["hosted", http("hosted", { when: { not: { hostGlob: ["localhost"] } } })],
		["raw", http("raw")],
		["off", http("off", { enabled: false })],
	]);
	const order = ["remote", "hosted", "raw"];

	it("follows the configured order", () => {
		const { chain } = selectProviders(providers, order, "https://example.com");
		assert.deepEqual(
			chain.map((p) => p.name),
			["remote", "hosted", "raw"],
		);
	});

	it("drops providers that cannot serve the subject, with a reason", () => {
		const { chain, skipped } = selectProviders(providers, order, "http://localhost:3000");
		assert.deepEqual(
			chain.map((p) => p.name),
			["raw"],
		);
		assert.deepEqual(skipped.map((s) => s.name).sort(), ["hosted", "off", "remote"]);
	});

	it("drops disabled providers", () => {
		const { chain } = selectProviders(providers, [...order, "off"], "https://example.com");
		assert.equal(
			chain.some((p) => p.name === "off"),
			false,
		);
	});

	it("appends providers missing from the order so a dropped-in file still works", () => {
		// Ordered names first, then whatever else was loaded, in load order
		// (providers/ is read alphabetically, so this is stable).
		const { chain } = selectProviders(providers, ["raw"], "https://example.com");
		assert.deepEqual(
			chain.map((p) => p.name),
			["raw", "remote", "hosted"],
		);
	});

	it("takes a forced provider literally, with no fallback", () => {
		const { chain } = selectProviders(providers, order, "https://example.com", "hosted");
		assert.deepEqual(
			chain.map((p) => p.name),
			["hosted"],
		);
	});

	it("forces even a provider whose when-predicate excludes the subject", () => {
		const { chain } = selectProviders(providers, order, "http://localhost:3000", "remote");
		assert.deepEqual(
			chain.map((p) => p.name),
			["remote"],
		);
	});

	it("reports an unknown forced provider instead of silently falling back", () => {
		const { chain, skipped } = selectProviders(providers, order, "https://example.com", "nope");
		assert.deepEqual(chain, []);
		assert.match(skipped[0].reason, /unknown/);
	});
});

describe("interpolateEnv", () => {
	it("substitutes known variables and leaves literals alone", () => {
		const result = interpolateEnv("Bearer $TOKEN", { TOKEN: "secret" });
		assert.equal(result.value, "Bearer secret");
		assert.deepEqual(result.missing, []);
	});

	it("reports missing variables so the rung can be skipped", () => {
		const result = interpolateEnv("Bearer $ABSENT", {});
		assert.deepEqual(result.missing, ["ABSENT"]);
		assert.equal(result.value, "Bearer $ABSENT");
	});

	it("treats an empty variable as missing", () => {
		assert.deepEqual(interpolateEnv("$EMPTY", { EMPTY: "" }).missing, ["EMPTY"]);
	});

	it("leaves a literal key untouched — the documented way to store one", () => {
		const literal = "abc123DEF";
		assert.equal(interpolateEnv(literal, {}).value, literal);
	});
});

describe("interpolate", () => {
	it("fills tokens from the map", () => {
		const tokens = { url: "https://a.com/b?c=1", urlEncoded: "ENC", dir: "/ext" };
		assert.equal(interpolate("https://r/{url}", tokens), "https://r/https://a.com/b?c=1");
		assert.equal(interpolate("{dir}/providers/x.sh", tokens), "/ext/providers/x.sh");
	});

	it("substitutes the longer token first so {urlEncoded} is not eaten by {url}", () => {
		const tokens = subjectTokens("fetch", "https://a.com/b?c=1", "/x");
		assert.equal(interpolate("{urlEncoded}", tokens), "https%3A%2F%2Fa.com%2Fb%3Fc%3D1");
		assert.equal(interpolate("{url}", tokens), "https://a.com/b?c=1");
	});

	it("leaves unknown tokens alone rather than blanking them", () => {
		assert.equal(interpolate("{nope}", { url: "u", dir: "/d" }), "{nope}");
	});
});

describe("subjectTokens", () => {
	it("gives a fetch provider {url} and a search provider {query}", () => {
		const fetchTokens = subjectTokens("fetch", "https://a.com", "/d");
		assert.equal(fetchTokens.url, "https://a.com");
		assert.equal(fetchTokens.query, undefined);

		const searchTokens = subjectTokens("search", "cats and dogs", "/d");
		assert.equal(searchTokens.query, "cats and dogs");
		assert.equal(searchTokens.queryEncoded, "cats%20and%20dogs");
		assert.equal(searchTokens.url, undefined);
	});
});

describe("pickPath", () => {
	it("walks a dot path", () => {
		assert.equal(pickPath({ result: { markdown: "hi" } }, "result.markdown"), "hi");
	});

	it("returns undefined for a missing path instead of throwing", () => {
		assert.equal(pickPath({ result: null }, "result.markdown"), undefined);
		assert.equal(pickPath(undefined, "a.b"), undefined);
	});
});
