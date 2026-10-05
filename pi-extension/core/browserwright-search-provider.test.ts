/**
 * `options.sessionArgs` on the browserwright-search runner: what decides which
 * browser a search runs in, and whether a remote rung stays inert when its
 * endpoint variable is unset.
 *
 * Run: node --test 'core/*.test.ts'
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import runner, { sessionNewArgs } from "../providers/browserwright-search.ts";
import { EXTENSION_DIR, loadProviders } from "./config.ts";
import type { ModuleProvider } from "./types.ts";

describe("browserwright-search sessionArgs", () => {
	it("defaults to the user's own Chrome", () => {
		const resolved = sessionNewArgs({}, {});
		assert.ok(resolved.ok && resolved.args.includes("--backend=extension"));
	});

	it("substitutes $ENV in each element", () => {
		const resolved = sessionNewArgs(
			{ sessionArgs: ["--backend=cdp", "--attach=$BW_REMOTE_CDP", "--name=x"] },
			{ BW_REMOTE_CDP: "https://cdp.test" },
		);
		assert.deepEqual(resolved, { ok: true, args: ["--backend=cdp", "--attach=https://cdp.test", "--name=x"] });
	});

	it("rejects a malformed declaration instead of spawning with it", () => {
		const resolved = sessionNewArgs({ sessionArgs: "--backend=cdp" }, {});
		assert.equal(resolved.ok, false);
	});

	it("leaves the shipped remote rung inert, before any session, when BW_REMOTE_CDP is unset", async () => {
		const remote = loadProviders(EXTENSION_DIR).get("browserwright-search-remote") as ModuleProvider;
		assert.equal(remote.module, "./providers/browserwright-search.ts");
		const saved = process.env.BW_REMOTE_CDP;
		delete process.env.BW_REMOTE_CDP;
		try {
			const outcome = await runner("anything", {
				dir: EXTENSION_DIR,
				timeoutMs: 1000,
				options: remote.options ?? {},
			});
			assert.deepEqual(outcome, { ok: false, reason: "missing env BW_REMOTE_CDP" });
		} finally {
			if (saved !== undefined) process.env.BW_REMOTE_CDP = saved;
		}
	});
});
