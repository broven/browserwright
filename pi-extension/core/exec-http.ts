/**
 * The "http" provider kind: one request, optionally pluck a field out of JSON.
 * Covers jina, both Cloudflare markdown endpoints, and the shape every other
 * hosted reader API happens to have.
 */

import { interpolate, interpolateEnv, pickPath, subjectTokens } from "./predicates.ts";
import { normalizeSearchPayload } from "./results.ts";
import type { HttpProvider, ProviderOutcome, Role } from "./types.ts";

/** Interpolate the subject tokens then $ENV, collecting unset variable names. */
function fill(
	template: string,
	tokens: Record<string, string>,
	env: Record<string, string | undefined>,
	missing: string[],
): string {
	const resolved = interpolateEnv(interpolate(template, tokens), env);
	missing.push(...resolved.missing);
	return resolved.value;
}

function fillBody(
	body: unknown,
	tokens: Record<string, string>,
	env: Record<string, string | undefined>,
	missing: string[],
): unknown {
	if (typeof body === "string") return fill(body, tokens, env, missing);
	if (Array.isArray(body)) return body.map((item) => fillBody(item, tokens, env, missing));
	if (body && typeof body === "object") {
		const out: Record<string, unknown> = {};
		for (const [key, value] of Object.entries(body)) {
			out[key] = fillBody(value, tokens, env, missing);
		}
		return out;
	}
	return body;
}

export async function execHttp(
	provider: HttpProvider,
	subject: string,
	options: {
		dir: string;
		role: Role;
		timeoutMs: number;
		signal?: AbortSignal;
		env?: Record<string, string | undefined>;
	},
): Promise<ProviderOutcome<unknown>> {
	const env = options.env ?? process.env;
	const missing: string[] = [];
	const tokens = subjectTokens(options.role, subject, options.dir);

	const target = fill(provider.url, tokens, env, missing);
	const headers: Record<string, string> = {};
	for (const [key, value] of Object.entries(provider.headers ?? {})) {
		headers[key] = fill(value, tokens, env, missing);
	}
	const body = provider.body === undefined ? undefined : fillBody(provider.body, tokens, env, missing);

	// A provider that references an unset key is not a failure to report, it is
	// a rung that does not exist on this machine. Say so plainly and move on.
	if (missing.length > 0) {
		return { ok: false, reason: `missing env ${[...new Set(missing)].join(", ")}` };
	}

	const timeout = AbortSignal.timeout(provider.timeoutMs ?? options.timeoutMs);
	const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;

	let response: Response;
	try {
		response = await fetch(target, {
			method: provider.method ?? "GET",
			headers,
			body: body === undefined ? undefined : JSON.stringify(body),
			signal,
			redirect: "follow",
		});
	} catch (error) {
		const message = (error as Error).name === "TimeoutError" ? "timeout" : (error as Error).message;
		return { ok: false, reason: message };
	}

	const text = await response.text();

	if (!response.ok) {
		// Include a slice of the body: Cloudflare puts the actionable part
		// ("Authentication error") there, not in the status line.
		const hint = text.slice(0, 200).replace(/\s+/g, " ").trim();
		return { ok: false, status: response.status, reason: `http ${response.status}${hint ? `: ${hint}` : ""}` };
	}

	if (!provider.pick) {
		// A search provider that plucks nothing still has to hand back rows, not
		// the raw body, or the chain would inspect a JSON string as if it were prose.
		if (options.role === "search") {
			try {
				return { ok: true, content: normalizeSearchPayload(JSON.parse(text)), status: response.status };
			} catch {
				return { ok: false, status: response.status, reason: "search provider returned a non-JSON body" };
			}
		}
		return { ok: true, content: text, status: response.status };
	}

	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return { ok: false, status: response.status, reason: `pick "${provider.pick}" needs JSON, got non-JSON body` };
	}

	const picked = pickPath(parsed, provider.pick);
	const errors = (parsed as { errors?: unknown })?.errors;
	const detail = errors ? ` (errors: ${JSON.stringify(errors).slice(0, 200)})` : "";

	// A fetch provider plucks one string; a search provider plucks a list. The
	// old code assumed the former unconditionally, which meant any list-shaped
	// API failed here with a misleading "no string at" before its rows were seen.
	if (options.role === "search") {
		if (!Array.isArray(picked)) {
			return { ok: false, status: response.status, reason: `no array at "${provider.pick}"${detail}` };
		}
		// `pick` names the organic rows; the SERP-feature fields, if the API
		// returned any, still come from the top level of the same body.
		const payload = normalizeSearchPayload(parsed);
		payload.results = normalizeResults(picked);
		return { ok: true, content: payload, status: response.status };
	}

	if (typeof picked !== "string") {
		return { ok: false, status: response.status, reason: `no string at "${provider.pick}"${detail}` };
	}

	return { ok: true, content: picked, status: response.status };
}
