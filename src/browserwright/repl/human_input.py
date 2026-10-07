"""Input defaults for the executor's live Playwright surface.

Install once on the resident context, before handing it to agent helpers. Real
Playwright objects remain real objects (including chained locators and frames).
Only registered contexts use these defaults. ``human=False`` on click/fill, or
``BW_HUMAN_INPUT=0``, restores native defaults. Native Playwright still
performs the final click and its actionability/navigation checks; fill
validates editability and clears existing text before keyboard typing.

Nothing here activates a tab or focuses a window: the extension backend drives
the user's own Chrome, and browser UI activation steals the desktop's active
window. Every step works on a background tab (see ``docs/human-input.md``).
Screenshot defaults live in ``screenshot_defaults.py``.
"""
from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import random
import time
import weakref
from datetime import timedelta
from fractions import Fraction
from typing import Any

_CONTEXTS: weakref.WeakSet[Any] = weakref.WeakSet()
_SCROLL_CONTEXTS: weakref.WeakSet[Any] = weakref.WeakSet()
_POINTERS: weakref.WeakKeyDictionary[Any, tuple[float, float]] = weakref.WeakKeyDictionary()
_BUTTONS: weakref.WeakKeyDictionary[Any, set[str]] = weakref.WeakKeyDictionary()
_INSTALLED = False
_RNG = random.SystemRandom()
_TEXT_TYPES = {None, "", "text", "search", "email", "url", "tel", "password"}
_SCROLL_ENGINE = '_browserwright_scroll'
_SCROLL_SOURCE = r'''({
  query(root, selector) { return this.queryAll(root, selector)[0] || null; },
  queryAll(root, selector) {
    if (!(root instanceof Element)) return [];
    const rect = e => {
      const r = e.getBoundingClientRect();
      return {x:r.x,y:r.y,width:r.width,height:r.height};
    };
    const parent = e => e.parentElement || e.getRootNode().host || null;
    const canScroll = (e, axis) => {
      if (e === document.scrollingElement)
        return axis === 'x' ? e.scrollWidth > innerWidth : e.scrollHeight > innerHeight;
      const s = getComputedStyle(e);
      return /^(auto|scroll|overlay)$/.test(axis === 'x' ? s.overflowX : s.overflowY) &&
        (axis === 'x' ? e.scrollWidth > e.clientWidth : e.scrollHeight > e.clientHeight);
    };
    const jobs = [];
    let target = rect(root);
    const position = JSON.parse(selector || '{}').position;
    if (position) target = {x:target.x+root.clientLeft+position.x,
      y:target.y+root.clientTop+position.y,width:0,height:0};
    for (let e = parent(root); e; e = parent(e)) {
      if (e === document.scrollingElement) continue;
      const sx = canScroll(e,'x'), sy = canScroll(e,'y');
      if (!sx && !sy) continue;
      const r = rect(e), scaleX = e.offsetWidth ? r.width/e.offsetWidth : 1;
      const scaleY = e.offsetHeight ? r.height/e.offsetHeight : 1;
      jobs.push({target,clip:{x:r.x+e.clientLeft*scaleX,y:r.y+e.clientTop*scaleY,
        width:e.clientWidth*scaleX,height:e.clientHeight*scaleY},sx,sy,element:e});
      target = r;
    }
    jobs.push({target,clip:{x:0,y:0,width:innerWidth,height:innerHeight},
      sx:true,sy:true,element:document.scrollingElement});
    const adjustment = (start,size,low,length) => {
      if (size <= length && start >= low && start+size <= low+length) return 0;
      const margin = Math.min(8,length/10);
      if (size > length-2*margin) return start+size/2-low-length/2;
      if (start < low+margin) return start-low-margin;
      if (start+size > low+length-margin) return start+size-low-length+margin;
      return 0;
    };
    for (const job of jobs) {
      const {target:t,clip:c} = job;
      job.dx = job.sx ? adjustment(t.x,t.width,c.x,c.width) : 0;
      job.dy = job.sy ? adjustment(t.y,t.height,c.y,c.height) : 0;
      const left=Math.max(1,c.x),top=Math.max(1,c.y);
      const right=Math.min(innerWidth-1,c.x+c.width),bottom=Math.min(innerHeight-1,c.y+c.height);
      job.point = null;
      if (right > left && bottom > top) {
        // Prefer exposed space belonging to this scroller; a nested scroller
        // can otherwise consume the wheel before its parent sees it.
        for (const fy of [.5,.1,.9,.02,.98]) for (const fx of [.5,.1,.9,.02,.98]) {
          const x=left+(right-left)*fx,y=top+(bottom-top)*fy;
          let hit=document.elementFromPoint(x,y);
          while (hit && hit !== job.element &&
            !((job.dx && canScroll(hit,'x')) || (job.dy && canScroll(hit,'y')))) hit=parent(hit);
          if (hit === job.element || (!hit && job.element === document.scrollingElement)) {
            job.point={x,y}; break;
          }
        }
        if (!job.point) job.point={x:(left+right)/2,y:(top+bottom)/2};
      }
      delete job.element;
    }
    const r=rect(root), out=document.createElement('span');
    let caret=null;
    if (root.isContentEditable) {
      const s=root.ownerDocument.getSelection();
      caret={inside:!!s && root.contains(s.anchorNode) && root.contains(s.focusNode),point:null};
      for (const fx of [.95,.05,.5]) for (const fy of [.5,.1,.9]) {
        const x=r.x+r.width*fx,y=r.y+r.height*fy;
        if (root.ownerDocument.elementFromPoint(x,y) === root) {
          caret.point={x:r.width*fx-root.clientLeft,y:r.height*fy-root.clientTop};break;
        }
      }
    }
    const active=root.getRootNode().activeElement || root.ownerDocument.activeElement;
    out.textContent=JSON.stringify({rect:r,jobs,focused:active === root || root.contains(active),
      viewport:{width:innerWidth,height:innerHeight,
      dpr:devicePixelRatio || 1},clientLeft:root.clientLeft,clientTop:root.clientTop,
      offsetWidth:root.offsetWidth,offsetHeight:root.offsetHeight,caret});
    // A detached transport node never changes the document or its styles.
    return [out];
  }
})'''
_SHIFT_KEYS = dict(zip('~!@#$%^&*()_+{}|:"<>?', (
    'Backquote', 'Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5', 'Digit6',
    'Digit7', 'Digit8', 'Digit9', 'Digit0', 'Minus', 'Equal', 'BracketLeft',
    'BracketRight', 'Backslash', 'Semicolon', 'Quote', 'Comma', 'Period', 'Slash',
)))


