"""Trusted wheel preparation through the extension-only executor.

Repeat with ``mise exec -- uv run pytest tests/daemon/e2e/test_human_scroll.py -q``.
The artifact records real wheel/scroll order for document, nested and frame input.
"""
from __future__ import annotations

import json
import textwrap
import pytest

from .helpers import run_skill


@pytest.fixture
def e2e_chrome(extension_only_chrome):
    return extension_only_chrome


_HTML = """<!doctype html><style>
body {margin:20px} .space {height:1100px}
#outer {height:220px;width:500px;overflow:auto;border:5px solid black}
#inner {height:180px;width:350px;overflow:auto;border:4px solid blue}
button,input {height:40px;width:180px} iframe {width:500px;height:230px}
</style><button id=top>Top</button><div class=space></div>
<label>Below<input id=below></label><div class=space></div>
<div id=outer><div style=height:600px></div>
<div id=inner><div style=height:650px></div><button id=nested>Nested</button>
<div style=height:100px></div></div><div style=height:100px></div></div>
<div class=space></div><iframe id=frame srcdoc="<div style='height:850px'></div><button id=child>Child</button>"></iframe>
<script>
window.events=[]; window.clicked=[];
for (const type of ['wheel','scroll','click']) document.addEventListener(type,e=>{
 events.push({type,target:e.target.id || e.target.nodeName,trusted:e.isTrusted,
 time:performance.now(),x:e.clientX,y:e.clientY,dx:e.deltaX,dy:e.deltaY});
 if(type==='click') clicked.push(e.target.id);
},true);
</script>"""


@pytest.mark.parametrize('extension_only_chrome', ['headless'], indirect=True)
def test_scroll_target_at_document_bottom(e2e_daemon, e2e_chrome, ext_ready, e2e_artifacts_dir):
    artifact = e2e_artifacts_dir / 'human-scroll-bottom.json'
    script = textwrap.dedent('''
        import json
        page.set_content("""<!doctype html><style>body{margin:0}button{display:block;height:40px}</style>
          <div style='height:850px'></div><button onclick='window.clicked=true'>Edge</button>
          <script>window.events=[];for(const t of ['wheel','scroll'])document.addEventListener(t,
            e=>events.push({type:t,trusted:e.isTrusted}),true)</script>""")
        returned=page.get_by_role('button',name='Edge').click(timeout=8000)
        result={'return_none':returned is None,'clicked':page.evaluate('window.clicked'),
                'events':page.evaluate('events')}
    ''')
    script += f'\nfrom pathlib import Path\nPath({str(artifact)!r}).write_text(json.dumps(result,indent=2))\n'
    execution = run_skill(script, backend='extension', runtime_dir=e2e_daemon.runtime_dir,timeout=20)
    assert execution.returncode == 0, (execution.stdout,execution.stderr)
    result = json.loads(artifact.read_text())
    assert result['return_none'] and result['clicked'],result
    assert any(e['type']=='wheel' and e['trusted'] for e in result['events']),result


@pytest.mark.parametrize('extension_only_chrome', ['headless'], indirect=True)
def test_executor_scroll_has_trusted_wheel_input(
    e2e_daemon, e2e_chrome, ext_ready, e2e_artifacts_dir,
):
    artifact = e2e_artifacts_dir / 'human-scroll.json'
    artifact.unlink(missing_ok=True)
    script = 'import json\nfrom pathlib import Path\n'
    script += f'artifact=Path({str(artifact)!r})\npage.set_content({_HTML!r})\n'
    script += textwrap.dedent('''
        from playwright.sync_api import TimeoutError
        result={}
        def save(): artifact.write_text(json.dumps(result,indent=2))
        returned=page.get_by_role('button',name='Top').click()
        result['visible']={'return_none':returned is None,'events':page.evaluate('events.splice(0)')}
        save()
        child_frame=page.frames[1]
        child_frame.evaluate("""() => {
          window.events=[]; window.clicked=false;
          for(const type of ['wheel','scroll','click']) document.addEventListener(type,e=>{
            events.push({type,trusted:e.isTrusted,time:performance.now()});
            if(type==='click' && e.target.id==='child') clicked=true;
          },true);
        }""")
        returned=page.get_by_role('textbox',name='Below').fill('Ada')
        result['document']={'return_none':returned is None,'value':page.locator('#below').input_value(),
                            'events':page.evaluate('events.splice(0)')}
        save()
        returned=page.get_by_role('button',name='Nested').click(position={'x':12,'y':11},force=True)
        result['nested']={'return_none':returned is None,'clicked':page.evaluate('clicked.slice()'),
                          'events':page.evaluate('events.splice(0)')}
        save()
        child=page.frame_locator('#frame').get_by_role('button',name='Child')
        returned=child.click()
        result['frame']={'return_none':returned is None,'events':page.evaluate('events.splice(0)'),
                        'child_events':child_frame.evaluate('events'),
                        'child_clicked':child_frame.evaluate('clicked')}
        save()
        page.get_by_role('button',name='Top').click(human=False)
        page.evaluate("events.splice(0); document.addEventListener('wheel',e=>e.preventDefault(),{passive:false})")
        try:
            page.get_by_role('textbox',name='Below').click(timeout=3000)
            result['blocked']={'timeout':False}
        except TimeoutError:
            result['blocked']={'timeout':True,'events':page.evaluate('events.splice(0)')}
        artifact.write_text(json.dumps(result,indent=2))
    ''')
    execution = run_skill(script, backend='extension', runtime_dir=e2e_daemon.runtime_dir,
                          timeout=90)
    output = {'returncode':execution.returncode,'stdout':execution.stdout,'stderr':execution.stderr}
    if artifact.exists():
        output['observations'] = json.loads(artifact.read_text())
    artifact.write_text(json.dumps(output,indent=2)+'\n')
    assert execution.returncode == 0, output
    observations = output['observations']
    assert observations['document']['value'] == 'Ada'
    assert 'nested' in observations['nested']['clicked']
    assert observations['visible']['return_none']
    assert not any(e['type'] in ['scroll','wheel'] for e in observations['visible']['events'])
    assert observations['frame']['child_clicked']
    for name in ['document','nested','frame']:
        events = observations[name]['events']
        assert observations[name]['return_none'], observations[name]
        assert any(e['type']=='wheel' and e['trusted'] for e in events), events
        for i, event in enumerate(events):
            if event['type']=='scroll':
                assert any(prior['type']=='wheel' and prior['trusted']
                           and event['time']-prior['time']<200 for prior in events[:i]), events
    child_events=observations['frame']['child_events']
    assert any(e['type']=='wheel' and e['trusted'] for e in child_events), child_events
    for i,event in enumerate(child_events):
        if event['type']=='scroll':
            assert any(prior['type']=='wheel' and prior['trusted']
                       and event['time']-prior['time']<200 for prior in child_events[:i]), child_events
    assert observations['blocked']['timeout'], observations['blocked']
    assert any(e['type']=='wheel' and e['trusted'] for e in observations['blocked']['events'])
    assert not any(e['type']=='scroll' for e in observations['blocked']['events'])
