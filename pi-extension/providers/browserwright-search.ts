/**
 * The `bw_web_search` rung: drive a real search engine in the user's own Chrome.
 *
 * This is a `kind: "module"` provider rather than a `kind: "command"` one
 * because a search is not one shot at a subprocess. It is: mint a session,
 * navigate, extract from the live DOM, tear the session down — with a retry in
 * the middle and a guaranteed teardown at the end. A shell script can express
 * that, and one used to (`.retired/browserwright.sh`, 122 lines), which is
 * exactly the experience this file exists to avoid repeating.
 *
 * Six measured behaviours of the executor are designed around here. They are
 * not hypothetical; each one produced a silent wrong answer at least once:
 *
 *  1. Executor stdout is truncated at ~10KB SILENTLY, with exit 0. So the
 *     payload never travels on stdout — the script writes it to a file and
 *     prints only a byte count, which we verify. This mirrors what the
 *     `browserwright markdown` command does internally for the same reason.
 *  2. `sys.exit()` inside the executor KILLS it, and the next call fails with
 *     ExecutorUnavailable. The script therefore never calls it; "no results" is
 *     data that comes back, not an exit status.
 *  3. Chrome serves its own `neterror` page through a *successful* navigation,
 *     so `page.content()` returns an error document rather than raising.
 *  4. The executor can die mid-call. One `session reset` + retry recovers it.
 *  5. browserwright reports errors as one JSON object per line on stderr;
 *     unwrapped here so the chain trace reads as a sentence.
 *  6. A session that is not ended leaks a Chrome tab group into the user's
 *     window, so teardown lives in a `finally` that runs on every path.
 */

import { spawn } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { normalizeSearchPayload } from "../core/results.ts";
import type { ModuleContext, ProviderOutcome, SearchPayload } from "../core/types.ts";

const BIN = "browserwright";
const READY_MARKER = "BW_SEARCH_OK";

/** Error types that mean "the plumbing broke", not "the page said no". */
const TRANSIENT = new Set(["ExecutorUnavailable", "PageBindTimeout", "DaemonUnavailable", "CDPError"]);

interface Run {
	code: number | null;
	stdout: string;
	stderr: string;
}

function run(args: string[], options: { input?: string; signal?: AbortSignal; timeoutMs: number }): Promise<Run> {
	// Adding an "abort" listener to an ALREADY-aborted signal never fires it, so
	// without this check a cancelled call still spawns a process and waits out
	// the full timeout. Teardown deliberately passes no signal so it still runs.
	if (options.signal?.aborted) {
		return Promise.resolve({ code: null, stdout: "", stderr: "aborted" });
	}
	return new Promise((resolve) => {
		const child = spawn(BIN, args, { stdio: ["pipe", "pipe", "pipe"] });
		let stdout = "";
		let stderr = "";
		let settled = false;

		const finish = (result: Run) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			options.signal?.removeEventListener("abort", onAbort);
			resolve(result);
		};

		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			finish({ code: null, stdout, stderr: `${stderr}\ntimeout after ${options.timeoutMs}ms` });
		}, options.timeoutMs);

		const onAbort = () => {
			child.kill("SIGKILL");
			finish({ code: null, stdout, stderr: `${stderr}\naborted` });
		};
		options.signal?.addEventListener("abort", onAbort, { once: true });

		child.stdout.on("data", (chunk) => {
			stdout += chunk;
		});
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.on("error", (error) => finish({ code: null, stdout, stderr: `spawn failed: ${error.message}` }));
		child.on("close", (code) => finish({ code, stdout, stderr }));

		if (options.input !== undefined) child.stdin.end(options.input);
		else child.stdin.end();
	});
}

/**
 * browserwright writes one JSON object per line on stderr for its own errors,
 * but a generic exception arrives as a bare Python traceback and a usage error
 * as plain text. Pull out a sentence whichever shape it is.
 */
export function explain(stderr: string): { message: string; type?: string; retryable?: boolean } {
	const lines = stderr
		.split("\n")
		.map((line) => line.trim())
		.filter(Boolean);

	for (const line of [...lines].reverse()) {
		if (!line.startsWith("{")) continue;
		try {
			const parsed = JSON.parse(line) as { msg?: string; type?: string; retryable?: boolean };
			if (parsed.msg) return { message: parsed.msg, type: parsed.type, retryable: parsed.retryable };
		} catch {
			// Not the envelope — keep scanning older lines.
		}
	}
	return { message: lines.at(-1) ?? "no output" };
}

