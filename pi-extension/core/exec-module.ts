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

const cache = new Map<string, ModuleRunner<unknown>>();

async function loadRunner(spec: string, dir: string): Promise<ModuleRunner<unknown>> {
	const cached = cache.get(spec);
	if (cached) return cached;

	const path = isAbsolute(spec) ? spec : resolve(dir, spec);
	const imported = (await import(pathToFileURL(path).href)) as { default?: unknown };
	const runner = imported.default;
	if (typeof runner !== "function") {
		throw new Error(`${spec} does not default-export a runner function`);
	}
	cache.set(spec, runner as ModuleRunner<unknown>);
	return runner as ModuleRunner<unknown>;
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