def _enabled(page: Any, human: bool | None) -> bool:
    return (page.context in _CONTEXTS and human is not False
            and os.environ.get("BW_HUMAN_INPUT", "1").lower() not in {"0", "false", "off"})


class _Budget:
    """Share one operation timeout across preparation, input, and native action."""

    def __init__(self, page: Any, timeout: Any):
        if isinstance(timeout, timedelta):
            timeout = timeout.total_seconds() * 1000
        ms = page._impl_obj._timeout_settings.timeout(timeout)
        self.end = time.monotonic() + ms / 1000 if ms else None

    def remaining(self) -> float:
        if self.end is None:
            return 0
        ms = (self.end - time.monotonic()) * 1000
        if ms <= 0:
            from playwright.sync_api import TimeoutError
            raise TimeoutError("Timeout exceeded during browserwright input")
        return ms

    def pause(self, seconds: float) -> None:
        remaining = self.remaining()
        if remaining and seconds * 1000 > remaining:
            seconds = remaining / 1000
        time.sleep(seconds)
        self.remaining()


def _mouse_samples(page: Any, points: list[tuple[float, float]], budget: _Budget) -> None:
    """Queue one small group of native samples before waiting for their ACKs.

    Playwright's public synchronous mouse.move waits for each dispatched event.
    Real pointer devices can supply several samples before Chrome paints; the
    implementation API keeps Playwright's button/modifier and drag handling
    while allowing Chrome to coalesce samples through its normal input queue.
    """
    remaining = budget.remaining()
    if _BUTTONS.get(page.mouse):
        # Playwright's drag interception owns shared state while a button is
        # held. Keep those moves sequential, as its public API normally does.
        for px, py in points:
            budget.remaining()
            page.mouse.move(px, py)
        budget.remaining()
        return

    async def send() -> None:
        tasks = [asyncio.create_task(page.mouse._impl_obj.move(px, py))
                 for px, py in points]
        try:
            batch = asyncio.gather(*tasks)
            if remaining:
                await asyncio.wait_for(batch, timeout=remaining / 1000)
            else:
                await batch
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    try:
        page._sync(send())
    except asyncio.TimeoutError as exc:
        from playwright.sync_api import TimeoutError
        raise TimeoutError('Timeout exceeded during browserwright input') from exc
    _POINTERS[page.mouse] = points[-1]
    budget.remaining()


