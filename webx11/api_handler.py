import json
from http.server import BaseHTTPRequestHandler
from webx11.settings import SettingsManager
from urllib.parse import urlparse
import os

module_dir = os.path.dirname(os.path.abspath(__file__))
html_path = os.path.join(module_dir, "partials", "display.html")

class APIHandler(BaseHTTPRequestHandler):
    def __init__(self, display_manager, *args, **kwargs):
        self.display_manager = display_manager
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
        elif path == '/displays/' or path == '/displays':
            self.serve_display_list()
        elif path.startswith('/display/'):
            self.serve_display(parsed_path)
        elif path == '/settings.json':
            self.serve_settings()
        else:
            self.send_error(404, "Not Found")
    
    def do_POST(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        if path.startswith('/resize/'):
            self.handle_resize(parsed_path)
        elif path == '/display' or path == '/display/':
            self.handle_create_display()
        elif path.startswith('/display/') and '/run' in path and self.settings.can_start_executables:
            self.handle_start_executable_display(parsed_path)
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        if path.startswith('/display/'):
            self.handle_close_display(parsed_path)
        else:
            self.send_error(404, "Not Found")
    
    def serve_index(self):
        self.send_response(302)
        id = len(self.display_manager.get_all_displays())
        if id > 0:
            self.send_header('Location', '/display/%s' %id)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
        else:
            self.send_error(404, "Not Found. No display seems to be running. Try again in a few seconds or start a new one.")

    def serve_settings(self):
        self.send_response(200)
        self.send_cors_headers()
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(self.settings.dump_json().encode('utf-8'))

    def handle_start_executable_display(self, parsed_path):
        display_id = None
        try:
            sections = parsed_path.path.split('/')
            print(sections)
            display_id = int(sections[2])
        except (ValueError, IndexError) as e:
            print(e)
            self.send_error(404, "Invalid display ID. Must be an int.")
            return
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "No data provided")
            return
        display = self.display_manager.get_display(display_id)
        if not display:
            self.send_error(404, "Display ID not found. You need to start a display first.")
            return
        try:
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data)
            if data.get('executable') is None:
                self.send_error(400, "Missing parameter: executable.")
                return
            
            process = self.display_manager.start_executable(display_id, data.get('executable'))
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"message": "OK", "display": display.display_id, "process": process.pid}).encode('utf-8'))
            
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
        except Exception as e:
            self.send_error(500, f"Server error: {str(e)}")


    def serve_display_list(self):
        self.send_response(200)
        self.send_cors_headers()
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        
        displays = []
        for display in self.display_manager.get_all_displays():
            display_info = display.get_window_info()
            displays.append(display_info)
        
        self.wfile.write(json.dumps(displays).encode('utf-8'))
    
    def serve_display(self, parsed_path):
        try:
            display_id = int(parsed_path.path.split('/')[-1])
        except (ValueError, IndexError):
            self.send_error(404, "Invalid display ID")
            return
        
        display = self.display_manager.get_display(display_id)
        if not display:
            self.send_error(404, "Display not found")
            return
        
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        
        app_name = f"Display {display_id}"
    
        with open(html_path, "r") as f:
            html_content = f.read()
            html_content = html_content.format(top=0, left=0, app_name=app_name, display_id=display_id)
        self.wfile.write(html_content.encode('utf-8'))
    
    def handle_close_display(self, parsed_path):
        try:
            display_id = int(parsed_path.path.split('/')[-1])
        except (ValueError, IndexError):
            self.send_error(404, "Invalid display ID")
            return
        self.display_manager.remove_display(display_id)
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
        return

    def handle_create_display(self):
        """ Start a new X11 display"""
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            self.send_error(400, "No data provided")
            return
        
        # Verifying parameters
        try:
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data)
            if data.get('width') is None or data.get('height') is None:
                self.send_error(400, "Missing parameters width and height")
                return
            
            # Creating a new display
            display = self.display_manager.create_display(data.get('width'), data.get('height'))
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"message": "OK", "display": display.display_id}).encode('utf-8'))
            
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
        except Exception as e:
            self.send_error(500, f"Server error: {str(e)}")

    def handle_resize(self, parsed_path):
        try:
            display_id, width, height = parsed_path.path.split('/')[2:]
        except (ValueError, IndexError):
            self.send_error(404, "Invalid display ID")
            return
        self.display_manager.resize_display(int(display_id), int(width), int(height))
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
        return