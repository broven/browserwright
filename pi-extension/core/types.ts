/**
 * Shared types for the provider contract.
 *
 * A provider is a declaration, not code. Three kinds are supported:
 *   - "http"    : issue one HTTP request, optionally pluck a field out of JSON
 *   - "command" : run one argv, take stdout
 *   - "module"  : hand off to a TS module in this package — the escape hatch for
 *                 a provider that needs real logic (a session lifecycle, retry,
 *                 progress reporting) rather than one shot at a subprocess.
 *
 * Everything a provider needs to say about itself lives in its JSON file: which
 * tool it serves, what format it returns, when it is applicable, and what its
 * output looks like when it has failed despite reporting success.
 */

/** Which tool a provider serves. A provider belongs to exactly one. */
export type Role = "fetch" | "search";

export const ROLES: readonly Role[] = ["fetch", "search"];

/** What the provider hands back. The core never converts between these. */
export type ReturnFormat = "markdown" | "html" | "text" | "results";

/** One organic row of a `search` provider's payload. */
export interface SearchResult {
	position: number;
	title: string;
	url: string;
	snippet?: string;
	/** Publication date, when the engine states one separately from the snippet. */
	date?: string;
}

/**
 * The engine's own direct answer.
 *
 * `ai-overview` is Google's generated summary. Measured 2026-08-10: its body is
 * **not** in the server-rendered HTML at all — it streams in afterwards — which
 * is the single reason this extractor has to run against the live DOM rather
 * than the document response.
 */
export interface AnswerBox {
	kind: "ai-overview" | "featured-snippet";
	text: string;
}

/** The entity panel: what the engine thinks the query is *about*. */
export interface KnowledgeGraph {
	title?: string;
	subtitle?: string;
	description?: string;
	/** Remaining labelled facts, keyed by the engine's own attribute name. */
	attributes?: Record<string, string>;
}

/**
 * What a `search` provider returns. Organic rows are the contract; everything
 * else is present only when that query happened to trigger it, so every
 * consumer must treat the optional fields as absent by default.
 */
export interface SearchPayload {
	results: SearchResult[];
	answerBox?: AnswerBox;
	knowledgeGraph?: KnowledgeGraph;
	peopleAlsoAsk?: string[];
	relatedSearches?: string[];
}

/**
 * Subject predicate. All fields are optional; an empty match means "always".
 * Globs are matched with `*` = any run of characters (no path semantics).
 *
 * The subject is a URL for `fetch` providers and the query string for `search`
 * providers. `hostGlob` only means anything for the former — it silently never
 * matches when the subject does not parse as a URL.
 */
export interface SubjectMatch {
	/** Glob against the whole subject, e.g. "http://localhost*". */
	urlGlob?: string[];
	/** Glob against the hostname only, e.g. "*.local". URLs only. */
	hostGlob?: string[];
	/** Negation. `{ not: { hostGlob: ["localhost"] } }` = anything but localhost. */
	not?: SubjectMatch;
}

/**
 * When to treat a "successful" call as a failure and drop to the next rung.
 *
 * Per-provider values REPLACE the core defaults field by field; they do not
 * merge. That is deliberate: `{"matches": []}` is how a provider opts out of
 * the default match list entirely, which any provider returning raw HTML needs
 * (a `<noscript>` block legitimately contains "enable JavaScript").
 */
export interface FailWhen {
	/** Fail when the returned text is shorter than this. 0 disables. */
	minChars?: number;
	/** Fail when a list payload has fewer than this many items. 0 disables. */
	minResults?: number;
	/** Case-insensitive substrings that mark the payload as a wall/shell. */
	matches?: string[];
}