function isTransient(info: { type?: string; retryable?: boolean; message: string }): boolean {
	if (info.retryable) return true;
	if (info.type && TRANSIENT.has(info.type)) return true;
	return /executor/i.test(info.message);
}

/**
 * The extraction script, run inside the session's executor.
 *
 * Selectors live here rather than in the Python package on purpose: a Google
 * layout change is then a `npm publish` of this package, not a PyPI release of
 * browserwright plus an upgrade on every machine.
 */
function buildScript(query: string, limit: number, outPath: string, searchUrl: string): string {
	// Every extractor is independently guarded. Google restyles constantly, and
	// the failure mode that matters is one changed container silently taking the
	// whole search down with it — organic rows must survive a broken AI-Overview
	// selector, and vice versa.
	const js = `() => {
  const QUERY = ${JSON.stringify(query)}.toLowerCase();
  const attempt = (fn, fallback) => { try { return fn(); } catch (e) { return fallback; } };
  const clean = (s) => (s || '')
    .replace(/\\s*\\bRead more\\s*$/i, '')
    .replace(/\\s*\\bShow more\\s*$/i, '')
    .replace(/\\s+/g, ' ')
    .trim();

  // --- organic rows ------------------------------------------------------
  const results = attempt(() => {
    const rows = [];
    document.querySelectorAll('#search a h3').forEach((h3) => {
      const a = h3.closest('a');
      if (!a || !a.href) return;
      const block = h3.closest('div[data-hveid]');
      let snippet = '';
      if (block) {
        const sn = block.querySelector('div[data-sncf], div[style*="-webkit-line-clamp"]');
        snippet = sn ? sn.innerText : '';
        if (!snippet) {
          const long = (block.innerText || '').split('\\n').filter((s) => s.length > 40);
          snippet = long[0] || '';
        }
      }
      // Google prefixes dated results with "Mar 5, 2025 — ". Promote that to a
      // field so the model can judge freshness without parsing prose.
      let date = null;
      const m = snippet.match(/^([A-Z][a-z]{2} \\d{1,2}, \\d{4})\\s*[—·\\-]\\s*/);
      if (m) { date = m[1]; snippet = snippet.slice(m[0].length); }
      rows.push({ title: clean(h3.innerText), url: a.href, snippet: clean(snippet).slice(0, 400), date });
    });
    return rows;
  }, []);

  // --- answer box / AI Overview -----------------------------------------
  // Class names here are obfuscated and rotate, so anchor on the accessible
  // heading instead and walk up until the container actually holds the body.
  const answerBox = attempt(() => {
    const head = [...document.querySelectorAll('[role="heading"]')]
      .find((e) => (e.innerText || '').trim() === 'AI Overview');
    if (!head) return null;
    let node = head;
    for (let i = 0; i < 8 && node; i++) {
      const t = node.innerText || '';
      if (t.length >= 150) {
        const body = clean(t.replace(/^AI Overview\\s*/, ''))
          .replace(/AI responses may include mistakes.*$/i, '')
          // innerText splices the citation chips into the prose ("Reddit +2").
          // The bare counters are pure noise; the source names are left alone.
          .replace(/\\s\\+\\d+\\b/g, '')
          .trim();
        return body ? { kind: 'ai-overview', text: body.slice(0, 4000) } : null;
      }
      node = node.parentElement;
    }
    return null;
  }, null);

  // --- knowledge panel ---------------------------------------------------
  // data-attrid is semantic markup rather than a styling class, which makes it
  // the one stable hook on this page.
  const knowledgeGraph = attempt(() => {
    const pick = (sel) => {
      const e = document.querySelector('[data-attrid="' + sel + '"]');
      return e ? clean(e.innerText).slice(0, 300) : undefined;
    };
    const title = pick('title');
    const subtitle = pick('subtitle');
    const description = pick('wa:/description') || pick('description');
    const attributes = {};
    document.querySelectorAll('[data-attrid]').forEach((e) => {
      const id = e.getAttribute('data-attrid') || '';
      // Keep only labelled facts: "kc:/…:label" style ids carry real values,
      // the rest are layout containers and image slots.
      if (!/^kc:/.test(id)) return;
      const label = id.split(':').pop() || id;
      // Google renders the label above the value, so innerText yields
      // "Programming language TypeScript" for a key already named
      // programming_language. Drop the echoed label.
      // Built with plain string ops rather than a constructed RegExp: the label
      // is engine-supplied, and a metacharacter in it would either throw (which
      // the guard above would silently swallow) or match the wrong thing.
      const human = label.replace(/_/g, ' ').toLowerCase();
      let text = clean(e.innerText);
      if (text.toLowerCase().startsWith(human)) {
        text = text.slice(human.length).replace(/^[\\s:\\uff1a]+/, '');
      }
      if (!text || text.length > 160) return;
      if (!attributes[label]) attributes[label] = text;
    });
    if (!title && !description && Object.keys(attributes).length === 0) return null;
    const out = { title, subtitle, description };
    if (Object.keys(attributes).length) out.attributes = attributes;
    return out;
  }, null);

  // --- people also ask ---------------------------------------------------
  // data-q also carries the original query on the search box, so drop anything
  // that just echoes what was asked.
  const peopleAlsoAsk = attempt(() => {
    const seen = new Set();
    const out = [];
    document.querySelectorAll('[data-q]').forEach((e) => {
      const q = clean(e.getAttribute('data-q'));
      if (!q || q.toLowerCase() === QUERY) return;
      if (seen.has(q.toLowerCase())) return;
      seen.add(q.toLowerCase());
      out.push(q);
    });
    return out.slice(0, 10);
  }, []);

  // --- related searches --------------------------------------------------
  // Scoped to #botstuff: the same href pattern at the top of the page is
  // Google's own tab bar ("Images", "News", "Past hour"), not a related query.
  const relatedSearches = attempt(() => {
    const bot = document.querySelector('#botstuff');
    if (!bot) return [];
    const seen = new Set();
    const out = [];
    bot.querySelectorAll('a[href*="/search?"]').forEach((a) => {
      const t = clean(a.innerText);
      // Pagination shares this selector: bare page numbers plus the nav labels.
      if (!t || /^\\d+$/.test(t) || t.length < 3 || t.length > 80) return;
      if (/^(next|previous|prev|more results?)$/i.test(t)) return;
      if (t.toLowerCase() === QUERY || seen.has(t.toLowerCase())) return;
      seen.add(t.toLowerCase());
      out.push(t);
    });
    return out.slice(0, 10);
  }, []);

  return { results, answerBox, knowledgeGraph, peopleAlsoAsk, relatedSearches };
}`;

	// json.dumps gives us correctly escaped Python string literals for free, so
	// a query containing quotes or newlines cannot break out of the script.
	return [
		"import json",
		`QUERY = ${JSON.stringify(query)}`,
		`LIMIT = ${limit}`,
		`OUT = ${JSON.stringify(outPath)}`,
		`URL = ${JSON.stringify(searchUrl)}`,
		`JS = ${JSON.stringify(js)}`,
		"payload = {}",
		"try:",
		"    page.goto(URL)",
		"    html = None",
		"    data = None",
		// A search engine can bounce the page right after load (consent, region
		// redirect), which destroys the execution context mid-evaluate. Observed
		// while mapping these selectors; one settle-and-retry clears it.
		"    for _ in range(2):",
		"        try:",
		"            html = page.content()",
		"            data = page.evaluate(JS)",
		"            break",
		"        except Exception as e:",
		'            if "ontext was destroyed" not in str(e):',
		"                raise",
		'            page.wait_for_load_state("domcontentloaded")',
		"            page.wait_for_timeout(800)",
		"    if data is None:",
		'        payload = {"failed": "the page kept navigating; could not read results"}',
		// (3) Chrome's own error document arrives through a successful navigation.
		'    elif "neterror" in html and "error-code" in html:',
		'        payload = {"blocked": "the browser could not reach the search engine"}',
		"    else:",
		"        rows = data.get(\"results\") or []",
		"        payload = {",
		'            "results": rows[:LIMIT],',
		'            "url": page.url,',
		'            "answerBox": data.get("answerBox"),',
		'            "knowledgeGraph": data.get("knowledgeGraph"),',
		'            "peopleAlsoAsk": data.get("peopleAlsoAsk") or [],',
		'            "relatedSearches": data.get("relatedSearches") or [],',
		"        }",
		"        if not rows:",
		// An interstitial parses fine and yields zero rows; say which kind it was.
		"            low = html.lower()",
		'            for needle in ("recaptcha", "unusual traffic", "detected unusual"):',
		"                if needle in low:",
		'                    payload = {"blocked": "search engine returned an interstitial (%s)" % needle}',
		"                    break",
		"except Exception as e:",
		// (2) never sys.exit() — a raise would kill the executor for the next call.
		'    payload = {"failed": "%s: %s" % (type(e).__name__, e)}',
		"data = json.dumps(payload, ensure_ascii=False)",
		'with open(OUT, "w", encoding="utf-8") as fh:',
		"    fh.write(data)",
		// (1) only a byte count travels on stdout, never the payload itself.
		`print(${JSON.stringify(READY_MARKER)}, len(data.encode("utf-8")))`,
	].join("\n");
}

