/**
 * The CLI contract, against a stub `browserwright` on PATH: the argv this
 * package sends, and how a CLI failure reaches the model. The CLI side of the
 * same contract is pinned by the Python e2e tests for `search` / `markdown`.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { BrowserwrightFailure, browserArgs, runBrowserwright } from "./browserwright.ts";

const dir = mkdtempSync(join(tmpdir(), "bw-pi-stub-"));
const stub = join(dir, "browserwright");
// Echoes its argv as JSON, or fails the way the CLI does when STUB_FAIL is set.
writeFileSync(
	stub,
	`#!/usr/bin/env node
if (process.env.STUB_FAIL) {
  process.stderr.write("[WARNING] search: noise\\n");
  process.stderr.write(JSON.stringify({ type: "Captcha", msg: "captcha (unusual traffic) at https://www.google.com/sorry" }) + "\\n");
  process.exit(5);
}
process.stdout.write(JSON.stringify(process.argv.slice(2)));
`,
);
chmodSync(stub, 0o755);
const env = (extra: Record<string, string> = {}) => ({ ...process.env, PATH: `${dir}:${process.env.PATH}`, ...extra });

test("the remote browser is chosen by BW_REMOTE_CDP, and only by it", () => {
	assert.deepEqual(browserArgs({}), []);
	assert.deepEqual(browserArgs({ BW_REMOTE_CDP: "  " }), []);
	assert.deepEqual(browserArgs({ BW_REMOTE_CDP: "https://cdp.example.ts.net" }), ["--attach=https://cdp.example.ts.net"]);
});

test("argv goes through unchanged, with the call deadline appended", async () => {
	const { stdout } = await runBrowserwright(["search", "a b", ...browserArgs({ BW_REMOTE_CDP: "http://h:1" })], {
		env: env(),
		callTimeoutS: 12,
	});
	assert.deepEqual(JSON.parse(stdout), ["search", "a b", "--attach=http://h:1", "--timeout=12"]);
});

test("a CLI failure becomes a thrown sentence from its error envelope", async () => {
	await assert.rejects(runBrowserwright(["search", "q"], { env: env({ STUB_FAIL: "1" }) }), (err: unknown) => {
		assert.ok(err instanceof BrowserwrightFailure);
		assert.equal(err.message, "Captcha: captcha (unusual traffic) at https://www.google.com/sorry");
		return true;
	});
});

test("a missing CLI says so", async () => {
	await assert.rejects(runBrowserwright(["search", "q"], { bin: join(dir, "nope") }), /could not run browserwright: .*is it on PATH/);
});

test("an already-aborted call spawns nothing", async () => {
	const controller = new AbortController();
	controller.abort();
	await assert.rejects(runBrowserwright(["search", "q"], { env: env(), signal: controller.signal }), /aborted/);
});