interface ProviderCommon {
	name: string;
	/** Which tool this provider serves. Defaults to "fetch". */
	role?: Role;
	/** Human-readable label for status lines. Defaults to `name`. */
	label?: string;
	/** Set false to keep the declaration around without using it. */
	enabled?: boolean;
	returns: ReturnFormat;
	/** Static capability declaration, e.g. "I am remote, I cannot see your LAN". */
	when?: SubjectMatch;
	failWhen?: FailWhen;
	timeoutMs?: number;
	/**
	 * Extra attempts at this rung after a TRANSPORT failure. Default 0.
	 *
	 * Content rejected by `failWhen` is never retried: that verdict is
	 * deterministic, so a second identical call only costs the user time (and,
	 * for a browser rung, another tab).
	 */
	retries?: number;
	/**
	 * Case-insensitive substrings of the failure reason that make a retry
	 * worthwhile. Omit to retry every transport failure. Use it to retry only
	 * what is genuinely transient rather than, say, a 404.
	 */
	retryWhen?: string[];
	/**
	 * Milliseconds to wait between probe cases. A provider's own rate limit is a
	 * static fact about it, like `when`. Probe-only: at runtime a 429 is just
	 * another transport failure that drops a rung.
	 */
	probeDelayMs?: number;
}

export interface HttpProvider extends ProviderCommon {
	kind: "http";
	method?: "GET" | "POST";
	/** Supports {url}/{query}, the {…Encoded} variants, {dir}, and $ENV_VAR. */
	url: string;
	headers?: Record<string, string>;
	/** JSON body; string values support the same interpolation as `url`. */
	body?: unknown;
	/** Dot path into a JSON response, e.g. "result". Omit to use the raw body. */
	pick?: string;
}

export interface CommandProvider extends ProviderCommon {
	kind: "command";
	/**
	 * argv. Supports {url}/{query}, the {…Encoded} variants, and {dir}.
	 * Exit code contract: 0 = success, 2 = not applicable (drop a rung),
	 * anything else = hard error (also drops a rung, but is reported as an error).
	 */
	command: string[];
	cwd?: string;
	env?: Record<string, string>;
}

export interface ModuleProvider extends ProviderCommon {
	kind: "module";
	/** Module path relative to the package directory, e.g. "./providers/foo.ts". */
	module: string;
	/** Passed through to the runner verbatim. */
	options?: Record<string, unknown>;
}

export type Provider = HttpProvider | CommandProvider | ModuleProvider;

/** What a `kind: "module"` runner receives. */
export interface ModuleContext {
	/** The package directory — the same value `{dir}` interpolates to. */
	dir: string;
	timeoutMs: number;
	signal?: AbortSignal;
	options: Record<string, unknown>;
	/**
	 * Report intermediate progress. This is the capability a subprocess cannot
	 * have, and the reason this provider kind exists.
	 */
	onProgress?: (text: string) => void;
}

/** A `kind: "module"` provider's default export. */
export type ModuleRunner<T = unknown> = (subject: string, ctx: ModuleContext) => Promise<ProviderOutcome<T>>;

/** Global config, from config.json. */
export interface PiConfig {
	/** Provider names per role, in the order they are attempted. */
	order: Record<Role, string[]>;
	/** Core default line of defence, overridable per provider. */
	defaultFailWhen: FailWhen;
	timeoutMs: number;
	maxBytes: number;
	maxLines: number;
}

/** One rung attempt, recorded for the chain trace. */
export interface Attempt {
	provider: string;
	ok: boolean;
	ms: number;
	/** Why it failed or was skipped. Absent when ok. */
	reason?: string;
	/** True when the provider was never called (when-predicate or missing env). */
	skipped?: boolean;
}

/** What a provider executor returns. */
export interface ProviderOutcome<T = string> {
	ok: boolean;
	content?: T;
	/** Populated on failure; becomes the Attempt reason. */
	reason?: string;
	/** HTTP status, when the executor knows one. */
	status?: number;
}

export interface ChainResult<T = string> {
	ok: boolean;
	attempts: Attempt[];
	provider?: string;
	format?: ReturnFormat;
	content?: T;
}

/**
 * A payload reduced to the two things `failWhen` can reason about. Supplying
 * one of these per role is what lets a single predicate guard both a Markdown
 * blob and a list of search results.
 */
export interface Inspected {
	/** The text the `matches` needles are searched in. */
	text: string;
	/** Item count, for list payloads. Undefined for text payloads. */
	count?: number;
}

export type Inspector<T> = (value: T) => Inspected;
