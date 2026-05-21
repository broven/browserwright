from browser_daemon import cli


def test_push_parses_and_sends_install(tmp_path, monkeypatch):
    path = tmp_path / "x.user.js"
    path.write_text(
        "// ==UserScript==\n// @name X\n// @namespace n\n"
        "// @match https://e.com/*\n// ==/UserScript==\nwindow.x=1;\n")
    captured = {}

    async def fake_call(cfg, method, params, timeout=5.0):
        captured["method"] = method
        captured["params"] = params
        return {"ok": True, "id": params["script"]["id"]}

    monkeypatch.setattr(cli, "_userscript_call_ws", fake_call, raising=False)
    rc = cli._cmd_userscript(["push", str(path)])
    assert rc == 0
    assert captured["method"] == "BrowserDaemon.userscript.install"
    assert captured["params"]["script"]["name"] == "X"
