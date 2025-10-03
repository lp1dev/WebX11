import json
from http.server import BaseHTTPRequestHandler
from webx11.settings import SettingsManager
from urllib.parse import urlparse

class APIHandler(BaseHTTPRequestHandler):
    def __init__(self, window_manager, *args, **kwargs):
        self.window_manager = window_manager
        self.settings = SettingsManager('settings.json')
        super().__init__(*args, **kwargs)
    
    def log_message(self, format, *args):
        pass

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()
    
    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        if path == '/':
            self.serve_index()
        elif path == '/windows':
            self.serve_windows_list()
        elif path.startswith('/windows/'):
            self.serve_window(parsed_path)
        elif path == '/settings.json':
            self.serve_settings()
        else:
            self.send_error(404, "Not Found")
    
    def do_POST(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        
        if path.startswith('/resize/'):
            self.handle_resize_application(parsed_path)
        else:
            self.send_error(404, "Not Found")
    
    def serve_index(self):
        self.send_response(302)
        for window in self.window_manager.get_all_windows():
            self.send_header('Location', '/windows/%s' %window.window_id)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            break

    def serve_settings(self):
        self.send_response(200)
        self.send_cors_headers()
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(self.settings.dump_json().encode('utf-8'))

    def serve_windows_list(self):
        self.send_response(200)
        self.send_cors_headers()
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        
        windows = []
        for window_display in self.window_manager.get_all_windows():
            window_info = window_display.get_window_info()
            windows.append(window_info)
        
        self.wfile.write(json.dumps(windows).encode('utf-8'))
    
    def serve_window(self, parsed_path):
        try:
            window_id = int(parsed_path.path.split('/')[-1])
        except (ValueError, IndexError):
            self.send_error(404, "Invalid window ID")
            return
        
        window_display = self.window_manager.get_window_display(window_id)
        if not window_display:
            self.send_error(404, "Window not found")
            return
        
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        
        app_name = f"Window {window_id}"
    
        with open("partials/appwindow.html", "r") as f:
            html_content = f.read()
            html_content = html_content.format(top=0, left=0, app_name=app_name, window_id=window_id)
        self.wfile.write(html_content.encode('utf-8'))
    
    def handle_stop_application(self, parsed_path):
        try:
            window_id = int(parsed_path.path.split('/')[-1])
        except (ValueError, IndexError):
            self.send_error(404, "Invalid window ID")
            return
        self.window_manager.remove_window_display(window_id)
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
        return
    
    def handle_resize_application(self, parsed_path):
        try:
            window_id, width, height = parsed_path.path.split('/')[2:]
        except (ValueError, IndexError):
            self.send_error(404, "Invalid window ID")
            return
        self.window_manager.resize_window_display(int(window_id), int(width), int(height))
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
        return