def _approach(page: Any, x: float, y: float, budget: _Budget, metrics: dict[str, float]) -> None:
    ratio = metrics['dpr']
    previous = _POINTERS.get(page.mouse)
    if previous is None:
        # Until input has been sent, the pointer is outside this viewport.
        # Enter at a screen edge rather than inventing a previous (0, 0).
        start_x = 0 if _RNG.random() < .5 else metrics['width'] - 1
        start_y = _RNG.uniform(.15, .85) * metrics['height']
    else:
        start_x, start_y = previous
    distance = math.hypot(x - start_x, y - start_y)
    # Sample by distance, with enough cadence for short paths too. A small
    # sideways arc keeps the approach smooth rather than jumping between a
    # handful of remote points.
    steps = min(512, max(32, math.ceil(distance / 6)))
    bend = _RNG.uniform(-min(20, max(3, distance / 8)), min(20, max(3, distance / 8)))
    for step in range(1, steps + 1):
        # Retain the visible path cadence and density. Each frame-sized group
        # contains several finer device samples, rather than stretching the
        # original waypoints farther apart when the browser coalesces them.
        count = _RNG.randint(2, 4)
        points = []
        for sample in range(1, count + 1):
            fraction = (step - 1 + sample / count) / steps
            eased = fraction * fraction * (3 - 2 * fraction)
            arc = math.sin(math.pi * fraction) * bend
            px = start_x + (x - start_x) * eased
            py = start_y + (y - start_y) * eased
            if distance:
                px += arc * (y - start_y) / distance
                py -= arc * (x - start_x) / distance
            else:
                px += arc
            points.append((round(max(0, px) * ratio) / ratio,
                           round(max(0, py) * ratio) / ratio))
        _mouse_samples(page, points, budget)
        budget.pause(_RNG.uniform(0.008, 0.015))


def _border_width(locator: Any, property_name: str, budget: _Budget) -> float:
    # Read computed CSS via Playwright's utility-world expectation transport.
    # Locator.evaluate runs in the main world and would expose internal reads.
    result = locator._sync(locator._impl_obj._expect('to.have.css', {
        'expressionArg': property_name, 'expectedText': [{'regexSource': '.*', 'regexFlags': ''}],
        'isNot': False, 'timeout': budget.remaining(),
    }))
    received = result['received']
    if isinstance(received, dict) and 'value' in received:
        received = received['value']
    if isinstance(received, dict):
        # Older Playwright releases return the wire-format value here.
        from playwright._impl._js_handle import parse_value
        received = parse_value(received)
    if not isinstance(received, str):
        raise ValueError(f'Unexpected computed CSS result for {property_name}')
    # Match Playwright's parseInt border offset, including scaled screens.
    return int(float(received.removesuffix('px')))


def _scroll_plan(handle: Any, budget: _Budget, position: Any = None) -> dict[str, Any]:
    budget.remaining()
    transport = handle.query_selector(f'{_SCROLL_ENGINE}={json.dumps({"position": position})}')
    if transport is None:
        from playwright.sync_api import Error
        raise Error('Unable to inspect scrolling target')
    try:
        result = json.loads(transport.text_content())
    finally:
        transport.dispose()
    budget.remaining()
    return result


