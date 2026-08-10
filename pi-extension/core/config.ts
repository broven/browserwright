/**
 * Config and provider loading.
 *
 * Provider declarations are plain JSON files in providers/. Adding a provider
 * means dropping one file in there — no code change, no registration table.
 * A declaration that names no `role` serves `web_fetch`, which is what the
 * majority of reader APIs are.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ROLES, type PiConfig, type Provider, type Role } from "./types.ts";

/**
 * The package's own directory, used for {dir} and for resolving module
 * providers. Two dirnames up from here, so this file must stay exactly one
 * directory below the package root — everything else hangs off this value.
 */
export const EXTENSION_DIR = dirname(dirname(fileURLToPath(import.meta.url)));

const CONFIG_FILE = "config.json";
const LOG_PREFIX = "[browserwright-pi]";

const DEFAULT_CONFIG: PiConfig = {
	order: {
		fetch: ["browserwright"],
		search: ["browserwright-search"],
	},
	// The default line of defence. minChars stays 0 on purpose: a false positive
	// escalates to a rung that opens a tab in the user's real Chrome, so
	// over-eager rejection interrupts them. Per-provider thresholds are meant to
	// come from `/browserwright probe` evidence, not from guesses.
	defaultFailWhen: {
		minChars: 0,
		minResults: 0,
		matches: ["enable javascript", "just a moment", "checking your browser", "captcha"],
	},
	timeoutMs: 30_000,
	maxBytes: 50 * 1024,
	maxLines: 2000,
};

function readJson<T>(path: string): T | undefined {
	try {
		return JSON.parse(readFileSync(path, "utf8")) as T;
	} catch (error) {
		console.error(`${LOG_PREFIX} failed to read ${path}: ${(error as Error).message}`);
		return undefined;
	}
}

export function loadConfig(dir: string = EXTENSION_DIR): PiConfig {
	const path = join(dir, CONFIG_FILE);
	if (!existsSync(path)) return DEFAULT_CONFIG;
	const raw = readJson<Partial<PiConfig>>(path) ?? {};
	return {
		...DEFAULT_CONFIG,
		...raw,
		// Merged per role rather than replaced wholesale, so overriding one
		// tool's order does not silently blank the other's.
		order: { ...DEFAULT_CONFIG.order, ...(raw.order ?? {}) },
		defaultFailWhen: { ...DEFAULT_CONFIG.defaultFailWhen, ...(raw.defaultFailWhen ?? {}) },
	};
}

export function loadProviders(dir: string = EXTENSION_DIR): Map<string, Provider> {
	const providerDir = join(dir, "providers");
	const providers = new Map<string, Provider>();
	if (!existsSync(providerDir)) return providers;

	for (const file of readdirSync(providerDir).sort()) {
		// *.probe.json holds probe evidence, not declarations. Runners for
		// module providers live here too and are imported, never loaded as JSON.
		if (!file.endsWith(".json") || file.endsWith(".probe.json")) continue;
		const declared = readJson<Provider>(join(providerDir, file));
		if (!declared) continue;
		if (!declared.name || !declared.kind || !declared.returns) {
			console.error(`${LOG_PREFIX} ${file}: needs name, kind and returns — skipped`);
			continue;
		}
		if (declared.role && !ROLES.includes(declared.role as Role)) {
			console.error(`${LOG_PREFIX} ${file}: unknown role ${JSON.stringify(declared.role)} — skipped`);
			continue;
		}
		providers.set(declared.name, declared);
	}

	return providers;
}
