import sys
import io
import time
import struct

from PIL import Image
from webx11.settings import SettingsManager

try:
    import numpy as np
except ImportError:
    print("Warning: numpy not available. Install with: pip3 install numpy")
    sys.exit(1)

try:
    import mss
except ImportError:
    print("Warning: mss not available. Install with: pip3 install mss")
    sys.exit(1)

try:
    import Xlib
    import Xlib.display
    import Xlib.threaded
    from Xlib import X, XK
    from Xlib.ext import xtest
except ImportError:
    print("Warning: Xlib not available. Install with: pip3 install python-xlib")
    sys.exit(1)


# --- Tile frame wire format (shared with partials/display.html) -------------
#
# Frame header (big-endian, 20 bytes):
#   B  version (=1)
#   B  flags          (bit0 = keyframe: full repaint of the whole frame)
#   H  frame_id
#   H  frame_width
#   H  frame_height
#   H  tile_count
#   H  reserved
#   Q  timestamp_ms
#
# Then, repeated tile_count times (12-byte header + payload):
#   H  x
#   H  y
#   H  w
#   H  h
#   I  data_len
#   <data_len bytes>   encoded image in settings.image_format
FRAME_HDR = struct.Struct('>BBHHHHHQ')
TILE_HDR = struct.Struct('>HHHHI')
PROTO_VERSION = 1
FLAG_KEYFRAME = 0x01

# When more than this fraction of tiles changed, send one full-frame encode
# instead of many small ones (fewer encode calls, real cross-frame compression,
# a single createImageBitmap on the client). Localized edits stay below this
# and use tight dirty regions.
FULLFRAME_FRACTION = 0.6


