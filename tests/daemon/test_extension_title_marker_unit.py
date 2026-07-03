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
    # The marker scripts are assembled from shared source fragments
    # (MARKER_STRIP_PREFIX_SRC / MARKER_RAW_TITLE_SRC in background.js), so
    # evaluate the whole const region in Node instead of regex-slicing a
    # single template literal.
    start = source.index("const MARKER_STRIP_PREFIX_SRC")
    end = source.index("const markedTabs")
    snippet = source[start:end]
    proc = subprocess.run(
        ["node"],
        input=f"{snippet}\nprocess.stdout.write(JSON.stringify({name}));",
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(proc.stdout)


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
const EYE = "\u{1F440}";
const NBSP = "\u00A0";
const NARROW_NBSP = "\u202F";

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
const bareEyeInstall = runMarkerScript(input.installScript, `${EYE}Hello`);
const nbspEyeInstall = runMarkerScript(input.installScript, `${EYE}${NBSP}Hello`);
const narrowEyeInstall = runMarkerScript(
  input.installScript,
  `${EYE}${NARROW_NBSP}Hello`,
);
const setterProbe = runMarkerScript(input.installScript, "Example");
setterProbe.document.title = `${PREFIX}${PREFIX}Next`;
const bareEyeSetter = runMarkerScript(input.installScript, "Example");
bareEyeSetter.document.title = `${EYE}World`;
const nonStringSetter = runMarkerScript(input.installScript, "Example");
nonStringSetter.document.title = 42;
const duplicateRemove = runMarkerScript(
  input.removeScript,
  `${PREFIX}${PREFIX}${PREFIX}Example`,
);
const bareEyeRemove = runMarkerScript(input.removeScript, `${EYE}Hello`);
const nbspEyeRemove = runMarkerScript(input.removeScript, `${EYE}${NBSP}Hello`);
const lifecycle = runMarkerScript(input.installScript, `${PREFIX}${PREFIX}Example`);
lifecycle.document.title = `${EYE}${NBSP}Mid`;
vm.runInNewContext(input.removeScript, lifecycle.sandbox);
lifecycle.document.title = "After";

process.stdout.write(JSON.stringify({
  stripSamples: [
    stripMarker(`${PREFIX}${PREFIX}Example`),
    stripMarker(`${PREFIX}${PREFIX}${PREFIX}`),
    stripMarker("Plain"),
    stripMarker(null),
    stripMarker(`${EYE}Hello`),
    stripMarker(`${EYE} Hello`),
    stripMarker(`${EYE}${NBSP}Hello`),
    stripMarker(`${EYE}${NARROW_NBSP}Hello`),
    stripMarker(`${EYE}${EYE}Hello`),
    stripMarker(`${PREFIX}${EYE}Hello`),
    stripMarker(42),
    stripMarker(undefined),
    stripMarker(true),
  ],
  duplicateInstallTitle: {
    rawTitle: duplicateInstall.document._rawTitle,
    pageTitle: duplicateInstall.document.title,
  },
  cleanInstallTitle: {
    rawTitle: cleanInstall.document._rawTitle,
    pageTitle: cleanInstall.document.title,
  },
  bareEyeInstallTitle: {
    rawTitle: bareEyeInstall.document._rawTitle,
    pageTitle: bareEyeInstall.document.title,
  },
  nbspEyeInstallTitle: {
    rawTitle: nbspEyeInstall.document._rawTitle,
    pageTitle: nbspEyeInstall.document.title,
  },
  narrowEyeInstallTitle: {
    rawTitle: narrowEyeInstall.document._rawTitle,
    pageTitle: narrowEyeInstall.document.title,
  },
  setterTitle: {
    rawTitle: setterProbe.document._rawTitle,
    pageTitle: setterProbe.document.title,
  },
  bareEyeSetterTitle: {
    rawTitle: bareEyeSetter.document._rawTitle,
    pageTitle: bareEyeSetter.document.title,
  },
  nonStringSetterTitle: {
    rawTitle: nonStringSetter.document._rawTitle,
    pageTitle: nonStringSetter.document.title,
    stripped: stripMarker(nonStringSetter.document._rawTitle),
  },
  duplicateRemoveTitle: {
    rawTitle: duplicateRemove.document._rawTitle,
    pageTitle: duplicateRemove.document.title,
  },
  bareEyeRemoveTitle: {
    rawTitle: bareEyeRemove.document._rawTitle,
    pageTitle: bareEyeRemove.document.title,
  },
  nbspEyeRemoveTitle: {
    rawTitle: nbspEyeRemove.document._rawTitle,
    pageTitle: nbspEyeRemove.document.title,
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
        "Hello",
        "Hello",
        "Hello",
        "Hello",
        "Hello",
        "Hello",
        "42",
        "",
        "true",
    ]
    assert results["duplicateInstallTitle"] == {
        "rawTitle": f"{PREFIX}Example",
        "pageTitle": "Example",
    }
    assert results["cleanInstallTitle"] == {
        "rawTitle": f"{PREFIX}Example",
        "pageTitle": "Example",
    }
    assert results["bareEyeInstallTitle"] == {
        "rawTitle": f"{PREFIX}Hello",
        "pageTitle": "Hello",
    }
    assert results["nbspEyeInstallTitle"] == {
        "rawTitle": f"{PREFIX}Hello",
        "pageTitle": "Hello",
    }
    assert results["narrowEyeInstallTitle"] == {
        "rawTitle": f"{PREFIX}Hello",
        "pageTitle": "Hello",
    }
    assert results["setterTitle"] == {
        "rawTitle": f"{PREFIX}Next",
        "pageTitle": "Next",
    }
    assert results["bareEyeSetterTitle"] == {
        "rawTitle": f"{PREFIX}World",
        "pageTitle": "World",
    }
    assert results["nonStringSetterTitle"] == {
        "rawTitle": f"{PREFIX}42",
        "pageTitle": "42",
        "stripped": "42",
    }
    assert results["duplicateRemoveTitle"] == {
        "rawTitle": "Example",
        "pageTitle": "Example",
    }
    assert results["bareEyeRemoveTitle"] == {
        "rawTitle": "Hello",
        "pageTitle": "Hello",
    }
    assert results["nbspEyeRemoveTitle"] == {
        "rawTitle": "Hello",
        "pageTitle": "Hello",
    }
    assert results["lifecycleTitle"] == {
        "rawTitle": "After",
        "pageTitle": "After",
    }
