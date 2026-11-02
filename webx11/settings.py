import json
import os

DEFAULTS = {
    # resize-x11 (resize X windows to the browser viewport), none, stretch
    "resize_mode": "resize-x11",
    # JPEG quality used for large changed regions
    "image_quality": 80,
    "image_format": "JPEG",
    "dpi": 300,
    # DPI announced by Xvfb to X clients (font sizes)
    "x_dpi": 96,
    # Upper bound of a display framebuffer; windows can never be larger
    "max_width": 3840,
    "max_height": 2160,
    "max_fps": 30,
    # Changed regions up to this many pixels are sent lossless (PNG)
    "lossless_max_area": 160000,
    # Re-send JPEG regions as PNG once the screen has been idle this long (ms), 0 = off
    "lossless_refresh_ms": 400,
    # Directory for Xvfb framebuffers (default: /dev/shm when it has room, else /tmp)
    "fbdir": None,
    "can_start_executables": False,
    "host": "127.0.0.1",
    "cors_unsafe_allow_all": False,
}


class SettingsManager:
    def __init__(self, filename='settings.json'):
        self.settings_file = filename
        if os.path.isfile(filename):
            path = filename
        else:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        with open(path) as f:
            self.settings = json.load(f)
        self.check_settings()

    def _get(self, key, kind=None, lo=None, hi=None):
        value = self.settings.get(key, DEFAULTS[key])
        if kind is not None and not isinstance(value, kind):
            return DEFAULTS[key]
        if lo is not None and value < lo:
            return DEFAULTS[key]
        if hi is not None and value > hi:
            return DEFAULTS[key]
        return value

    def check_settings(self):
        self.resize_mode = self._get('resize_mode')
        if self.resize_mode not in ('resize-x11', 'none', 'stretch'):
            self.resize_mode = DEFAULTS['resize_mode']
        self.image_quality = self._get('image_quality', int, 10, 100)
        self.image_format = self._get('image_format') or 'JPEG'
        self.dpi = self._get('dpi', int, 100, 1500)
        self.x_dpi = self._get('x_dpi', int, 48, 384)
        self.max_width = self._get('max_width', int, 320, 16384)
        self.max_height = self._get('max_height', int, 240, 16384)
        self.fps = self.max_fps = self._get('max_fps', int, 1, 120)
        self.lossless_max_area = self._get('lossless_max_area', int, 0)
        self.lossless_refresh_ms = self._get('lossless_refresh_ms', int, 0)
        self.fbdir = self.settings.get('fbdir') or None
        self.host = self._get('host')
        self.cors_unsafe_allow_all = bool(self._get('cors_unsafe_allow_all'))
        self.can_start_executables = self._get('can_start_executables', bool)

    def dump_json(self):
        return json.dumps({"settings": {
            "resize_mode": self.resize_mode,
            "image_format": self.image_format,
            "max_height": self.max_height,
            "max_width": self.max_width,
            "max_fps": self.max_fps,
            "protocol": 2,
        }})
