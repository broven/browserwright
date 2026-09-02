/**
 * The raw text fetch rung.
 *
 * `browserwright markdown` deliberately only converts HTML. Public source
 * endpoints such as raw.githubusercontent.com return the source as text/plain,
 * so they need a reader that returns the response body verbatim instead of
 * trying to turn a browser's non-HTML document into Markdown.
 */

import type { ModuleContext, ProviderOutcome } from "../core/types.ts";

/**
 * MIME types whose response body is safe to hand back as text.
 *
 * `text/*` covers source files served as text/plain. The application types
 * cover APIs and source/document formats that are commonly served without a
 * text/* MIME type. Binary responses such as application/pdf and
 * application/octet-stream are intentionally not decoded here.
 */
const TEXT_APPLICATION_TYPES = new Set([
	"application/graphql",
	"application/javascript",
	"application/json",
	"application/ld+json",
	"application/manifest+json",
	"application/sql",
	"application/toml",
	"application/typescript",
	"application/xml",
	"application/x-javascript",
	"application/x-yaml",
	"application/yaml",
]);

export function isTextContentType(raw: string | null | undefined): boolean {
	const contentType = (raw ?? "").split(";", 1)[0].trim().toLowerCase();
	return contentType.startsWith("text/") || TEXT_APPLICATION_TYPES.has(contentType);
}

export default async function rawText(
	subject: string,
	ctx: ModuleContext,
): Promise<ProviderOutcome<string>> {
	let response: Response;
	try {
		response = await fetch(subject, {
			redirect: "follow",
			signal: ctx.signal,
		});
	} catch (error) {
		if (ctx.signal?.aborted) return { ok: false, reason: "aborted" };
		return { ok: false, reason: `fetch failed: ${(error as Error).message}` };
	}

	const contentType = response.headers.get("content-type") ?? "";
	if (!isTextContentType(contentType)) {
		const type = contentType.split(";", 1)[0].trim() || "unknown";
		return {
			ok: false,
			status: response.status,
			reason: `not a text response (Content-Type: ${type})`,
		};
	}

	let text: string;
	try {
		text = await response.text();
	} catch (error) {
		return { ok: false, status: response.status, reason: `could not read response: ${(error as Error).message}` };
	}

	if (!response.ok) {
		const hint = text.slice(0, 200).replace(/\s+/g, " ").trim();
		return { ok: false, status: response.status, reason: `http ${response.status}${hint ? `: ${hint}` : ""}` };
	}

	return { ok: true, content: text, status: response.status };
}
