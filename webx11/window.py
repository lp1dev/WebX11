import sys
import io
from PIL import Image
from webx11.settings import SettingsManager

try:
    import Xlib
    import Xlib.display
    from Xlib import X, XK
    from Xlib.ext import xtest
except ImportError:
    print("Warning: Xlib not available. Install with: pip3 install python-xlib")
    sys.exit(1)

class WindowScreenCapture:
    def __init__(self, window_display):
        self.window_display = window_display
        self.display = window_display.get_display()
        if not self.display:
            raise Exception("No X11 display available")
        self.screen = self.display.screen()
        self.root = self.screen.root
        self.settings = SettingsManager()
        
    def capture_window(self, x=0, y=0, height=0, width=0, quality=30, dpi=200, force=False):
        try:
            image, raw = None, None
            geometry = self.root.get_geometry()

            raw = self.root.get_image(0, 0, width, height, X.ZPixmap, 0xffffffff)
            image = Image.frombytes("RGB", (width, height), raw.data, "raw", "BGRX")

            buffer = io.BytesIO()
            image.save(buffer, format=self.settings.image_format, dpi=[dpi, dpi], quality=quality, compression_level=9)
            return buffer.getvalue()

        except Exception as e:
            print(f"Window capture error: {e}")
            return self.create_blank_image()
    
    def create_blank_image(self):
        """Create a blank image when capture fails"""
        print('create_blank_image:: width, height', self.window_display.width, self.window_display.height)

        try:
            image = Image.new('RGB', (self.window_display.width, self.window_display.height), color='lightgray')
            buffer = io.BytesIO()
            image.save(buffer, format=self.settings.image_format)
            return buffer.getvalue()
        except:
            return None

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
            
            # Move pointer first
            self.root.warp_pointer(x + self.window_display.x, y + self.window_display.y)
            self.display.sync()
            
            # Send button event
            xtest.fake_input(self.display, event_type, button)
            self.display.sync()
            return True

        except Exception as e:
            raise e
            print(f"Mouse event error: {e}")
            return False
    
    def send_scroll_event(self, x, y, delta_y):
        """Send scroll wheel event to this window's display"""
        try:
            # Move pointer to the scroll position
            self.root.warp_pointer(x, y)
            self.display.sync()
            
            # Determine scroll direction and button
            if delta_y > 0:
                # Scroll down
                button = 5
            else:
                # Scroll up
                button = 4
            
            # Send scroll events (press and release)
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
            # Look up keycode in our mapping
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
                # Look up keycode in our mapping first
                keycode = self.key_map.get(char)
                if keycode is not None:
                    self.send_key_event(keycode, True)
                    self.send_key_event(keycode, False)
                else:
                    # Try using XK for other characters
                    keysym = XK.string_to_keysym(char)
                    if keysym == 0:
                        # Try uppercase
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