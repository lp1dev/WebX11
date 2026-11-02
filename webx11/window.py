import sys
import time

from webx11.settings import SettingsManager
from webx11.capture import encode_full_frame

try:
    import Xlib
    import Xlib.display
    import Xlib.threaded  # noqa: F401  (makes Display objects thread safe)
    from Xlib import X, XK
    from Xlib.ext import xtest
except ImportError:
    print("Warning: Xlib not available. Install with: pip3 install python-xlib")
    sys.exit(1)


class WindowScreenCapture:
    """Legacy full-frame capture (kept for API compatibility)."""

    def __init__(self, window_display):
        self.window_display = window_display
        self.settings = SettingsManager()
        self.last_frame = None

    def capture_window(self, x=0, y=0, height=0, width=0, quality=None, dpi=None, force=False):
        image = self.window_display.grab_image()
        if image is None:
            return None
        frame = encode_full_frame(image, self.settings.image_format, self.settings.image_quality)
        if not force and frame == self.last_frame:
            return None
        self.last_frame = frame
        return frame


# Browser KeyboardEvent.key values -> X keysym names
DOM_KEY_TO_KEYSYM = {
    'Enter': 'Return', 'Tab': 'Tab', 'Backspace': 'BackSpace', 'Escape': 'Escape',
    'Delete': 'Delete', 'Insert': 'Insert', 'Home': 'Home', 'End': 'End',
    'PageUp': 'Prior', 'PageDown': 'Next',
    'ArrowLeft': 'Left', 'ArrowRight': 'Right', 'ArrowUp': 'Up', 'ArrowDown': 'Down',
    'ContextMenu': 'Menu', 'PrintScreen': 'Print', 'Pause': 'Pause', 'ScrollLock': 'Scroll_Lock',
    ' ': 'space', 'Spacebar': 'space',
    # legacy WebX11 names
    'escape': 'Escape', 'esc': 'Escape', 'enter': 'Return', 'return': 'Return', 'tab': 'Tab',
    'backspace': 'BackSpace', 'delete': 'Delete', 'insert': 'Insert', 'home': 'Home', 'end': 'End',
    'pageup': 'Prior', 'pagedown': 'Next', 'left': 'Left', 'right': 'Right', 'up': 'Up',
    'down': 'Down', 'space': 'space',
}
for _i in range(1, 25):
    DOM_KEY_TO_KEYSYM[f'F{_i}'] = f'F{_i}'
    DOM_KEY_TO_KEYSYM[f'f{_i}'] = f'F{_i}'

MODIFIER_KEYSYMS = {
    'ctrl': 'Control_L', 'control': 'Control_L',
    'shift': 'Shift_L',
    'alt': 'Alt_L',
    'meta': 'Super_L', 'super': 'Super_L',
}

SCROLL_BUTTONS = {'up': 4, 'down': 5, 'left': 6, 'right': 7}


def char_to_keysym(ch):
    if ch == '\n' or ch == '\r':
        return XK.string_to_keysym('Return')
    if ch == '\t':
        return XK.string_to_keysym('Tab')
    cp = ord(ch)
    if 0x20 <= cp <= 0x7e or 0xa0 <= cp <= 0xff:
        return cp  # Latin-1 keysyms equal their code point
    return 0x01000000 + cp  # Unicode keysym