def _wheel_target(page: Any, handle: Any, frame: Any, budget: _Budget, position: Any = None) -> None:
    """Reveal a target using native wheel input, including frame ancestors."""
    offset_x = offset_y = 0.0
    scale_x = scale_y = 1.0
    if frame.parent_frame is not None:
        owner = frame.frame_element()
        try:
            _wheel_target(page, owner, frame.parent_frame, budget)
            owner_plan = _scroll_plan(owner, budget)
            box = owner.bounding_box()
            if box is not None:
                scale_x = box['width'] / (owner_plan['offsetWidth'] or box['width'])
                scale_y = box['height'] / (owner_plan['offsetHeight'] or box['height'])
                offset_x = box['x'] + owner_plan['clientLeft'] * scale_x
                offset_y = box['y'] + owner_plan['clientTop'] * scale_y
        finally:
            owner.dispose()
    from .isolated_world import isolated_evaluate
    metrics = isolated_evaluate(page, '() => ({width:innerWidth,height:innerHeight,dpr:devicePixelRatio || 1})')
    while True:
        plan = _scroll_plan(handle, budget, position)
        if plan['rect']['width'] < 2 or plan['rect']['height'] < 2:
            return  # Native click supplies its normal hidden-element error.
        job = next((j for j in reversed(plan['jobs'])
                    if abs(j['dx']) > .5 or abs(j['dy']) > .5), None)
        if job is None:
            return
        point = job['point']
        if point is None:
            # Unreachable clipping cannot be repaired by moving scrollTop.
            budget.pause(.05)
            continue
        x = round((offset_x + point['x'] * scale_x) * metrics['dpr']) / metrics['dpr']
        y = round((offset_y + point['y'] * scale_y) * metrics['dpr']) / metrics['dpr']
        previous = _POINTERS.get(page.mouse)
        if previous is None or math.hypot(x-previous[0],y-previous[1]) > 2:
            _approach(page, x, y, budget, metrics)
        amount = _RNG.uniform(160, 280)
        dx = max(-amount,min(amount,job['dx'])) * scale_x
        dy = max(-amount,min(amount,job['dy'])) * scale_y
        budget.remaining()
        page.mouse.wheel(dx,dy)
        # wheel ACK precedes compositor scrolling. Read fresh geometry after
        # Chrome has had a frame to process the actual input.
        budget.pause(_RNG.uniform(.035,.065))


def _wheel_into_view(locator: Any, budget: _Budget, position: Any = None) -> None:
    handle = locator.element_handle(timeout=budget.remaining())
    try:
        # The channel owner is the exact frame resolved by Playwright's
        # selector engine, including FrameLocator chains. owner_frame() would
        # evaluate ownerDocument in the page's main world unnecessarily.
        from playwright._impl._sync_base import mapping
        frame = mapping.from_impl(handle._impl_obj._frame)
        _wheel_target(locator.page,handle,frame,budget,position)
    finally:
        handle.dispose()


def _enable_pointer_grid(page: Any, ratio: float) -> Any:
    """Lease dispatch-time rounding for generated clicks on the local facade.

    Native Chrome CDP has no such method; it keeps the existing input path.
    This state belongs to the connection and tab, never to the document.
    """
    if getattr(page, '_browserwright_pointer_grid_supported', None) is False:
        return None
    from playwright.sync_api import Error
    from .isolated_world import page_cdp_session
    session = page_cdp_session(page)
    try:
        session.send('Browserwright.setPointerGrid', {'devicePixelRatio': ratio})
    except Error as exc:
        if any(message in str(exc).lower() for message in
               ("wasn't found", 'method not found', 'unknown method', 'not implemented')):
            page._browserwright_pointer_grid_supported = False
            return None
        raise
    page._browserwright_pointer_grid_supported = True
    return session


