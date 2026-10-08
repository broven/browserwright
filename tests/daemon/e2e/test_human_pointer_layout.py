"""Native pointer coordinates after target geometry changes during an approach.

Repeat with ``mise exec -- uv run pytest tests/daemon/e2e/test_human_pointer_layout.py -q``.
The artifact records trusted browser events and the final target geometry.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from .helpers import run_skill


@pytest.fixture
def e2e_chrome(extension_only_chrome):
    return extension_only_chrome


@pytest.mark.parametrize('extension_only_chrome', ['headless'], indirect=True)
def test_click_after_layout_shift_stays_on_physical_pixel_grid(
    e2e_daemon, e2e_chrome, ext_ready, e2e_artifacts_dir,
):
    artifact = e2e_artifacts_dir / 'human-pointer-layout.json'
    artifact.unlink(missing_ok=True)
    script = 'import json\nfrom pathlib import Path\n'
    script += f'artifact=Path({str(artifact)!r})\n'
    script += textwrap.dedent('''
        result={}
        metrics=context.new_cdp_session(page)
        for ratio in [2,1.75]:
            page.goto('about:blank')
            metrics.send('Emulation.setDeviceMetricsOverride', {
                'width':1000,'height':700,'deviceScaleFactor':ratio,'mobile':False})
            page.set_content("""<!doctype html><button id=target
              style='position:absolute;left:360.25px;top:250.25px;width:180px;height:60px'>Moving</button>
              <script>
              window.events=[];window.clicked=false;window.shifted=false;
              document.addEventListener('pointermove',()=>setTimeout(()=>{
                document.querySelector('button').style.left='360.625px';shifted=true;
              },20),{once:true});
              document.querySelector('button').addEventListener('click',()=>clicked=true);
              for(const type of ['pointermove','pointerdown','pointerup','click'])
                document.addEventListener(type,e=>events.push({type,trusted:e.isTrusted,
                  x:e.clientX,y:e.clientY,dpr:devicePixelRatio,target:e.target.id,
                  coalesced:typeof e.getCoalescedEvents==='function'?
                    e.getCoalescedEvents().map(c=>({x:c.clientX,y:c.clientY,trusted:c.isTrusted})):null
                }),true);
              </script>""")
            returned=page.get_by_role('button',name='Moving').click()
            result[str(ratio)]={'return_none':returned is None,
                'observation':page.evaluate("""() => ({clicked,shifted,events,
                  box:document.querySelector('button').getBoundingClientRect().toJSON()})""")}
            # The framework lease must end with the generated click. Explicit
            # padding-relative positions and human=False retain native CDP
            # coordinates, even when those are between physical pixels.
            result[str(ratio)]['explicit']=[]
            for native in [False,True]:
                page.evaluate('events=[]')
                page.get_by_role('button',name='Moving').click(
                    position={'x':20.123,'y':20.123},human=not native)
                result[str(ratio)]['explicit'].append(page.evaluate("""() => ({events,
                  box:document.querySelector('button').getBoundingClientRect().toJSON(),
                  border:parseFloat(getComputedStyle(document.querySelector('button')).borderLeftWidth)})"""))
            artifact.write_text(json.dumps(result,indent=2))
        metrics.send('Emulation.clearDeviceMetricsOverride')
        # Detaching a CDP handle must retire its temporary input state without
        # affecting the live page or native calls made through another handle.
        lease=context.new_cdp_session(page)
        lease.send('Browserwright.setPointerGrid', {'devicePixelRatio':2})
        lease.detach()
        page.evaluate('events=[]')
        page.get_by_role('button',name='Moving').click(
            position={'x':20.123,'y':20.123},human=False)
        result['detached_lease']=page.evaluate("""() => ({events,
          box:document.querySelector('button').getBoundingClientRect().toJSON(),
          border:parseFloat(getComputedStyle(document.querySelector('button')).borderLeftWidth)})""")
        artifact.write_text(json.dumps(result,indent=2))
        metrics.detach()
    ''')
    execution = run_skill(script, backend='extension', runtime_dir=e2e_daemon.runtime_dir,
                          timeout=40)
    output = {'returncode':execution.returncode,'stdout':execution.stdout,'stderr':execution.stderr}
    if artifact.exists():
        output['observations'] = json.loads(artifact.read_text())
    artifact.write_text(json.dumps(output,indent=2)+'\n')
    assert execution.returncode == 0, output
    detached = output['observations'].pop('detached_lease')
    press = next(event for event in detached['events'] if event['type']=='pointerdown')
    assert press['trusted'] and press['target']=='target', detached
    for axis in ('x','y'):
        expected = int((detached['box'][axis]+detached['border']+20.123)*100)/100
        assert abs(press[axis]-expected) < .001, detached
    for result in output['observations'].values():
        observed = result['observation']
        assert result['return_none'] and observed['clicked'] and observed['shifted'], observed
        box = observed['box']
        assert box['x'] == 360.625, box
        pointer = [event for event in observed['events'] if event['type'].startswith('pointer')]
        assert any(event['type']=='pointerdown' and event['target']=='target' for event in pointer)
        for event in pointer:
            assert event['trusted'], event
            for sample in [event, *(event['coalesced'] or [])]:
                assert sample['trusted'], sample
                for axis in ('x','y'):
                    scaled = sample[axis]*event['dpr']
                    assert abs(scaled-round(scaled)) < .01, event
        for explicit in result['explicit']:
            press = next(event for event in explicit['events'] if event['type']=='pointerdown')
            assert press['trusted'] and press['target']=='target', explicit
            for axis in ('x','y'):
                expected = int((explicit['box'][axis]+explicit['border']+20.123)*100)/100
                assert abs(press[axis]-expected) < .001, explicit
