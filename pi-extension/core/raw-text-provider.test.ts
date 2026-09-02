import assert from "node:assert/strict";
import { describe, it } from "node:test";
import rawText, { isTextContentType } from "../providers/raw-text.ts";
import { EXTENSION_DIR, loadConfig, loadProviders } from "./config.ts";
import type { ModuleContext } from "./types.ts";

const context: ModuleContext = {
	dir: "/tmp",
	timeoutMs: 1000,
	signal: undefined,
	options: {},
};

async function withFetch(
	fetcher: typeof fetch,
	check: () => Promise<void>,
): Promise<void> {
	const original = globalThis.fetch;
	globalThis.fetch = fetcher;
	try {
		await check();
	} finally {
		globalThis.fetch = original;
	}
}

describe("raw text provider", () => {
	it("is registered after browserwright in the shipped fetch chain", () => {
		const config = loadConfig(EXTENSION_DIR);
		const providers = loadProviders(EXTENSION_DIR);
		assert.deepEqual(config.order.fetch, ["browserwright", "raw"]);
		assert.equal(providers.get("raw")?.kind, "module");
		assert.equal(providers.get("raw")?.returns, "text");
	});

	it("recognizes text and supported application content types", () => {
		for (const contentType of [
			"text/plain",
			"text/plain; charset=utf-8",
			"application/json",
			"application/yaml",
			"application/x-yaml",
			"application/javascript",
		]) {
			assert.equal(isTextContentType(contentType), true, contentType);
		}
		for (const contentType of ["application/pdf", "image/png", "application/octet-stream", ""]) {
			assert.equal(isTextContentType(contentType), false, contentType);
		}
	});

	it("returns a raw GitHub-style text response without conversion", async () => {
		let requestedUrl = "";
		await withFetch(
			(async (input, init) => {
				requestedUrl = String(input);
				assert.equal(init?.redirect, "follow");
				return new Response("services:\n  - github\n", {
					status: 200,
					headers: { "content-type": "text/plain; charset=utf-8" },
				});
			}) as typeof fetch,
			async () => {
				const result = await rawText("https://raw.githubusercontent.com/org/repo/main/compose.yml", context);
				assert.deepEqual(result, {
					ok: true,
					content: "services:\n  - github\n",
					status: 200,
				});
			},
		);
		assert.equal(requestedUrl, "https://raw.githubusercontent.com/org/repo/main/compose.yml");
	});

	it("does not decode binary responses as text", async () => {
		let bodyRead = false;
		await withFetch(
			(async () => {
				const response = new Response("not actually decoded", {
					status: 200,
					headers: { "content-type": "application/pdf" },
				});
				const originalText = response.text.bind(response);
				response.text = async () => {
					bodyRead = true;
					return originalText();
				};
				return response;
			}) as typeof fetch,
			async () => {
				const result = await rawText("https://example.com/document.pdf", context);
				assert.equal(result.ok, false);
				assert.match(result.reason ?? "", /Content-Type: application\/pdf/);
				assert.equal(bodyRead, false);
			},
		);
	});

	it("reports HTTP failures after reading a text error body", async () => {
		await withFetch(
			(async () =>
				new Response("not found", {
					status: 404,
					headers: { "content-type": "text/plain" },
				})) as typeof fetch,
			async () => {
				const result = await rawText("https://raw.githubusercontent.com/missing", context);
				assert.deepEqual(result, { ok: false, status: 404, reason: "http 404: not found" });
			},
		);
	});
});