def _click(native: Any, locator: Any, kwargs: dict[str, Any], *, generated_position: bool = False) -> Any:
    page = locator.page
    budget = _Budget(page, kwargs.get("timeout"))
    if kwargs.get("trial"):
        return native(locator, **kwargs)
    # Final native click owns stability, enabled/receives-events checks,
    # force semantics, modifiers, click_count and navigation.
    _wheel_into_view(locator, budget, kwargs.get('position'))
    box = locator.bounding_box(timeout=budget.remaining())
    if box is None or box["width"] < 2 or box["height"] < 2:
        return native(locator, **{**kwargs, "timeout": budget.remaining()})
    position = kwargs.get("position")
    from .isolated_world import isolated_evaluate
    metrics = isolated_evaluate(
        page, "() => ({width: innerWidth, height: innerHeight, dpr: devicePixelRatio || 1})")
    if position is None:
        viewport = page.viewport_size or metrics
        left = max(0, -box["x"])
        top = max(0, -box["y"])
        right = min(box["width"], viewport["width"] - box["x"])
        bottom = min(box["height"], viewport["height"] - box["y"])
        if right - left < 2 or bottom - top < 2:
            return native(locator, **{**kwargs, "timeout": budget.remaining()})
        # A comfortable interior point, deliberately away from dead center.
        fraction_x = _RNG.uniform(0.23, 0.40) if _RNG.random() < .5 else _RNG.uniform(.60, .77)
        border_x = _border_width(locator, 'border-left-width', budget)
        border_y = _border_width(locator, 'border-top-width', budget)
        ratio = metrics['dpr']
        # Native click truncates to hundredths of a CSS pixel. Choose points
        # representable on both that grid and the physical pixel grid (e.g.
        # quarter-scale screens need multiples of four CSS pixels).
        scale = Fraction(str(ratio)).limit_denominator(100)
        grid = scale.denominator / math.gcd(scale.numerator, 100)
        x = round((box['x'] + left + (right - left) * fraction_x) / grid) * grid
        y = round((box['y'] + top + (bottom - top) * _RNG.uniform(.28, .72)) / grid) * grid
        if not (box['x'] + left < x < box['x'] + right
                and box['y'] + top < y < box['y'] + bottom):
            return native(locator, **{**kwargs, 'timeout': budget.remaining()})
        # Playwright adds border widths to padding-relative positions and
        # truncates the resulting coordinates to two decimals. The epsilon
        # avoids floating-point cancellation moving an exact grid point down.
        position = {'x': x - box['x'] - border_x + .00001,
                    'y': y - box['y'] - border_y + .00001}
    else:
        x = math.trunc((box['x'] + _border_width(locator, 'border-left-width', budget)
                        + position['x']) * 100) / 100
        y = math.trunc((box['y'] + _border_width(locator, 'border-top-width', budget)
                        + position['y']) * 100) / 100
    _approach(page, x, y, budget, metrics)
    # Native actionability can observe a newer target box than the approach.
    # Correct its final absolute coordinates at the dispatch boundary instead
    # of assuming the old padding-relative offset still lands on the grid.
    grid_session = (_enable_pointer_grid(page, metrics['dpr'])
                    if kwargs.get('position') is None or generated_position else None)
    point = None
    options = {**kwargs, "position": position}
    if options.get("delay") is None:
        options["delay"] = _RNG.uniform(35, 95)
    try:
        result = native(locator, **{**options, 'timeout': budget.remaining()})
    finally:
        if grid_session is not None:
            from playwright.sync_api import Error
            try:
                point = grid_session.send('Browserwright.setPointerGrid', {
                    'devicePixelRatio': None}).get('point')
                if point:
                    # Native input can succeed before navigation waiting
                    # times out. Retain its real position on that path too.
                    _POINTERS[page.mouse] = (point['x'], point['y'])
            except Error:
                # A disconnected/closed target also retires its facade lease.
                # Cleanup must not replace the native action's exception.
                pass
    _BUTTONS.get(page.mouse, set()).discard(kwargs.get('button') or 'left')
    _POINTERS[page.mouse] = (point['x'], point['y']) if point else (x, y)
    return result


def _select_all_with_keyboard(keyboard: Any, budget: _Budget) -> None:
    """Select the focused editor through its platform's native shortcut."""
    # Use Playwright's ControlOrMeta shortcut rather than guessing an OS key.
    # Physical events let the browser perform selection itself, including
    # ranges containing replaced contenteditable elements without any text.
    keyboard.down('ControlOrMeta')
    try:
        budget.pause(_RNG.uniform(.025, .065))
        keyboard.down('KeyA')
        try:
            budget.pause(_RNG.uniform(.045, .100))
        finally:
            keyboard.up('KeyA')
        budget.pause(_RNG.uniform(.025, .055))
    finally:
        keyboard.up('ControlOrMeta')


