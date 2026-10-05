/**
 * The "command" provider kind: run one argv, take stdout.
 *
 * Exit code contract (the whole reason a command can participate in the chain
 * without the core knowing anything about it):
 *   0        success, stdout is the content
 *   2        not applicable — drop to the next rung, this is not an error
 *   anything else  hard error — also drops a rung, but is reported as an error
 *
 * The provider script is the thing that understands its own tool, so it owns
 * the "is this page actually empty" judgement and signals it with exit 2.
 *
 * `{timeout}` interpolates to the caller's call deadline in seconds (or
 * browserwright's 90s default), for a command that forwards it as `--timeout`.
 */

import { spawn } from "node:child_process";
import { DEFAULT_CALL_TIMEOUT_S, withCallDeadline } from "./config.ts";
import { fillTemplate, missingEnvReason, subjectTokens } from "./predicates.ts";
import type { CommandProvider, ProviderOutcome, Role } from "./types.ts";

export async function execCommand(
	provider: CommandProvider,
	subject: string,
	options: {
		dir: string;
		role: Role;
		timeoutMs: number;
		signal?: AbortSignal;
		env?: Record<string, string | undefined>;
		callTimeoutS?: number;
	},
): Promise<ProviderOutcome<string>> {
	const env = options.env ?? process.env;
	const tokens = {
		...subjectTokens(options.role, subject, options.dir),
		timeout: String(options.callTimeoutS ?? DEFAULT_CALL_TIMEOUT_S),
	};
	const missing: string[] = [];
	const fill = (template: string) => {
		const resolved = fillTemplate(template, tokens, env);
		missing.push(...resolved.missing);
		return resolved.value;
	};
	const argv = provider.command.map(fill);
	const cwd = provider.cwd ? fill(provider.cwd) : options.dir;
	if (argv.length === 0) return { ok: false, reason: "empty command" };
	// Same rule as the http kind: an argv that names an unset variable is a rung
	// this machine does not have, not a process worth spawning with "$NAME" in it.
	if (missing.length > 0) return { ok: false, reason: missingEnvReason(missing) };

	const [bin, ...args] = argv;
	const timeoutMs = withCallDeadline(provider.timeoutMs ?? options.timeoutMs, options.callTimeoutS);

	return await new Promise<ProviderOutcome<string>>((resolve) => {
		let settled = false;
		const finish = (outcome: ProviderOutcome<string>) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			options.signal?.removeEventListener("abort", onAbort);
			resolve(outcome);
		};

		const child = spawn(bin, args, {
			cwd,
			env: { ...env, ...(provider.env ?? {}) },
			stdio: ["ignore", "pipe", "pipe"],
		});

		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			finish({ ok: false, reason: `timeout after ${timeoutMs}ms` });
		}, timeoutMs);

		const onAbort = () => {
			child.kill("SIGKILL");
			finish({ ok: false, reason: "aborted" });
		};
		options.signal?.addEventListener("abort", onAbort, { once: true });

		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk) => {
			stdout += chunk;
		});
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});

		child.on("error", (error) => {
			finish({ ok: false, reason: `spawn failed: ${error.message}` });
		});

		child.on("close", (code) => {
			if (code === 0) return finish({ ok: true, content: stdout });
			if (code === 2) {
				const why = stderr.trim().split("\n").pop() ?? "";
				return finish({ ok: false, reason: `not applicable${why ? `: ${why}` : ""}` });
			}
			const detail = (stderr.trim() || stdout.trim()).split("\n").pop() ?? "";
			finish({ ok: false, reason: `exit ${code}${detail ? `: ${detail.slice(0, 200)}` : ""}` });
		});
	});
}
