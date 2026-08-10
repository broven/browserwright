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
 */

import { spawn } from "node:child_process";
import { interpolate, subjectTokens } from "./predicates.ts";
import type { CommandProvider, ProviderOutcome, Role } from "./types.ts";

export async function execCommand(
	provider: CommandProvider,
	subject: string,
	options: { dir: string; role: Role; timeoutMs: number; signal?: AbortSignal },
): Promise<ProviderOutcome<string>> {
	const tokens = subjectTokens(options.role, subject, options.dir);
	const argv = provider.command.map((part) => interpolate(part, tokens));
	if (argv.length === 0) return { ok: false, reason: "empty command" };

	const [bin, ...args] = argv;
	const timeoutMs = provider.timeoutMs ?? options.timeoutMs;

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
			cwd: provider.cwd ? interpolate(provider.cwd, tokens) : options.dir,
			env: { ...process.env, ...(provider.env ?? {}) },
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