def _ensure_editor_caret(locator: Any, budget: _Budget, force: Any) -> bool:
    """Keep select-all inside the focused editor after clicking rich media."""
    handle = locator.element_handle(timeout=budget.remaining())
    if handle is None:
        return False
    try:
        plan = _scroll_plan(handle, budget)
        if not plan['focused']:
            return False
        caret = plan.get('caret')
        if caret is None:
            return False
        if caret['inside']:
            return True
        point = caret['point']
        if point is not None:
            # A replaced child can receive focus without updating the caret.
            # Exposed editor space accepts an ordinary pointer caret gesture.
            _click(type(locator).click.__wrapped__, locator,
                   {'position': point, 'timeout': budget.remaining(), 'force': force},
                   generated_position=True)
        caret = _scroll_plan(handle, budget).get('caret')
        if caret is None:
            return False
        if not caret['inside']:
            # With no exposed blank space, native focus traversal initializes
            # an editor caret. This can fire blur/focus just as physical Tab
            # navigation does; only use it when the current caret is outside.
            keyboard = locator.page.keyboard
            keyboard.down('Tab')
            try:
                budget.pause(_RNG.uniform(.045, .100))
            finally:
                keyboard.up('Tab')
            keyboard.down('Shift')
            try:
                budget.pause(_RNG.uniform(.025, .065))
                keyboard.down('Tab')
                try:
                    budget.pause(_RNG.uniform(.045, .100))
                finally:
                    keyboard.up('Tab')
                budget.pause(_RNG.uniform(.025, .055))
            finally:
                keyboard.up('Shift')
        final = _scroll_plan(handle, budget)
        return bool(final['focused'] and (final.get('caret') or {}).get('inside'))
    finally:
        handle.dispose()


def _fill(native: Any, locator: Any, value: str, kwargs: dict[str, Any]) -> Any:
    budget = _Budget(locator.page, kwargs.get("timeout"))
    if not isinstance(value, str):
        return native(locator, value, **kwargs)
    # Native validation handles non-strings, select elements and specialized
    # inputs (number/date/time/etc.). Textarea and contenteditable have no type.
    kind = locator.get_attribute("type", timeout=budget.remaining())
    if kind is not None:
        kind = kind.lower()
    if kind not in _TEXT_TYPES:
        return native(locator, value, **{**kwargs, "timeout": budget.remaining()})
    # Keep native fill's errors/actionability for invalid or noneditable
    # targets, and its forced hidden-element behavior. Valid visible text
    # controls receive a trusted pointer gesture before selecting existing text.
    from playwright.sync_api import Error
    try:
        editable = locator.is_editable(timeout=budget.remaining())
    except Error:
        return native(locator, value, **{**kwargs, "timeout": budget.remaining()})
    if (not editable or locator.locator("xpath=self::select").count()
            or locator.bounding_box(timeout=budget.remaining()) is None):
        return native(locator, value, **{**kwargs, "timeout": budget.remaining()})
    locator.click(timeout=budget.remaining(), force=kwargs.get("force"))
    if kwargs.get('force'):
        handle = locator.element_handle(timeout=budget.remaining())
        try:
            focused = handle is not None and _scroll_plan(handle, budget)['focused']
        finally:
            if handle is not None:
                handle.dispose()
        if not focused:
            # A forced pointer action can hit an overlay. Native fill retains
            # its forced focus/replacement semantics for the requested editor.
            return native(locator, value, **{**kwargs, 'timeout': budget.remaining()})
    contenteditable = False
    try:
        current = locator.input_value(timeout=budget.remaining())
    except Error:
        contenteditable = True
        current = locator.inner_text(timeout=budget.remaining())
        if not current:
            # Replaced elements can be selected/removed even without text.
            current = bool(locator.locator(
                'img,video,audio,iframe,object,embed,input,textarea,select,hr').count())
    if contenteditable and not _ensure_editor_caret(locator, budget, kwargs.get('force')):
        # Custom focus/keyboard handlers may prevent caret placement. Keep
        # native contenteditable replacement rather than select the page.
        return native(locator, value, **{**kwargs, 'timeout': budget.remaining()})
    if current:
        keyboard = locator.page.keyboard
        _select_all_with_keyboard(keyboard, budget)
        keyboard.down('Backspace')
        try:
            budget.pause(_RNG.uniform(.045, .100))
        finally:
            keyboard.up('Backspace')
    for character in value:
        budget.remaining()
        remaining = budget.remaining()
        delay = _RNG.uniform(35, 105)
        delay = min(delay, remaining) if remaining else delay
        # Use physical Shift chords for capitals and shifted punctuation;
        # Keyboard.type alone accepts an uppercase key without holding Shift.
        shifted = (f"Key{character}" if 'A' <= character <= 'Z'
                   else _SHIFT_KEYS.get(character))
        if shifted:
            keyboard = locator.page.keyboard
            keyboard.down('Shift')
            try:
                budget.pause(_RNG.uniform(.025, .065))
                keyboard.down(shifted)
                try:
                    budget.pause(delay / 1000)
                finally:
                    keyboard.up(shifted)
                budget.pause(_RNG.uniform(.025, .055))
            finally:
                keyboard.up('Shift')
        else:
            # Retain Playwright's insertion path for non-keyboard Unicode.
            locator.page.keyboard.type(character, delay=delay)
        budget.remaining()
    return None