interface Attempted {
	ok: boolean;
	payload?: SearchPayload;
	reason?: string;
	transient?: boolean;
}

async function attempt(sid: string, script: string, outPath: string, ctx: ModuleContext): Promise<Attempted> {
	const exec = await run(["-s", sid, "--code-stdin"], {
		input: script,
		signal: ctx.signal,
		timeoutMs: ctx.timeoutMs,
	});

	if (exec.code !== 0) {
		const info = explain(exec.stderr);
		return { ok: false, reason: info.message, transient: isTransient(info) };
	}

	const marker = exec.stdout.match(new RegExp(`${READY_MARKER}\\s+(\\d+)`));
	if (!marker) {
		return { ok: false, reason: "extraction script produced no completion marker", transient: true };
	}

	let raw: string;
	try {
		raw = readFileSync(outPath, "utf8");
	} catch (error) {
		return { ok: false, reason: `could not read extraction output: ${(error as Error).message}` };
	}

	// (1) The byte count is the whole point of writing to a file: a short read
	// here is the signature of the silent stdout truncation this avoids.
	const expected = Number(marker[1]);
	const actual = Buffer.byteLength(raw, "utf8");
	if (actual !== expected) {
		return { ok: false, reason: `truncated transfer: expected ${expected} bytes, read ${actual}` };
	}

	const body = JSON.parse(raw) as { blocked?: string; failed?: string };
	if (body.blocked) return { ok: false, reason: body.blocked };
	if (body.failed) return { ok: false, reason: body.failed };

	// The script emits exactly the key names normalizeSearchPayload aliases, so
	// the browser rung and a hosted JSON rung land on the same shape.
	return { ok: true, payload: normalizeSearchPayload(body) };
}

