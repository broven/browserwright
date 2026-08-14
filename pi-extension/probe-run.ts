/**
 * Headless probe runner — the same code path as `/bw probe`, without pi's
 * confirmation dialog. Use it when you want the evidence matrix captured to a
 * file rather than rendered into a session.
 *
 *   node probe-run.ts                 # every provider
 *   node probe-run.ts jina curl       # only these
 */

import { EXTENSION_DIR, loadConfig, loadProviders } from "./core/config.ts";
import { formatProbeReport, loadProbeCases, runProbe, saveProbeEvidence } from "./core/probe.ts";

const config = loadConfig();
const providers = loadProviders();
const cases = loadProbeCases();
const requested = process.argv.slice(2);
const names = requested.length > 0 ? requested : [...providers.keys()];

console.log(`probing ${names.join(", ")} against ${cases.length} real URLs\n`);

for (const name of names) {
	const provider = providers.get(name);
	if (!provider) {
		console.log(`unknown provider: ${name}\n`);
		continue;
	}
	const rows = await runProbe(provider, cases, {
		config,
		dir: EXTENSION_DIR,
		onCase: (probeCase, index) => console.error(`  [${name}] ${index + 1}/${cases.length} ${probeCase.name}`),
	});
	console.log(formatProbeReport(name, rows));
	console.log(`evidence: ${saveProbeEvidence(name, rows)}\n`);
}
