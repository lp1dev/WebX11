"""Client input message handling for the WebSocket server.

Messages (JSON, client -> server):
    {type: 'mousemove', x, y}
    {type: 'mousedown' | 'mouseup', x, y, button, mods?}
    {type: 'scroll', x, y, dx?, dy?}            dx/dy are wheel clicks (signed)
    {type: 'scroll', x, y, deltaY}               legacy
    {type: 'key', key, mods?}                    atomic shortcut / special key
    {type: 'text_input', text}                   exact characters to type
    {type: 'paste', text, key?, mods?}           set X clipboard, then press key combo
    {type: 'release_all'}                        release every held key/button
    {type: 'keydown' | 'keyup', key}             legacy, keydown -> atomic tap
    {type: 'resize', width, height}
"""

import asyncio


def handle_input(display, data):
    """Apply one input message. Returns True when handled. Runs on the event loop."""
    handler = display.input_handler if display else None
    if not handler:
        return False
    t = data.get('type')
    try:
        if t == 'mousemove':
            handler.move_pointer(data['x'], data['y'])
        elif t in ('mousedown', 'mouseup'):
            handler.send_mouse_event(data['x'], data['y'], data.get('button', 1),
                                     t == 'mousedown', data.get('mods') or ())
        elif t == 'scroll':
            if 'dx' in data or 'dy' in data:
                handler.send_scroll(data['x'], data['y'], data.get('dx', 0), data.get('dy', 0))
            elif data.get('deltaY'):
                handler.send_scroll_event(data['x'], data['y'], data['deltaY'])
        elif t == 'key':
            handler.send_key_combo(str(data.get('key', '')), data.get('mods') or ())
        elif t == 'text_input':
            text = data.get('text') or ''
            if text:
                handler.send_text_input(text)
        elif t == 'release_all':
            handler.release_all()
        elif t == 'keydown':
            handler.send_key_event_by_name(str(data.get('key', '')), True)
        elif t == 'keyup':
            pass  # keydown already sent a complete tap
        elif t == 'resize':
            w, h = data.get('width'), data.get('height')
            if w and h and (display.width != int(w) or display.height != int(h)):
                display.force_resize(int(h), int(w))
        else:
            return False
    except (KeyError, TypeError, ValueError) as e:
        print(f"Malformed input message {data!r}: {e}")
    except Exception as e:
        print(f"Input error for {t}: {e}")
    return True


async def handle_paste(display, data):
    """Put text on the X clipboard (and PRIMARY) then replay the paste shortcut."""
    text = data.get('text')
    if text is None or not display or not display.input_handler:
        return
    await asyncio.to_thread(display.write_clipboard, text)
    key = data.get('key')
    if key:
        display.input_handler.send_key_combo(key, data.get('mods') or ())
