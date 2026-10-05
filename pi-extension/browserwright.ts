/**
 * The whole contract between this package and the browserwright CLI: build
 * the argv, run it, hand back stdout or a sentence explaining why not.
 *
 * Both tools are one CLI call each (`browserwright search`, `browserwright
 * markdown`); session lifecycle, extraction and teardown all live in the CLI.
 * There is no fallback: `BW_REMOTE_CDP` picks the browser, it does not add one.
 */

import { spawn } from "node:child_process";

/** Mirrors `DEFAULT_CALL_TIMEOUT_S` in the Python package. */
export const DEFAULT_CALL_TIMEOUT_S = 90;

/**
 * Process budget on top of the call deadline. The deadline covers the code;
 * the command also opens and ends a session around it, and an attach to a
 * remote forwarder may queue. The slack lets browserwright report
 * `DeadlineExceeded` (exit 7) itself instead of being SIGKILLed silently.
 */
const SLACK_MS = 60_000;

/** Which browser: the remote one when `BW_REMOTE_CDP` is set, else the user's own Chrome. */
export function browserArgs(env: NodeJS.ProcessEnv = process.env): string[] {
	const remote = env.BW_REMOTE_CDP?.trim();
	return remote ? [`--attach=${remote}`] : [];
}

export function browserLabel(env: NodeJS.ProcessEnv = process.env): string {
	return env.BW_REMOTE_CDP?.trim() ? `remote browser (${env.BW_REMOTE_CDP!.trim()})` : "your Chrome";
}

export interface Ran {
	stdout: string;
	stderr: string;
}

export class BrowserwrightFailure extends Error {}

/**
 * browserwright reports its own errors as one JSON object per line on stderr
 * (`{type, msg, fix}`); anything else is a traceback or a usage line. Pull out
 * a sentence whichever it is.
 */
export function explain(stderr: string, code: number | null): string {
	const lines = stderr
		.split("\n")
		.map((line) => line.trim())
		.filter(Boolean);
	for (const line of [...lines].reverse()) {
		if (!line.startsWith("{")) continue;
		try {
			const parsed = JSON.parse(line) as { type?: string; msg?: string };
			if (parsed.msg) return parsed.type ? `${parsed.type}: ${parsed.msg}` : parsed.msg;
		} catch {
			// not the envelope; keep scanning older lines
		}
	}
	return `${lines.at(-1) ?? "no output"} (exit ${code})`;
}

export function runBrowserwright(
	args: string[],
	options: { signal?: AbortSignal; callTimeoutS?: number; bin?: string; env?: NodeJS.ProcessEnv } = {},
): Promise<Ran> {
	const callTimeoutS = options.callTimeoutS ?? DEFAULT_CALL_TIMEOUT_S;
	const argv = [...args, `--timeout=${callTimeoutS}`];
	const budgetMs = Math.ceil(callTimeoutS * 1000) + SLACK_MS;
	// An "abort" listener on an already-aborted signal never fires.
	if (options.signal?.aborted) return Promise.reject(new BrowserwrightFailure("aborted"));

	return new Promise((resolve, reject) => {
		const child = spawn(options.bin ?? "browserwright", argv, {
			env: options.env ?? process.env,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		let settled = false;
		const settle = (fn: () => void) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			options.signal?.removeEventListener("abort", onAbort);
			fn();
		};
		// SIGTERM, not SIGKILL: the CLI ends its throwaway session in a `finally`.
		const timer = setTimeout(() => {
			child.kill("SIGTERM");
			settle(() => reject(new BrowserwrightFailure(`no answer from browserwright within ${budgetMs / 1000}s`)));
		}, budgetMs);
		const onAbort = () => {
			child.kill("SIGTERM");
			settle(() => reject(new BrowserwrightFailure("aborted")));
		};
		options.signal?.addEventListener("abort", onAbort, { once: true });

		child.stdout.on("data", (chunk) => (stdout += chunk));
		child.stderr.on("data", (chunk) => (stderr += chunk));
		child.on("error", (error) =>
			settle(() => reject(new BrowserwrightFailure(`could not run browserwright: ${error.message} (is it on PATH?)`))),
		);
		child.on("close", (code) =>
			settle(() =>
				code === 0 ? resolve({ stdout, stderr }) : reject(new BrowserwrightFailure(explain(stderr, code))),
			),
		);
	});
}
