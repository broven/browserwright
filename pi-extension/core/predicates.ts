/**
 * Pure predicate and interpolation helpers.
 *
 * Everything in here is a total function over plain data, which is the whole
 * point: the fallback engine's decisions are the part of this extension that
 * fails silently when it is wrong (a rung that is never tried, or garbage
 * accepted as success), so it is the part that gets unit tests.
 */

import type { FailWhen, Inspected, Inspector, Provider, Role, SearchPayload, SubjectMatch } from "./types.ts";

/** Translate a `*`-glob into a RegExp anchored at both ends. */
export function globToRegExp(glob: string): RegExp {
	const escaped = glob.replace(/[.+^${}()|[\]\\?]/g, "\\$&").replace(/\*/g, ".*");
	return new RegExp(`^${escaped}$`, "i");
}

export function globMatchesAny(value: string, globs: string[]): boolean {
	return globs.some((g) => globToRegExp(g).test(value));
}

function hostOf(subject: string): string {
	try {
		return new URL(subject).hostname;
	} catch {
		return "";
	}
}

/**
 * Evaluate a `when` predicate. An absent or empty match means "applicable".
 * Positive fields are OR-ed together; `not` inverts a nested match.
 *
 * The subject is a URL for fetch providers and the raw query for search ones;
 * `hostGlob` simply never matches in the latter case rather than throwing.
 */
export function matchesSubject(subject: string, match: SubjectMatch | undefined): boolean {
	if (!match) return true;

	const positives: boolean[] = [];
	if (match.urlGlob?.length) positives.push(globMatchesAny(subject, match.urlGlob));
	if (match.hostGlob?.length) positives.push(globMatchesAny(hostOf(subject), match.hostGlob));

	if (match.not && matchesSubject(subject, match.not)) return false;
	if (positives.length === 0) return true;
	return positives.some(Boolean);
}

/**
 * Merge the core default defence with a provider's own, field by field.
 * A field present on the provider REPLACES the default; it does not merge.
 * That is what makes `{"matches": []}` a working opt-out.
 */
export function resolveFailWhen(defaults: FailWhen, own: FailWhen | undefined): FailWhen {
	return {
		minChars: own?.minChars ?? defaults.minChars ?? 0,
		minResults: own?.minResults ?? defaults.minResults ?? 0,
		matches: own?.matches ?? defaults.matches ?? [],
	};
}

/** A text payload: the whole blob is the haystack, and it has no item count. */
export const inspectText: Inspector<string> = (content) => ({ text: content });

/**
 * A search payload. The haystack is result titles and snippets joined —
 * deliberately not the serialized JSON, because needles would then match
 * structural punctuation and field names rather than what the page said.
 *
 * The count is organic rows only. The optional SERP features are bonuses that
 * most queries do not trigger, so counting them would make `minResults` mean
 * something different from one query to the next.
 */
export const inspectSearch: Inspector<SearchPayload> = (payload) => ({
	count: payload.results.length,
	text: payload.results.map((r) => `${r.title} ${r.snippet ?? ""}`).join("\n"),
});

/**
 * Decide whether a payload that a provider reported as successful should still
 * be rejected. Returns a human-readable reason, or undefined to accept.
 *
 * `inspect` reduces the payload to text plus an optional item count, so the
 * same rule set guards a Markdown blob and a list of search results.
 */
export function failureReason<T>(value: T, rule: FailWhen, inspect: Inspector<T>): string | undefined {
	const inspected: Inspected = inspect(value);
	const trimmed = inspected.text.trim();
	const count = inspected.count;

	if (count !== undefined) {
		// A search that parsed cleanly but found nothing is a failure worth
		// falling through on: an interstitial usually yields a valid, empty list
		// rather than an error.
		if (count === 0) return "no results";
		const minResults = rule.minResults ?? 0;
		if (minResults > 0 && count < minResults) {
			return `too few results (${count} < ${minResults})`;
		}
	} else {
		if (trimmed.length === 0) return "empty response";
		// minChars measures a prose blob, so it is deliberately not applied to a
		// list: the floor that means anything there is minResults. Applying both
		// would let a short-but-complete set of hits be rejected for its length.
		const min = rule.minChars ?? 0;
		if (min > 0 && trimmed.length < min) {
			return `too short (${trimmed.length} chars < ${min})`;
		}
	}

	const haystack = trimmed.toLowerCase();
	for (const needle of rule.matches ?? []) {
		if (!needle) continue;
		if (haystack.includes(needle.toLowerCase())) return `matched ${JSON.stringify(needle)}`;
	}

	return undefined;
}