const runner = async (query: string, ctx: ModuleContext): Promise<ProviderOutcome<SearchPayload>> => {
	const limit = Number(ctx.options.limit ?? 10);
	const template = String(
		ctx.options.searchUrl ?? "https://www.google.com/search?q={queryEncoded}&hl=en&num={limit}",
	);
	const searchUrl = template
		.replaceAll("{queryEncoded}", encodeURIComponent(query))
		.replaceAll("{limit}", String(limit));

	const outPath = join(tmpdir(), `bw-search-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}.json`);
	const script = buildScript(query, limit, outPath, searchUrl);

	ctx.onProgress?.("opening a search session");
	const created = await run(
		["session", "new", "--backend=extension", "--name=pi-websearch"],
		{ signal: ctx.signal, timeoutMs: ctx.timeoutMs },
	);
	if (created.code !== 0) {
		return { ok: false, reason: explain(created.stderr).message };
	}
	// stdout is the bare session id and nothing else; the human-readable
	// "OK: session N created" goes to stderr. Reading "the last line" of the
	// two streams merged is what the retired wrapper did, and why it was fragile.
	const sid = created.stdout.trim();
	if (!sid) return { ok: false, reason: "session new printed no id" };

	try {
		ctx.onProgress?.(`searching (session ${sid})`);
		let result = await attempt(sid, script, outPath, ctx);

		// (4) One recycle-and-retry. Only for plumbing failures — a page that
		// said no will say no again, and retrying it just costs the user a tab.
		if (!result.ok && result.transient && !ctx.signal?.aborted) {
			ctx.onProgress?.("executor died, recycling and retrying once");
			await run(["session", "reset", sid], { signal: ctx.signal, timeoutMs: 30_000 });
			result = await attempt(sid, script, outPath, ctx);
		}

		if (!result.ok) return { ok: false, reason: result.reason ?? "search failed" };
		return { ok: true, content: result.payload ?? { results: [] } };
	} finally {
		// (6) Always. A leaked session is a tab group left in the user's window.
		await run(["session", "end", `--session=${sid}`], { timeoutMs: 30_000 });
		rmSync(outPath, { force: true });
	}
};

export default runner;
