import json

class SettingsManager:
    def __init__(self, filename):
        self.settings_file = filename
        with open(filename) as f:
            self.settings = json.load(f)
        self.check_settings()

    def check_settings(self):
        # Either resize-x11 (resize x11 window based on viewport size), 
        #       none (no resizing), stretch (maximize in width and height)
        if self.settings.get('resize_mode') in ['resize-x11', 'none', 'stretch']:
            self.resize_mode = self.settings.get('resize_mode')
        if self.settings.get('transport') in ['websocket', 'webtransport']:
            self.transport = self.settings.get('transport')
        if isinstance(self.settings.get('image_quality'), int):
            image_quality = self.settings.get('image_quality')
            if image_quality <= 100 and image_quality >= 10:
                self.image_quality = image_quality
        if isinstance(self.settings.get('dpi'), int):
            dpi = self.settings.get('dpi')
            if dpi <= 1500 and dpi >= 100:
                self.dpi = dpi
        if isinstance(self.settings.get('max_width'), int):
            self.max_width = self.settings.get('max_width')
        if isinstance(self.settings.get('max_height'), int):
            self.max_height = self.settings.get('max_height')
        if isinstance(self.settings.get('max_fps'), int):
            self.fps = self.settings.get('max_fps')
            self.max_fps = self.settings.get('max_fps')
        return
    
    def dump_json(self):
        return json.dumps({ "settings" : {
            "resize_mode": self.resize_mode,
            "transport": self.transport
        }})