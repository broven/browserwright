"""Unit checks for the extension title marker snippets.

The tests extract only the marker helpers/scripts from
``chrome-extension/background.js`` and run them in Node with a tiny DOM stub.
They do not load the extension runtime or launch Chrome.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_JS = ROOT / "chrome-extension" / "background.js"
PREFIX = "\U0001f440 "


def _template_const(source: str, name: str) -> str:
    match = re.search(rf"const {name} = `\n(?P<body>.*?)\n`;", source, re.DOTALL)
    assert match is not None, f"{name} template not found"
    return match.group("body")


def _function_decl(source: str, name: str) -> str:
    match = re.search(
        rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n\}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{name} function not found"
    return match.group(0)


def _marker_sources() -> dict[str, str]:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    return {
        "installScript": _template_const(source, "MARKER_INSTALL_SCRIPT"),
        "removeScript": _template_const(source, "MARKER_REMOVE_SCRIPT"),
        "stripMarker": _function_decl(source, "stripMarker"),
    }


def _run_node_probe(payload: dict[str, str]) -> dict[str, object]:
    probe = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const PREFIX = "\u{1F440} ";

function runMarkerScript(script, initialTitle) {
  class Document {}
  Object.defineProperty(Document.prototype, "title", {
    configurable: true,
    enumerable: true,
    get() {
      return this._rawTitle || "";
    },
    set(value) {
      this._rawTitle = String(value);
    },
  });

  class MutationObserver {
    constructor(callback) {
      this.callback = callback;
      this.disconnected = false;
    }
    observe() {
      this.callback();
    }
    disconnect() {
      this.disconnected = true;
    }
  }

  const document = new Document();
  document._rawTitle = initialTitle;
  document.head = { appendChild() {} };
  document.querySelector = () => null;
  document.createElement = () => ({ textContent: "" });
  document.addEventListener = () => {};

  const sandbox = {
    window: {},
    document,
    Document,
    HTMLDocument: Document,
    MutationObserver,
  };
  vm.runInNewContext(script, sandbox);
  return { sandbox, document };
}

vm.runInThisContext(
  `const TITLE_PREFIX = ${JSON.stringify(PREFIX)};\n${input.stripMarker}`,
);

const duplicateInstall = runMarkerScript(
  input.installScript,
  `${PREFIX}${PREFIX}Example`,
);
const cleanInstall = runMarkerScript(input.installScript, "Example");
const setterProbe = runMarkerScript(input.installScript, "Example");
setterProbe.document.title = `${PREFIX}${PREFIX}Next`;
const duplicateRemove = runMarkerScript(
  input.removeScript,
  `${PREFIX}${PREFIX}${PREFIX}Example`,
);
const lifecycle = runMarkerScript(input.installScript, `${PREFIX}${PREFIX}Example`);
lifecycle.document.title = `${PREFIX}${PREFIX}Next`;
vm.runInNewContext(input.removeScript, lifecycle.sandbox);
lifecycle.document.title = "After";

process.stdout.write(JSON.stringify({
  stripSamples: [
    stripMarker(`${PREFIX}${PREFIX}Example`),
    stripMarker(`${PREFIX}${PREFIX}${PREFIX}`),
    stripMarker("Plain"),
    stripMarker(null),
  ],
  duplicateInstallTitle: {
    rawTitle: duplicateInstall.document._rawTitle,
    pageTitle: duplicateInstall.document.title,
  },
  cleanInstallTitle: {
    rawTitle: cleanInstall.document._rawTitle,
    pageTitle: cleanInstall.document.title,
  },
  setterTitle: {
    rawTitle: setterProbe.document._rawTitle,
    pageTitle: setterProbe.document.title,
  },
  duplicateRemoveTitle: {
    rawTitle: duplicateRemove.document._rawTitle,
    pageTitle: duplicateRemove.document.title,
  },
  lifecycleTitle: {
    rawTitle: lifecycle.document._rawTitle,
    pageTitle: lifecycle.document.title,
  },
}));
"""
    proc = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


def test_title_marker_helpers_normalize_and_remove_all_leading_markers() -> None:
    results = _run_node_probe(_marker_sources())

    assert results["stripSamples"] == [
        "Example",
        "",
        "Plain",
        "",
    ]
    assert results["duplicateInstallTitle"] == {
        "rawTitle": f"{PREFIX}Example",
        "pageTitle": "Example",
    }
    assert results["cleanInstallTitle"] == {
        "rawTitle": f"{PREFIX}Example",
        "pageTitle": "Example",
    }
    assert results["setterTitle"] == {
        "rawTitle": f"{PREFIX}Next",
        "pageTitle": "Next",
    }
    assert results["duplicateRemoveTitle"] == {
        "rawTitle": "Example",
        "pageTitle": "Example",
    }
    assert results["lifecycleTitle"] == {
        "rawTitle": "After",
        "pageTitle": "After",
    }