class WindowScreenCapture:
    def __init__(self, window_display):
        self.window_display = window_display
        self.settings = SettingsManager()
        self.tile_size = getattr(self.settings, 'tile_size', 256)
        self.display_name = window_display.display_name

        # mss connections are NOT thread-safe and have X11 thread affinity, so
        # create it lazily inside whatever worker thread first calls a capture
        # method, and recreate if the thread ever changes.
        self._sct = None
        self._sct_thread = None

        self.prev32 = None          # previous frame as (h, w) uint32, OWN array
        self.frame_id = 0
        self._buf = io.BytesIO()    # reused encode buffer

    def _ensure_sct(self):
        import threading
        tid = threading.get_ident()
        if self._sct is None or self._sct_thread != tid:
            if self._sct is not None:
                try:
                    self._sct.close()
                except Exception:
                    pass
            self._sct = mss.mss(display=self.display_name)
            self._sct_thread = tid

    def _clamp(self, x, y, width, height):
        """Keep the requested region inside the real virtual screen so mss can
        never be asked for an out-of-bounds or zero-size grab."""
        mon = self._sct.monitors[0]  # union of all monitors = the Xvfb screen
        sw, sh = int(mon['width']), int(mon['height'])
        x = max(0, int(x))
        y = max(0, int(y))
        width = max(1, min(int(width), sw - x))
        height = max(1, min(int(height), sh - y))
        return x, y, width, height

    def _encode(self, image):
        self._buf.seek(0)
        self._buf.truncate()
        fmt = self.settings.image_format.lower()
        if fmt == 'png':
            image.save(self._buf, format='PNG', optimize=False, compress_level=1)
        elif fmt == 'webp':
            # method=0 is libwebp's fastest encoder (default is 4, much slower)
            image.save(self._buf, format='WEBP',
                       quality=self.settings.image_quality, method=0)
        else:
            image.save(self._buf, format='JPEG',
                       quality=self.settings.image_quality,
                       optimize=False, subsampling=2)
        return self._buf.getvalue()

    def _pack(self, width, height, tiles, keyframe):
        self.frame_id = (self.frame_id + 1) % 65536
        flags = FLAG_KEYFRAME if keyframe else 0
        ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
        parts = [FRAME_HDR.pack(PROTO_VERSION, flags, self.frame_id,
                                width, height, len(tiles), 0, ts_ms)]
        for (x, y, w, h, data) in tiles:
            parts.append(TILE_HDR.pack(x, y, w, h, len(data)))
            parts.append(data)
        return b''.join(parts)

    def _encode_full(self, raw, width, height):
        """Encode the whole grab as a single full-frame tile."""
        full = Image.frombytes("RGB", (width, height), raw.bgra, "raw", "BGRX")
        return [(0, 0, width, height, self._encode(full))]

    def capture_keyframe(self, x=0, y=0, height=0, width=0):
        """Full-frame keyframe that does NOT touch the diff state. Used to seed
        a freshly connected client without disturbing the shared diff baseline
        that other clients of the same display rely on."""
        try:
            self._ensure_sct()
            x, y, width, height = self._clamp(x, y, width, height)
            raw = self._sct.grab({"left": x, "top": y, "width": width, "height": height})
            tiles = self._encode_full(raw, width, height)
            return self._pack(width, height, tiles, keyframe=True)
        except Exception as e:
            print(f"Window keyframe error: {e}")
            return None

    def capture_window(self, x=0, y=0, height=0, width=0,
                       quality=30, dpi=200, force=False):
        """Capture and return a serialized tile frame, or None if nothing
        changed. Runs inside the display's single-worker capture executor."""
        try:
            self._ensure_sct()
            x, y, width, height = self._clamp(x, y, width, height)

            raw = self._sct.grab({"left": x, "top": y, "width": width, "height": height})
            arr32 = np.frombuffer(raw.bgra, dtype=np.uint32).reshape(height, width)

            # First frame or geometry change -> full keyframe for everyone.
            if self.prev32 is None or self.prev32.shape != arr32.shape:
                force = True

            if force:
                tiles = self._encode_full(raw, width, height)
                self.prev32 = arr32.copy()
                return self._pack(width, height, tiles, keyframe=True)

            # One C-level pass: which pixels differ from the previous frame.
            changed = arr32 != self.prev32
            if not changed.any():
                return None

            ts = self.tile_size
            cols = (width + ts - 1) // ts
            rows = (height + ts - 1) // ts
            total = cols * rows

            # Which tiles contain any change.
            dirty = []
            for r in range(rows):
                ty = r * ts
                th = min(ts, height - ty)
                for c in range(cols):
                    tx = c * ts
                    tw = min(ts, width - tx)
                    if changed[ty:ty + th, tx:tx + tw].any():
                        dirty.append((tx, ty, tw, th))

            if not dirty:
                return None

            # Lots changed -> one full-frame encode beats many small ones.
            if len(dirty) >= total * FULLFRAME_FRACTION:
                tiles = self._encode_full(raw, width, height)
                self.prev32 = arr32.copy()
                return self._pack(width, height, tiles, keyframe=True)

            # Localized change: crop+encode only dirty tiles straight from the
            # raw buffer (no full-frame RGB allocation), and update prev only
            # where it actually changed (cheap memcpy of a few tiles).
            arr8 = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(height, width, 4)
            tiles = []
            for (tx, ty, tw, th) in dirty:
                sub = arr8[ty:ty + th, tx:tx + tw, :3][:, :, ::-1]  # BGR -> RGB
                img = Image.fromarray(np.ascontiguousarray(sub), "RGB")
                tiles.append((tx, ty, tw, th, self._encode(img)))
                self.prev32[ty:ty + th, tx:tx + tw] = arr32[ty:ty + th, tx:tx + tw]

            return self._pack(width, height, tiles, keyframe=False)

        except Exception as e:
            print(f"Window capture error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


class WindowInputHandler:
    def __init__(self, window_display):
        self.window_display = window_display
        self.display = window_display.get_display()
        if not self.display:
            raise Exception("No X11 display available")
        self.root = self.display.screen().root

        # Key mapping
        self.key_map = self._create_key_map()

    def _create_key_map(self):
        """Create mapping from common key names to X11 keycodes"""
        key_map = {}

        # Alphabet keys (a-z)
        for i, char in enumerate("abcdefghijklmnopqrstuvwxyz"):
            key_map[char] = 97 + i
            key_map[char.upper()] = 65 + i

        # Number keys (0-9)
        for i in range(10):
            key_map[str(i)] = 0x0030 + i

        # Special characters
        key_map.update({
            ' ': 0x0020,  # Space
            '\n': 0xff8d,  # Enter
            '\t': 0xff09,  # Tab
            '`': 0x0ad0, '~': 0x007e,
            '!': 0x0021, '@': 0x0040, '#': 0x0af5, '$': 0x0024, '%': 0x0025, '^': 15, '&': 0x0026, '*': 0x002a,
            '(': 0x0028, ')': 0x0029, '-': 0x002d, '_': 0x005f, '=': 0x003d, '+': 0x002b,
            '[': 0x005b, '{': 0x007b, ']': 0x005d, '}': 0x007d, '\\': 0x005c, '|': 0x007c,
            ';': 0x003b, ':': 0x003a, "'": 0x0027, '"': 48,
            ',': 0x002c, '<': 0x003c, '.': 0x002e, '>': 0x003e, '/': 0x002f, '?': 0x003f, '¨': 0x0afe, '´': 0x0afd
        })

        # Function keys
        for i in range(1, 13):
            key_map[f'f{i}'] = 0xffbe + i
            key_map[f'F{i}'] = 0xffbe + i

        # Control keys
        key_map.update({
            'escape': 0xff1b, 'esc': 0xff1b,
            'backspace': 0xff08,
            'enter': 0xff8d, 'return': 0xff0d,
            'tab': 0xff09,
            'capslock': 0xffe5,
            'shift': 0xffe1, 'shift_l': 0xffe1, 'shift_r': 0xffe2,
            'control': 0xffe3, 'ctrl': 0xffe3, 'control_l': 0xffe3, 'control_r': 0xffe4,
            'alt': 0xffe9, 'alt_l': 0xffe9, 'alt_r': 0xffea,
            'super': 0xffe7, 'super_l': 0xffe7, 'super_r': 0xffe8, 'windows': 0xffe7,
            'space': 0x0020,
            'left': 0xff51, 'right': 0xff53, 'up': 0xff52, 'down': 0xff54,
            'insert': 0xff63, 'delete': 0xff9f, 'home': 0xff50, 'end': 0xff57,
            'pageup': 0xff55, 'pagedown': 0xff56,
            'numlock': 0xff7f, 'scrolllock': 0xff14,
        })

        return key_map

    def send_mouse_event(self, x, y, button=1, pressed=True):
        """Send mouse event to this window's display"""
        try:
            event_type = X.ButtonPress if pressed else X.ButtonRelease
            self.root.warp_pointer(x + self.window_display.x, y + self.window_display.y)
            self.display.sync()
            xtest.fake_input(self.display, event_type, button)
            self.display.sync()
            return True
        except Exception as e:
            print(f"Mouse event error: {e}")
            return False

    def send_scroll_event(self, x, y, delta_y):
        """Send scroll wheel event to this window's display"""
        try:
            self.root.warp_pointer(x, y)
            self.display.sync()
            button = 5 if delta_y > 0 else 4  # 5 = down, 4 = up
            xtest.fake_input(self.display, X.ButtonPress, button)
            self.display.sync()
            xtest.fake_input(self.display, X.ButtonRelease, button)
            self.display.sync()
            return True
        except Exception as e:
            print(f"Scroll event error: {e}")
            return False

    def send_key_event(self, keycode, pressed=True):
        """Send keyboard event to this window's display"""
        try:
            keycode = self.display.keysym_to_keycode(keycode)
            event_type = X.KeyPress if pressed else X.KeyRelease
            xtest.fake_input(self.display, event_type, keycode)
            self.display.sync()
            return True
        except Exception as e:
            print(f"Key event error: {e}")
            return False

    def send_key_event_by_name(self, key_name, pressed=True):
        """Send keyboard event using key name"""
        try:
            keycode = self.key_map.get(key_name.lower())
            if keycode is None:
                print(f"Unknown key: {key_name}")
                return False
            return self.send_key_event(keycode, pressed)
        except Exception as e:
            print(f"Key event by name error: {e}")
            return False

    def send_text_input(self, text):
        """Send text input by simulating key presses for each character"""
        try:
            for char in text:
                keycode = self.key_map.get(char)
                if keycode is not None:
                    self.send_key_event(keycode, True)
                    self.send_key_event(keycode, False)
                else:
                    keysym = XK.string_to_keysym(char)
                    if keysym == 0:
                        keysym = XK.string_to_keysym(char.upper())
                    if keysym != 0:
                        keycode = self.display.keysym_to_keycode(keysym)
                        if keycode:
                            self.send_key_event(keycode, True)
                            self.send_key_event(keycode, False)
                        else:
                            print(f"Could not find keycode for character: {char}")
                    else:
                        print(f"Could not find keysym for character: {char}")
            return True
        except Exception as e:
            print(f"Text input error: {e}")
            return False