/**
 * Substitute $ENV_VAR references from `env`. Literal values pass through
 * untouched, which is the documented way to put an API key straight into a
 * provider declaration.
 *
 * Returns the missing variable names so a provider that references an unset
 * key can be skipped rather than sending the literal string "$TOKEN".
 */
export function interpolateEnv(
	value: string,
	env: Record<string, string | undefined>,
): { value: string; missing: string[] } {
	const missing: string[] = [];
	const out = value.replace(/\$([A-Z0-9_]+)/g, (whole, name: string) => {
		const found = env[name];
		if (found === undefined || found === "") {
			missing.push(name);
			return whole;
		}
		return found;
	});
	return { value: out, missing };
}

/**
 * Fill {token} placeholders. Longer names are substituted first so that
 * {urlEncoded} wins over {url} — otherwise the shorter token would eat its
 * own prefix and leave a stray "Encoded" behind.
 */
export function interpolate(template: string, tokens: Record<string, string>): string {
	let out = template;
	for (const key of Object.keys(tokens).sort((a, b) => b.length - a.length)) {
		out = out.replaceAll(`{${key}}`, tokens[key]);
	}
	return out;
}

/**
 * The tokens a provider of this role may use. A fetch provider gets {url};
 * a search provider gets {query}. Both get the encoded variant and {dir}.
 */
export function subjectTokens(role: Role, subject: string, dir: string): Record<string, string> {
	const key = role === "search" ? "query" : "url";
	return {
		[key]: subject,
		[`${key}Encoded`]: encodeURIComponent(subject),
		dir,
	};
}

/** A provider's role, defaulting to fetch so an older declaration still loads. */
export function roleOf(provider: Provider): Role {
	return provider.role ?? "fetch";
}

export function providersForRole(providers: Map<string, Provider>, role: Role): Map<string, Provider> {
	return new Map([...providers].filter(([, provider]) => roleOf(provider) === role));
}

/**
 * Order the providers for one request: config order first, then drop the
 * disabled ones and the ones whose `when` says they cannot serve this subject.
 *
 * When `forced` is set, only that provider is returned and no fallback
 * happens — an explicit provider choice from the model is taken literally.
 */
export function selectProviders(
	providers: Map<string, Provider>,
	order: string[],
	subject: string,
	forced?: string,
): { chain: Provider[]; skipped: Array<{ name: string; reason: string }> } {
	const skipped: Array<{ name: string; reason: string }> = [];

	if (forced) {
		const one = providers.get(forced);
		if (!one) return { chain: [], skipped: [{ name: forced, reason: "unknown provider" }] };
		return { chain: [one], skipped };
	}

	const names = [...order, ...[...providers.keys()].filter((n) => !order.includes(n))];
	const chain: Provider[] = [];

	for (const name of names) {
		const provider = providers.get(name);
		if (!provider) continue;
		if (provider.enabled === false) {
			skipped.push({ name, reason: "disabled" });
			continue;
		}
		if (!matchesSubject(subject, provider.when)) {
			skipped.push({ name, reason: "not applicable to this subject" });
			continue;
		}
		chain.push(provider);
	}

	return { chain, skipped };
}

/** Dot path lookup, e.g. pick("result.markdown") on a parsed JSON body. */
export function pickPath(body: unknown, path: string): unknown {
	let cursor: unknown = body;
	for (const segment of path.split(".")) {
		if (cursor === null || typeof cursor !== "object") return undefined;
		cursor = (cursor as Record<string, unknown>)[segment];
	}
	return cursor;
}