class WindowInputHandler:
    """Injects input into one Xvfb display through XTEST.

    Design rules that prevent stuck keys:
      * modifiers are never left pressed between messages: shortcuts arrive
        as one atomic message (key + modifier list) and are pressed/released
        together;
      * text is typed as exact characters, the handler presses Shift itself
        when the character needs it and remaps a spare keycode for characters
        that are not on the X keymap (AltGr / Option characters, accents...);
      * release_all() releases every key and button the X server reports as
        down; it runs on blur, on disconnect and on demand.
    """

    def __init__(self, window_display):
        self.window_display = window_display
        self.display = window_display.get_display()
        if not self.display:
            raise Exception("No X11 display available")
        self.root = self.display.screen().root
        self.buttons_down = set()
        self._scratch_keycodes = []
        self._remapped = {}  # keysym -> keycode
        self._remap_next = 0
        self._shift_keycode = self.display.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
        self._init_scratch_keycodes()

    # ----------------------------------------------------------------- utils
    def _init_scratch_keycodes(self):
        try:
            first = self.display.display.info.min_keycode
            last = self.display.display.info.max_keycode
            mapping = self.display.get_keyboard_mapping(first, last - first + 1)
            for i, syms in enumerate(mapping):
                if all(s == X.NoSymbol for s in syms):
                    self._scratch_keycodes.append(first + i)
            # keep the highest ones, they are the least likely to be real keys
            self._scratch_keycodes = self._scratch_keycodes[-8:]
        except Exception as e:
            print(f"Could not read keyboard mapping: {e}")

    def _fake_key(self, keycode, pressed):
        xtest.fake_input(self.display, X.KeyPress if pressed else X.KeyRelease, keycode)

    def _lookup(self, keysym):
        """Return (keycode, needs_shift) for keysym, remapping a spare key if needed."""
        if keysym in self._remapped:
            return self._remapped[keysym], False
        best = None
        for keycode, index in self.display.keysym_to_keycodes(keysym):
            if index == 0:
                return keycode, False
            if index == 1 and best is None:
                best = (keycode, True)
        if best:
            return best
        return self._remap(keysym), False

    def _remap(self, keysym):
        if not self._scratch_keycodes:
            return None
        keycode = self._scratch_keycodes[self._remap_next % len(self._scratch_keycodes)]
        self._remap_next += 1
        for ks, kc in list(self._remapped.items()):
            if kc == keycode:
                del self._remapped[ks]
        self.display.change_keyboard_mapping(keycode, [(keysym, keysym)])
        self.display.sync()
        time.sleep(0.01)  # give clients time to process MappingNotify
        self._remapped[keysym] = keycode
        return keycode

    def _keysym_for(self, key):
        if key in DOM_KEY_TO_KEYSYM:
            return XK.string_to_keysym(DOM_KEY_TO_KEYSYM[key])
        if len(key) == 1:
            return char_to_keysym(key)
        ks = XK.string_to_keysym(key)
        return ks or None

    # ----------------------------------------------------------- keyboard
    def send_key_combo(self, key, mods=()):
        """Press modifiers + key and release everything (atomic shortcut)."""
        keysym = self._keysym_for(key)
        if not keysym:
            print(f"Unknown key: {key!r}")
            return False
        mods = [m for m in (mods or []) if m in MODIFIER_KEYSYMS]
        keycode, needs_shift = self._lookup(keysym)
        if not keycode:
            return False
        mod_keycodes = []
        for m in mods:
            kc = self.display.keysym_to_keycode(XK.string_to_keysym(MODIFIER_KEYSYMS[m]))
            if kc and kc not in mod_keycodes:
                mod_keycodes.append(kc)
        if needs_shift and self._shift_keycode not in mod_keycodes:
            mod_keycodes.append(self._shift_keycode)
        try:
            for kc in mod_keycodes:
                self._fake_key(kc, True)
            self._fake_key(keycode, True)
            self._fake_key(keycode, False)
        finally:
            for kc in reversed(mod_keycodes):
                self._fake_key(kc, False)
            self.display.sync()
        return True

    def send_text_input(self, text):
        """Type text exactly as given, independent of the X keyboard layout."""
        try:
            for ch in text:
                keysym = char_to_keysym(ch)
                keycode, needs_shift = self._lookup(keysym)
                if not keycode:
                    print(f"Could not type character: {ch!r}")
                    continue
                if needs_shift:
                    self._fake_key(self._shift_keycode, True)
                self._fake_key(keycode, True)
                self._fake_key(keycode, False)
                if needs_shift:
                    self._fake_key(self._shift_keycode, False)
            self.display.sync()
            return True
        except Exception as e:
            print(f"Text input error: {e}")
            return False

    def send_key_event_by_name(self, key_name, pressed=True):
        """Legacy keydown/keyup API. Only key-down is acted upon, as an atomic tap,
        so a lost keyup can never leave a key pressed."""
        if not pressed:
            return True
        if key_name.lower() in MODIFIER_KEYSYMS:
            return True  # bare modifiers are ignored; combos carry their modifiers
        return self.send_key_combo(key_name)

    # -------------------------------------------------------------- mouse
    def _to_root(self, x, y):
        return int(x) + self.window_display.x, int(y) + self.window_display.y

    def move_pointer(self, x, y):
        rx, ry = self._to_root(x, y)
        self.root.warp_pointer(rx, ry)
        self.display.sync()

    def send_mouse_event(self, x, y, button=1, pressed=True, mods=()):
        rx, ry = self._to_root(x, y)
        mod_keycodes = []
        if pressed:
            for m in mods or []:
                if m in MODIFIER_KEYSYMS:
                    mod_keycodes.append(self.display.keysym_to_keycode(XK.string_to_keysym(MODIFIER_KEYSYMS[m])))
        try:
            self.root.warp_pointer(rx, ry)
            for kc in mod_keycodes:
                self._fake_key(kc, True)
            xtest.fake_input(self.display, X.ButtonPress if pressed else X.ButtonRelease, int(button))
            if pressed:
                self.buttons_down.add(int(button))
            else:
                self.buttons_down.discard(int(button))
        finally:
            for kc in reversed(mod_keycodes):
                self._fake_key(kc, False)
            self.display.sync()
        return True

    def send_scroll(self, x, y, dx=0, dy=0):
        rx, ry = self._to_root(x, y)
        self.root.warp_pointer(rx, ry)
        clicks = []
        if dy:
            clicks += [SCROLL_BUTTONS['down' if dy > 0 else 'up']] * min(abs(int(dy)), 20)
        if dx:
            clicks += [SCROLL_BUTTONS['right' if dx > 0 else 'left']] * min(abs(int(dx)), 20)
        for b in clicks:
            xtest.fake_input(self.display, X.ButtonPress, b)
            xtest.fake_input(self.display, X.ButtonRelease, b)
        self.display.sync()
        return True

    def send_scroll_event(self, x, y, delta_y):
        """Legacy API: one click per event in the direction of delta_y."""
        return self.send_scroll(x, y, 0, 1 if delta_y > 0 else -1)

    # ------------------------------------------------------------- safety
    def release_all(self):
        """Release every key and mouse button currently held on this display."""
        released = []
        try:
            keymap = self.display.query_keymap()
            for keycode in range(8, 256):
                if keymap[keycode // 8] & (1 << (keycode % 8)):
                    self._fake_key(keycode, False)
                    released.append(keycode)
            mask = self.root.query_pointer().mask
            for button, bit in ((1, X.Button1Mask), (2, X.Button2Mask), (3, X.Button3Mask)):
                if mask & bit or button in self.buttons_down:
                    xtest.fake_input(self.display, X.ButtonRelease, button)
            self.buttons_down.clear()
            self.display.sync()
        except Exception as e:
            print(f"release_all error: {e}")
        return released
