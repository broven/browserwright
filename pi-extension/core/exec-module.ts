/**
 * The "module" provider kind: hand off to a TS module inside this package.
 *
 * This is the escape hatch for a provider that cannot be expressed as one shot
 * at a subprocess — one that owns a multi-step lifecycle, retries on its own,
 * or reports progress while it works. A `kind: "command"` provider gets exactly
 * one process and one exit code; a module gets the event loop.
 *
 * The contract is deliberately the same as the other two kinds from the
 * engine's point of view: it returns a ProviderOutcome and never throws past
 * this file, so a broken runner drops a rung instead of failing the whole call.
 *
 * Cancellation is cooperative. Unlike a subprocess there is nothing to SIGKILL,
 * so the runner is handed an AbortSignal that fires on either the caller's
 * abort or the timeout, and is expected to unwind its own resources. The race
 * below only bounds how long the engine waits — a runner that ignores its
 * signal will keep running in the background, which is why every runner in this
 * package cleans up in a `finally`.
 */

import { pathToFileURL } from "node:url";
import { isAbsolute, resolve } from "node:path";
import type { ModuleContext, ModuleProvider, ModuleRunner, ProviderOutcome, Role } from "./types.ts";

/**
 * Completed and in-flight loads, keyed by spec.
 *
 * This stores the PROMISE, not the resolved runner, on purpose. Two bw_web_search
 * calls fired in the same turn both miss a value cache and both import; under
 * pi's jiti runtime that race is not merely wasteful but wrong — the second
 * caller can observe a half-initialized module record and fail the load (see
 * the mechanism note above loadRunner). A promise cache collapses the second
 * caller onto the first's in-flight import, so everyone awaits the same
 * import and sees the same resolved runner.
 */
const cache = new Map<string, Promise<ModuleRunner<unknown>>>();

/**
 * How a module runner is loaded. Injectable for tests; production always uses
 * the native dynamic import, which pi's jiti runtime routes through its own
 * transpile-and-cache pipeline.
 *
 * Why the unwrap (measured, not hypothetical): jiti transpiles .ts modules to
 * CommonJS and rewrites every static import into a top-level
 * `await jitiImport(...)` inside an async wrapper. While the module body is
 * suspended on those awaits, the module record is already in jiti's shared
 * per-chain cache with `exports.default` still unset. A concurrent import of
 * the same module then hits that half-initialized record, and jiti's
 * interopDefault proxy surfaces the RAW exports object (`{ default: runner,
 * explain }`) as the module's `.default` — which is not a function. Observable
 * as an intermittent "does not default-export a runner function" on the first
 * concurrent call, never again once cached. The promise cache above removes
 * the race; this unwrap is belt-and-braces for any residual interop quirk
 * (e.g. an environment forcing JITI_INTEROP_DEFAULT=false).
 */
export type ModuleImporter = (href: string) => Promise<{ default?: unknown }>;

const defaultImporter: ModuleImporter = (href) => import(href) as Promise<{ default?: unknown }>;

export async function loadRunner(
	spec: string,
	dir: string,
	importer: ModuleImporter = defaultImporter,
): Promise<ModuleRunner<unknown>> {
	const cached = cache.get(spec);
	if (cached) return cached;

	const path = isAbsolute(spec) ? spec : resolve(dir, spec);
	const load = (async () => {
		const imported = (await importer(pathToFileURL(path).href)) as { default?: unknown };
		const direct = imported.default as unknown;
		const runner =
			typeof direct === "function"
				? (direct as ModuleRunner<unknown>)
				: ((direct as { default?: unknown } | null)?.default as unknown);
		if (typeof runner !== "function") {
			throw new Error(`${spec} does not default-export a runner function`);
		}
		return runner as ModuleRunner<unknown>;
	})();

	cache.set(spec, load);
	try {
		return await load;
	} catch (error) {
		// A failed load must not poison the cache forever: drop it so the next
		// call re-imports. Both callers of a shared rejected promise reach here
		// (delete is idempotent).
		cache.delete(spec);
		throw error;
	}
}

export async function execModule<T>(
	provider: ModuleProvider,
	subject: string,
	options: {
		dir: string;
		role: Role;
		timeoutMs: number;
		signal?: AbortSignal;
		onProgress?: (text: string) => void;
	},
): Promise<ProviderOutcome<T>> {
	if (!provider.module) return { ok: false, reason: "module provider has no `module` path" };

	if (options.signal?.aborted) return { ok: false, reason: "aborted" };

	const timeoutMs = provider.timeoutMs ?? options.timeoutMs;
	const controller = new AbortController();
	const onOuterAbort = () => controller.abort();
	// Registering on an already-aborted signal never fires, so the check above is
	// what actually handles "cancelled before we started".
	options.signal?.addEventListener("abort", onOuterAbort, { once: true });

	let timer: NodeJS.Timeout | undefined;
	const deadline = new Promise<ProviderOutcome<T>>((res) => {
		timer = setTimeout(() => {
			controller.abort();
			res({ ok: false, reason: `timeout after ${timeoutMs}ms` });
		}, timeoutMs);
	});

	try {
		const runner = await loadRunner(provider.module, options.dir);
		const ctx: ModuleContext = {
			dir: options.dir,
			timeoutMs,
			signal: controller.signal,
			options: provider.options ?? {},
			onProgress: options.onProgress,
		};
		const running = (async () => {
			try {
				return (await runner(subject, ctx)) as ProviderOutcome<T>;
			} catch (error) {
				return { ok: false, reason: (error as Error).message } as ProviderOutcome<T>;
			}
		})();
		return await Promise.race([running, deadline]);
	} catch (error) {
		// Only reachable from loadRunner — a missing file or a bad default export.
		return { ok: false, reason: `module load failed: ${(error as Error).message}` };
	} finally {
		if (timer) clearTimeout(timer);
		options.signal?.removeEventListener("abort", onOuterAbort);
	}
}
