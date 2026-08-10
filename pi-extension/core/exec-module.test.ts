/**
 * loadRunner concurrency and interop tests.
 *
 * The importer is injected, so nothing here touches jiti, the network or a
 * browser. The concurrency case is the regression test for the intermittent
 * "does not default-export a runner function" seen when two web_search calls
 * raced the FIRST module load under pi's jiti runtime: a promise cache makes
 * the second caller share the first's in-flight import instead of importing a
 * half-initialized module record. See the mechanism note above loadRunner in
 * exec-module.ts.
 *
 * Each test uses its own spec string: the module-level cache is shared across
 * tests in this file, and a warm cache would silently neuter the cases that
 * are supposed to exercise the miss path.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { loadRunner, type ModuleImporter } from "./exec-module.ts";

const DIR = "/tmp";

const aRunner = () => "ran";

describe("loadRunner", () => {
	it("deduplicates concurrent loads: one import, one shared runner", async () => {
		const spec = "./fixtures/concurrent.ts";
		let imports = 0;
		let release!: () => void;
		const gate = new Promise<void>((resolve) => (release = resolve));
		const importer: ModuleImporter = async () => {
			imports++;
			await gate;
			return { default: aRunner };
		};

		const first = loadRunner(spec, DIR, importer);
		const second = loadRunner(spec, DIR, importer);

		// Both calls have started; only one may have reached the importer.
		await new Promise((resolve) => setTimeout(resolve, 5));
		assert.equal(imports, 1, "concurrent callers must share one in-flight import");

		release();
		const [a, b] = await Promise.all([first, second]);
		assert.equal(a, aRunner);
		assert.equal(b, aRunner);
	});

	it("drops the cache entry on load failure so the next call retries", async () => {
		const spec = "./fixtures/failure.ts";
		let calls = 0;
		const importer: ModuleImporter = async () => {
			calls++;
			if (calls === 1) throw new Error("first load exploded");
			return { default: aRunner };
		};

		await assert.rejects(loadRunner(spec, DIR, importer), /first load exploded/);
		const runner = await loadRunner(spec, DIR, importer);
		assert.equal(runner, aRunner);
		assert.equal(calls, 2, "a failed load must not be cached forever");
	});

	it("unwraps the raw CJS exports shape jiti can surface as default", async () => {
		const spec = "./fixtures/exports-shape.ts";
		const importer: ModuleImporter = async () => ({
			default: { default: aRunner, explain: () => "explain" },
		});
		const runner = await loadRunner(spec, DIR, importer);
		assert.equal(runner, aRunner);
	});

	it("rejects when default is not a runner in any shape", async () => {
		const spec = "./fixtures/not-a-runner.ts";
		const importer: ModuleImporter = async () => ({
			default: { explain: () => "explain" },
		});
		await assert.rejects(
			loadRunner(spec, DIR, importer),
			/does not default-export a runner function/,
		);
	});
});
