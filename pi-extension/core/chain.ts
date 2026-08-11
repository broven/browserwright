/**
 * The fallback engine.
 *
 * Walks the selected providers in order, and treats a provider as failed both
 * when it errors and when it succeeds with a payload that its own failWhen rule
 * rejects. The executor is injected so this whole file is testable without a
 * network or a browser.
 *
 * The engine is payload-agnostic: it never looks inside `content`, it only asks
 * the caller-supplied `inspect` to reduce it to text plus an item count. That is
 * what lets one chain serve `bw_web_fetch` (a Markdown blob) and `bw_web_search`
 * (a list of results).
 */

import { execCommand } from "./exec-command.ts";
import { execHttp } from "./exec-http.ts";
import { execModule } from "./exec-module.ts";
import { failureReason, providersForRole, resolveFailWhen, selectProviders } from "./predicates.ts";
import type { Attempt, ChainResult, Inspector, PiConfig, Provider, ProviderOutcome, Role } from "./types.ts";

export type Executor<T> = (provider: Provider, subject: string) => Promise<ProviderOutcome<T>>;

export interface RunChainOptions<T> {
	providers: Map<string, Provider>;
	config: PiConfig;
	/** Which tool is asking. Selects both the provider set and the order. */
	role: Role;
	/** A URL for fetch, the raw query for search. */
	subject: string;
	/** Reduces a payload to what failWhen can reason about. */
	inspect: Inspector<T>;
	/** Explicit provider from the model. Disables fallback. */
	forced?: string;
	executor: Executor<T>;
	/** Called before each attempt so the caller can show a status line. */
	onAttempt?: (provider: Provider, index: number, total: number) => void;
}

async function callOnce<T>(
	executor: Executor<T>,
	provider: Provider,
	subject: string,
): Promise<ProviderOutcome<T>> {
	try {
		return await executor(provider, subject);
	} catch (error) {
		return { ok: false, reason: (error as Error).message };
	}
}

/** No `retryWhen` means "any transport failure is worth one more go". */
function shouldRetry(retryWhen: string[] | undefined, reason: string | undefined): boolean {
	if (!retryWhen || retryWhen.length === 0) return true;
	const haystack = (reason ?? "").toLowerCase();
	return retryWhen.some((needle) => needle && haystack.includes(needle.toLowerCase()));
}

export async function runChain<T>(options: RunChainOptions<T>): Promise<ChainResult<T>> {
	const { config, role, subject, forced, executor, inspect } = options;
	const eligible = providersForRole(options.providers, role);
	const { chain, skipped } = selectProviders(eligible, config.order[role] ?? [], subject, forced);

	const attempts: Attempt[] = skipped.map((entry) => ({
		provider: entry.name,
		ok: false,
		ms: 0,
		reason: entry.reason,
		skipped: true,
	}));

	for (const [index, provider] of chain.entries()) {
		options.onAttempt?.(provider, index, chain.length);
		const startedAt = performance.now();
		let outcome = await callOnce(executor, provider, subject);

		// Transport-level retry, before the content gate. browserwright reports
		// some failures as explicitly `retryable` (a target that vanished between
		// binding and use); with one rung per tool there is nothing to fall
		// through to, so not retrying turns a blip into a failed call.
		let remaining = provider.retries ?? 0;
		while (!outcome.ok && remaining > 0 && shouldRetry(provider.retryWhen, outcome.reason)) {
			remaining -= 1;
			options.onAttempt?.(provider, index, chain.length);
			outcome = await callOnce(executor, provider, subject);
		}

		const ms = performance.now() - startedAt;

		if (!outcome.ok || outcome.content === undefined) {
			attempts.push({ provider: provider.name, ok: false, ms, reason: outcome.reason ?? "failed" });
			continue;
		}

		// Succeeded at the transport level — now apply this provider's own
		// line of defence to the payload it returned.
		const rule = resolveFailWhen(config.defaultFailWhen, provider.failWhen);
		const rejected = failureReason(outcome.content, rule, inspect);
		if (rejected) {
			attempts.push({ provider: provider.name, ok: false, ms, reason: rejected });
			continue;
		}

		attempts.push({ provider: provider.name, ok: true, ms });
		return {
			ok: true,
			attempts,
			provider: provider.name,
			format: provider.returns,
			content: outcome.content,
		};
	}

	return { ok: false, attempts };
}

export interface ExecutorOptions {
	dir: string;
	role: Role;
	signal?: AbortSignal;
	/** Forwarded to `kind: "module"` runners, which are the only ones that stream. */
	onProgress?: (text: string) => void;
}

/** Wire the real executors. Kept separate so tests can skip it entirely. */
export function makeExecutor<T>(config: PiConfig, options: ExecutorOptions): Executor<T> {
	const { dir, role, signal, onProgress } = options;
	return async (provider, subject) => {
		const shared = { dir, role, timeoutMs: config.timeoutMs, signal };
		if (provider.kind === "http") {
			return (await execHttp(provider, subject, shared)) as ProviderOutcome<T>;
		}
		if (provider.kind === "module") {
			return await execModule<T>(provider, subject, { ...shared, onProgress });
		}
		return (await execCommand(provider, subject, shared)) as ProviderOutcome<T>;
	};
}