def install_human_input(context: Any) -> None:
    """Register the executor context and patch its sync API input methods."""
    global _INSTALLED
    from playwright.sync_api import BrowserContext, Frame, Locator, Mouse, Page
    if not isinstance(context, BrowserContext):
        return
    if context not in _SCROLL_CONTEXTS:
        context._sync(context._impl_obj._channel.send('registerSelectorEngine', None, {
            'selectorEngine': {'name': _SCROLL_ENGINE, 'source': _SCROLL_SOURCE,
                               'contentScript': True},
        }))
        _SCROLL_CONTEXTS.add(context)
    _CONTEXTS.add(context)
    if _INSTALLED:
        return
    _INSTALLED = True

    for name, operation in (("click", _click), ("fill", _fill)):
        native = getattr(Locator, name)

        def wrap(native: Any, operation: Any) -> Any:
            @functools.wraps(native)
            def input_method(self: Any, *args: Any, human: bool | None = None, **kwargs: Any) -> Any:
                if not _enabled(self.page, human):
                    return native(self, *args, **kwargs)
                return operation(native, self, *args, kwargs)
            return input_method

        setattr(Locator, name, wrap(native, operation))

    # Selector convenience methods must use the same defaults as chained
    # locators, including within child frames. Page/Frame default to first match;
    # Locator's native strictness is retained.
    for cls in (Page, Frame):
        for name in ("click", "fill"):
            native = getattr(cls, name)

            def wrap_selector(native: Any, name: str, cls: Any) -> Any:
                @functools.wraps(native)
                def input_method(self: Any, selector: str, *args: Any,
                                 human: bool | None = None, **kwargs: Any) -> Any:
                    page = self if cls is Page else self.page
                    if not _enabled(page, human):
                        return native(self, selector, *args, **kwargs)
                    locator = self.locator(selector)
                    if not kwargs.pop("strict", False):
                        locator = locator.first
                    return getattr(locator, name)(*args, human=human, **kwargs)
                return input_method

            setattr(cls, name, wrap_selector(native, name, cls))

    # Track explicit agent mouse calls too; pointer state stays in Python.
    for name in ("move", "click", "dblclick"):
        native = getattr(Mouse, name)

        def wrap_mouse(native: Any, name: str) -> Any:
            @functools.wraps(native)
            def move(self: Any, x: float, y: float, **kwargs: Any) -> Any:
                result = native(self, x, y, **kwargs)
                _POINTERS[self] = (x, y)
                if name != 'move':
                    _BUTTONS.get(self, set()).discard(kwargs.get('button') or 'left')
                return result
            return move

        setattr(Mouse, name, wrap_mouse(native, name))

    for name in ('down', 'up'):
        native = getattr(Mouse, name)

        def wrap_button(native: Any, name: str) -> Any:
            @functools.wraps(native)
            def button(self: Any, **kwargs: Any) -> Any:
                result = native(self, **kwargs)
                held = _BUTTONS.setdefault(self, set())
                pressed = kwargs.get('button') or 'left'
                if name == 'down':
                    held.add(pressed)
                else:
                    held.discard(pressed)
                return result
            return button

        setattr(Mouse, name, wrap_button(native, name))
