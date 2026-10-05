/**
 * The command kind's $ENV handling. A remote rung (`--attach=$BW_REMOTE_CDP`)
 * ships in the default chain, so an unset variable must make it inert — never
 * spawn a process with the literal "$NAME" in its argv.
 *
 * Run: node --test 'core/*.test.ts'
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { execCommand } from "./exec-command.ts";
import type { CommandProvider } from "./types.ts";

/** Prints its own argv as JSON, so the test sees exactly what was spawned. */
function echoArgv(extra: string[]): CommandProvider {
	return {
		name: "echo",
		kind: "command",
		returns: "text",
		command: [process.execPath, "-e", "console.log(JSON.stringify(process.argv.slice(1)))", ...extra],
	};
}

const base = { dir: "/tmp", role: "fetch" as const, timeoutMs: 10_000 };

describe("execCommand $ENV", () => {
	it("reports missing env without spawning anything", async () => {
		const provider = echoArgv(["{url}", "--attach=$BW_TEST_ABSENT"]);
		provider.command[0] = "/nonexistent/should-not-spawn";
		const outcome = await execCommand(provider, "https://e.com", { ...base, env: {} });
		assert.deepEqual(outcome, { ok: false, reason: "missing env BW_TEST_ABSENT" });
	});

	it("substitutes a set variable into argv and leaves a `$` in the subject alone", async () => {
		const outcome = await execCommand(
			echoArgv(["{url}", "--attach=$BW_TEST_CDP"]),
			"https://e.com/?q=$BW_TEST_CDP",
			{ ...base, env: { ...process.env, BW_TEST_CDP: "wss://cdp.test/x" } },
		);
		assert.equal(outcome.ok, true, outcome.reason);
		assert.deepEqual(JSON.parse(outcome.content ?? ""), ["https://e.com/?q=$BW_TEST_CDP", "--attach=wss://cdp.test/x"]);
	});
});
