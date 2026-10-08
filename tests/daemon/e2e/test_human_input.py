"""Real executor input behavior, observed by an ordinary page's event listeners.

Repeat with ``uv run pytest tests/daemon/e2e/test_human_input.py -m real_chrome -v``.
The JSON artifact includes the browser's trusted keyboard and pointer events,
native opt-out, actionability failures, and input-type compatibility results.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import textwrap
import threading
import uuid

import pytest

from .helpers import bs_home, drop_ledger_session, run_skill, seed_ledger_session


@pytest.fixture
def e2e_chrome(extension_only_chrome):
    return extension_only_chrome


_HTML = """
<!doctype html><meta charset="utf-8"><title>Input behavior fixture</title>
<style>
body {margin: 24px; font: 16px sans-serif}
input, [contenteditable] {display: block; margin: 8px; width: 180px}
button {position: absolute; left: 360.25px; top: 260.25px; width: 180px; height: 60px}
</style>
<section aria-label="Form">
<label>Name<input id="name" value="replace me"></label>
<label>Empty<input id="empty"></label>
<label>Notes<textarea id="notes">old multiline\nnotes</textarea></label>
<div role="textbox" aria-label="Rich text" id="rich" contenteditable>old rich text</div>
<div role="textbox" aria-label="Empty editor" id="emptyrich" contenteditable style="min-height:20px"></div>
<div role="textbox" aria-label="Rich media" id="media" contenteditable><img alt="Existing image"></div>
<input id="readonly" readonly value="locked">
<input id="disabled" disabled value="disabled">
<input id="number" type="number" value="1">
<input id="date" type="date" value="2025-01-01">
<select id="select"><option>One</option></select>
<button id="submit">Submit</button>
</section>
<script>
window.events = [];
for (const type of ['focus', 'keydown', 'keypress', 'input', 'keyup',
                    'pointermove', 'mousemove', 'mousedown', 'mouseup', 'click', 'select']) {
  document.addEventListener(type, e => {
    const r = e.target.getBoundingClientRect();
    const style = getComputedStyle(e.target);
    events.push({type, target: e.target.id, key: e.key || null,
      data: e.data ?? null, trusted: e.isTrusted, time: performance.now(),
      shift: e.shiftKey || false, ctrl: e.ctrlKey || false, meta: e.metaKey || false,
      selection: 'selectionStart' in e.target ?
        {start:e.target.selectionStart,end:e.target.selectionEnd} :
        {text:String(getSelection()),collapsed:getSelection().isCollapsed},
      activated: navigator.userActivation.isActive,
      buttons: e.buttons ?? null,
      dpr: devicePixelRatio,
      x: e.clientX ?? null, y: e.clientY ?? null,
      coalesced: typeof e.getCoalescedEvents === 'function' ?
        e.getCoalescedEvents().map(c => ({x:c.clientX, y:c.clientY,
          time:c.timeStamp, trusted:c.isTrusted})) : null,
      eventTime: e.timeStamp,
      rect: {x: r.x, y: r.y, width: r.width, height: r.height,
             borderX: parseFloat(style.borderLeftWidth) || 0,
             borderY: parseFloat(style.borderTopWidth) || 0}});
  }, true);
}
</script>
"""


@pytest.fixture
def human_input_site():
    # getCoalescedEvents is restricted to secure contexts. A loopback origin
    # is trustworthy; about:blank set_content cannot exercise this API.
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _assert_typed(record: dict, expected: str) -> None:
    """Verify actual event order and final replacement, not wrapper internals."""
    assert record["value"] == expected
    assert record["return_none"] is True
    events = record["events"]
    # Clearing a preexisting value can emit Delete/Backspace before typing.
    starts = [i for i, event in enumerate(events)
              if event["type"] == "keydown" and len(event["key"] or "") == 1
              and not (event.get('ctrl') or event.get('meta'))]
    assert len(starts) == len(expected), events
    for index, character in zip(starts, expected):
        char_events = events[index:index + 4]
        assert [event["type"] for event in char_events] == [
            "keydown", "keypress", "input", "keyup",
        ], char_events
        assert [event["key"] for event in char_events if event["key"]] == [
            character, character, character,
        ], char_events
        assert char_events[2]["data"] == character
        assert all(event["trusted"] for event in char_events)
        if "shift" in char_events[0] and character.isascii() and character.isupper():
            assert char_events[0]["shift"] is True, char_events
            assert any(event["type"] == "keydown" and event["key"] == "Shift"
                       for event in events[:index]), events


@pytest.mark.parametrize("extension_only_chrome", ["headless"], indirect=True)
def test_executor_human_input_defaults_and_playwright_semantics(
    e2e_daemon, e2e_chrome, ext_ready, e2e_artifacts_dir, human_input_site,
):
    """Agent-created locators/pages/frames use normal browser input events."""
    pytest.importorskip("playwright.sync_api")
    assert not (e2e_chrome.profile_path / 'DevToolsActivePort').exists()
    sid = f"e2e-input-{uuid.uuid4().hex}"
    seed_ledger_session(bs_home("extension"), sid, backend="extension", name="input")
    artifact = e2e_artifacts_dir / "human-input.json"
    artifact.unlink(missing_ok=True)
    script = "import json\n" + f"page.goto({human_input_site!r}, wait_until='load')\n"
    script += "from pathlib import Path\n" + f"artifact = Path({str(artifact)!r})\n"
    script += textwrap.dedent("""
        from playwright.sync_api import Error, TimeoutError
        result = {}
        result['browser_version'] = {'reported': context.browser.version,
                                    'actual_user_agent': page.evaluate('navigator.userAgent')}
        def take():
            return page.evaluate('events.splice(0)')
        field = page.get_by_role('region', name='Form').get_by_role('textbox', name='Name')
        returned = field.fill('Alice')
        result['locator_fill'] = {'value': field.input_value(), 'return_none': returned is None,
                                  'events': take()}
        returned = field.fill('A!b?')
        result['shift_fill'] = {'value': field.input_value(), 'return_none': returned is None,
                                'events': take()}
        empty = page.get_by_role('textbox', name='Empty', exact=True)
        returned = empty.fill('Eve')
        result['initially_empty_fill'] = {'value': empty.input_value(),
                                         'return_none': returned is None, 'events': take()}
        notes = page.get_by_role('textbox', name='Notes')
        returned = notes.fill('New notes', force=True)
        result['textarea_fill'] = {'value': notes.input_value(),
                                   'return_none': returned is None, 'events': take()}

        returned = page.get_by_role('button', name='Submit').click()
        result['click'] = {'return_none': returned is None, 'events': take()}
        metrics_session = context.new_cdp_session(page)
        metrics_session.send('Emulation.setDeviceMetricsOverride', {
            'width': 1280, 'height': 900, 'deviceScaleFactor': 1.75, 'mobile': False})
        page.get_by_role('button', name='Submit').click()
        result['scaled_click'] = {'events': take()}
        metrics_session.send('Emulation.clearDeviceMetricsOverride')
        metrics_session.detach()
        returned = page.click('#submit', position={'x': 9, 'y': 11}, force=True)
        result['position_click'] = {'return_none': returned is None, 'events': take()}

        returned = field.fill('native', human=False)
        result['native_fill'] = {'value': field.input_value(), 'return_none': returned is None,
                                'events': take()}
        page.click('#submit', human=False)
        result['native_click'] = {'events': take()}

        rich = page.get_by_role('textbox', name='Rich text')
        returned = rich.fill('Rich')
        result['rich_fill'] = {'value': rich.inner_text(), 'return_none': returned is None,
                              'events': take()}
        empty_rich = page.get_by_role('textbox', name='Empty editor')
        returned = empty_rich.fill('Empty rich')
        result['empty_editor_fill'] = {'value': empty_rich.inner_text(),
                                       'return_none': returned is None, 'events': take()}
        media = page.get_by_role('textbox', name='Rich media')
        returned = media.fill('Media')
        result['media_fill'] = {'value': media.inner_text(), 'return_none': returned is None,
                                'children': media.locator('img').count(), 'events': take()}
        returned = page.fill('#name', 'Page')
        result['page_fill'] = {'value': field.input_value(), 'return_none': returned is None,
                              'events': take()}
        returned = field.fill('')
        result['empty_fill'] = {'value': field.input_value(), 'return_none': returned is None,
                               'events': take()}
        # A forced click can land on an overlay instead of the intended input.
        # Replacement still belongs to the requested field, and must not select
        # or erase another editor that was focused before the call.
        empty.click(human=False)
        page.evaluate('''() => {
            const r=document.querySelector('#name').getBoundingClientRect();
            const overlay=document.createElement('div');overlay.id='overlay';
            Object.assign(overlay.style,{position:'fixed',left:r.left+'px',top:r.top+'px',
                width:r.width+'px',height:r.height+'px',zIndex:'1000'});
            document.body.append(overlay);
        }''')
        take()
        returned = field.fill('Forced', force=True)
        result['forced_overlay_fill'] = {'value': field.input_value(),
                                         'other_value': empty.input_value(),
                                         'return_none': returned is None, 'events': take()}
        page.evaluate("document.querySelector('#overlay').remove()")

        for selector in ['#readonly', '#disabled']:
            try:
                page.locator(selector).fill('x', timeout=180)
                result[selector] = {'error': None}
            except TimeoutError as exc:
                result[selector] = {'error': type(exc).__name__, 'message': str(exc)}
        try:
            page.locator('#select').fill('bad', timeout=180)
            result['select'] = {'error': None}
        except Error as exc:
            result['select'] = {'error': type(exc).__name__, 'message': str(exc)}
        for selector, value in [('#number', '42'), ('#date', '2026-10-07')]:
            returned = page.locator(selector).fill(value)
            result[selector] = {'value': page.locator(selector).input_value(),
                                'return_none': returned is None, 'events': take()}
        try:
            page.locator('#number').fill('not a number')
            result['invalid_number'] = {'error': None}
        except Error as exc:
            result['invalid_number'] = {'error': type(exc).__name__, 'message': str(exc)}
        # Keep observations available if later page/frame setup fails.
        artifact.write_text(json.dumps(result, indent=2) + '\\n')

        # A held button enters Playwright's native drag handling. Preserve its
        # state throughout the approach and let native click release normally.
        page.mouse.move(20, 20)
        page.mouse.down()
        take()
        returned = page.get_by_role('button', name='Submit').click(force=True)
        result['held_button_click'] = {'return_none': returned is None, 'events': take()}
        page.mouse.up()
        take()

        other = context.new_page()
        other.set_content('<label>Other<input id="other" value="old"></label>')
        other.evaluate('''() => {
            window.events = [];
            for (const type of ['keydown', 'keypress', 'input', 'keyup'])
                document.addEventListener(type, e => events.push({type, key:e.key || null,
                    data:e.data ?? null, trusted:e.isTrusted,
                    ctrl:e.ctrlKey, meta:e.metaKey}));
        }''')
        returned = other.get_by_role('textbox', name='Other').fill('New')
        result['new_page_fill'] = {'value': other.locator('#other').input_value(),
                                  'return_none': returned is None,
                                  'events': other.evaluate('events')}
        frame = other.main_frame
        frame.evaluate('events.splice(0)')
        returned = frame.fill('#other', 'Frame')
        result['frame_fill'] = {'value': frame.locator('#other').input_value(),
                               'return_none': returned is None,
                               'events': frame.evaluate('events')}
        other.close()
        artifact.write_text(json.dumps(result, indent=2) + '\\n')
        print('HUMAN_INPUT=' + ','.join(result))
    """)
    try:
        execution = run_skill(script, backend="extension", runtime_dir=e2e_daemon.runtime_dir,
                              extra_env={"BD_SESSION": sid}, timeout=90)
        output = {"returncode": execution.returncode, "stdout": execution.stdout,
                  "stderr": execution.stderr}
        if artifact.exists():
            output["observations"] = json.loads(artifact.read_text())
        artifact.write_text(json.dumps(output, indent=2) + "\n")
        assert execution.returncode == 0, output
        observations = output["observations"]
        assert 'Chrome/' + observations['browser_version']['reported'] in \
            observations['browser_version']['actual_user_agent']
        _assert_typed(observations['initially_empty_fill'], 'Eve')
        assert not any(e['key'] in ('Delete', 'Backspace')
                       for e in observations['initially_empty_fill']['events'])
        assert not any(e.get('ctrl') or e.get('meta')
                       for e in observations['initially_empty_fill']['events'])
        _assert_typed(observations['empty_editor_fill'], 'Empty rich')
        assert not any(e['key'] in ('Delete', 'Backspace') or e.get('ctrl') or e.get('meta')
                       for e in observations['empty_editor_fill']['events'])
        # Replacements must select through an actual trusted keyboard shortcut,
        # with a full selection visible before deletion. This also covers a
        # multiline textarea and contenteditable containing only an image.
        for name, old in [('locator_fill', 'replace me'), ('shift_fill', 'Alice'),
                          ('textarea_fill', 'old multiline\nnotes'),
                          ('rich_fill', 'old rich text'), ('media_fill', None),
                          ('page_fill', 'native'), ('empty_fill', 'Page')]:
            events = observations[name]['events']
            shortcut = next(e for e in events if e['type'] == 'keyup'
                            and (e['key'] or '').lower() == 'a'
                            and (e['ctrl'] or e['meta']))
            assert shortcut['trusted'], shortcut
            assert any(e['type'] == 'keydown' and e['key'] in ('Control', 'Meta')
                       and e['trusted'] for e in events[:events.index(shortcut)]), events
            selection = shortcut['selection']
            if 'start' in selection:
                assert selection == {'start': 0, 'end': len(old)}, shortcut
            else:
                assert selection['collapsed'] is False, shortcut
                if old is not None:
                    assert selection['text'] == old, shortcut
            assert any(e['type'] == 'keyup' and e['key'] in ('Control', 'Meta')
                       for e in events[events.index(shortcut) + 1:]), events
        for name in ('locator_fill', 'shift_fill', 'initially_empty_fill'):
            pressed = {}
            shift_down = None
            for event in observations[name]['events']:
                if event['type'] == 'keydown':
                    pressed[event['key']] = event['time']
                    if event['key'] == 'Shift':
                        shift_down = event['time']
                    elif event['shift'] and shift_down is not None:
                        assert event['time'] - shift_down >= 15, event
                elif event['type'] == 'keyup' and event['key'] in pressed:
                    assert event['time'] - pressed.pop(event['key']) >= 20, event
        first_moves = [e for e in observations['locator_fill']['events']
                       if e['type'] == 'pointermove']
        assert first_moves and (first_moves[0]['x'] > 5 or first_moves[0]['y'] > 5), first_moves
        for name in ('locator_fill', 'shift_fill', 'initially_empty_fill', 'click', 'scaled_click'):
            for event in observations[name]['events']:
                if event['type'] == 'pointermove':
                    assert abs(event['x'] * event['dpr'] - round(event['x'] * event['dpr'])) < .01, event
                    assert abs(event['y'] * event['dpr'] - round(event['y'] * event['dpr'])) < .01, event
                    for sample in event['coalesced'] or []:
                        assert abs(sample['x'] * event['dpr'] - round(sample['x'] * event['dpr'])) < .01, event
                        assert abs(sample['y'] * event['dpr'] - round(sample['y'] * event['dpr'])) < .01, event
        focus = next(event for event in observations["locator_fill"]["events"]
                     if event["type"] == "focus" and event["target"] == "name")
        assert focus["activated"] is True, focus
        assert any(event["type"] == "mousedown" and event["target"] == "name"
                   for event in observations["locator_fill"]["events"]), observations["locator_fill"]
        for key, value in [("locator_fill", "Alice"), ("rich_fill", "Rich"),
                           ("textarea_fill", "New notes"),
                           ("page_fill", "Page"), ("new_page_fill", "New"),
                           ("frame_fill", "Frame")]:
            _assert_typed(observations[key], value)
        _assert_typed(observations["shift_fill"], "A!b?")
        assert all(event["shift"] for event in observations["shift_fill"]["events"]
                   if event["type"] == "keydown" and event["key"] in ("A", "!", "?"))
        _assert_typed(observations['media_fill'], 'Media')
        assert observations['media_fill']['children'] == 0
        assert observations["empty_fill"]["value"] == ""
        assert observations["empty_fill"]["return_none"] is True
        forced = observations['forced_overlay_fill']
        assert forced['value'] == 'Forced' and forced['other_value'] == 'Eve', forced
        assert forced['return_none'] is True
        assert not any(e.get('ctrl') or e.get('meta') for e in forced['events']), forced
        assert observations["native_fill"]["value"] == "native"
        assert observations["native_fill"]["return_none"] is True
        assert not any(event["type"] in ("keydown", "keypress", "keyup")
                       for event in observations["native_fill"]["events"])
        assert observations["click"]["return_none"] is True
        held = observations['held_button_click']
        assert held['return_none'] is True
        assert any(e['type'] == 'mousemove' and e['buttons'] == 1 for e in held['events']), held
        assert any(e['type'] == 'click' and e['target'] == 'submit' for e in held['events']), held
        assert any(e['type'] == 'mouseup' and e['buttons'] == 0 for e in held['events']), held
        pointer = observations["click"]["events"]
        coalesced = [event for event in pointer if event['type'] == 'pointermove'
                     and len(event['coalesced'] or []) > 1]
        assert coalesced, pointer
        for event in coalesced:
            assert all(sample['trusted'] for sample in event['coalesced']), event
            assert all(a['time'] <= b['time'] for a, b in
                       zip(event['coalesced'], event['coalesced'][1:])), event
        assert sum(event["type"] == "mousemove" for event in pointer) >= 3, pointer
        visible_moves = [event for event in pointer if event['type'] == 'mousemove']
        assert len(visible_moves) >= 15, visible_moves
        assert all(((b['x'] - a['x']) ** 2 + (b['y'] - a['y']) ** 2) ** .5 <= 15
                   for a, b in zip(visible_moves, visible_moves[1:])), visible_moves
        # Chrome may deliver several physical samples in one pointermove.
        # Check path density using those original samples, rather than requiring
        # the browser to dispatch every sample as a separate DOM event.
        moves = [sample for event in pointer if event['type'] == 'pointermove'
                 for sample in (event['coalesced'] or [event])]
        assert len(moves) >= 15, moves
        assert all(((b["x"] - a["x"]) ** 2 + (b["y"] - a["y"]) ** 2) ** .5 <= 15
                   for a, b in zip(moves, moves[1:])), moves
        click = next(event for event in pointer if event["type"] == "click")
        rect = click["rect"]
        assert rect["x"] < click["x"] < rect["x"] + rect["width"]
        assert rect["y"] < click["y"] < rect["y"] + rect["height"]
        assert (abs(click["x"] - rect["x"] - rect["width"] / 2) > 0.5 or
                abs(click["y"] - rect["y"] - rect["height"] / 2) > 0.5), click
        down = next(event for event in pointer if event["type"] == "mousedown")
        up = next(event for event in pointer if event["type"] == "mouseup")
        assert up["time"] - down["time"] >= 5, pointer
        positioned = next(event for event in observations["position_click"]["events"]
                          if event["type"] == "click")
        assert abs(positioned["x"] - positioned["rect"]["x"] -
                   positioned["rect"]["borderX"] - 9) <= 1
        assert abs(positioned["y"] - positioned["rect"]["y"] -
                   positioned["rect"]["borderY"] - 11) <= 1
        native = next(event for event in observations["native_click"]["events"]
                      if event["type"] == "click")
        assert abs(native["x"] - native["rect"]["x"] - native["rect"]["width"] / 2) <= 1
        assert abs(native["y"] - native["rect"]["y"] - native["rect"]["height"] / 2) <= 1
        for selector in ("#readonly", "#disabled"):
            assert observations[selector]["error"] == "TimeoutError", observations[selector]
        assert observations["select"]["error"] == "Error"
        assert observations["invalid_number"]["error"] == "Error"
        assert observations["#number"]["value"] == "42"
        assert observations["#date"]["value"] == "2026-10-07"
    finally:
        drop_ledger_session(bs_home("extension"), sid)
