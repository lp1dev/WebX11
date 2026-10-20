import asyncio
import json
import os
from urllib.parse import urlparse

from aiohttp import web
from webx11.settings import SettingsManager

module_dir = os.path.dirname(os.path.abspath(__file__))
html_path = os.path.join(module_dir, "partials", "display.html")


class APIHandler:
    def __init__(self, display_manager):
        self.display_manager = display_manager
        self.settings = SettingsManager()

    async def handle_request(self, request):
        body = await request.read()
        # Display creation, process startup and X11 calls may block. Keep them
        # off the event loop shared with WebSocket frame delivery.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self.dispatch, request.method, request.path_qs, body)

    def response(self, data=None, status=200, text=None, headers=None):
        headers = dict(headers or {})
        if self.settings.cors_unsafe_allow_all:
            headers.update({
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "GET,POST,OPTIONS,DELETE",
            })
        if text is not None:
            return web.Response(text=text, status=status, headers=headers)
        return web.json_response(data, status=status, headers=headers)

    def error(self, status, message):
        return self.response(status=status, text=message)

    def dispatch(self, method, path, body):
        parsed_path = urlparse(path)
        path = parsed_path.path
        if method == 'OPTIONS':
            return self.response(text='')
        if method == 'GET':
            if path == '/':
                return self.serve_index()
            if path in ('/displays', '/displays/'):
                return self.serve_display_list()
            if path.startswith('/display/'):
                return self.serve_display(parsed_path)
            if path == '/settings.json':
                return self.response(json.loads(self.settings.dump_json()))
        elif method == 'POST':
            if path.startswith('/resize/'):
                return self.handle_resize(parsed_path)
            if path in ('/display', '/display/'):
                return self.handle_create_display(body)
            if (path.startswith('/display/') and '/run' in path
                    and self.settings.can_start_executables):
                return self.handle_start_executable_display(parsed_path, body)
        elif method == 'DELETE' and path.startswith('/display/'):
            return self.handle_close_display(parsed_path)
        return self.error(404, "Not Found")

    def serve_index(self):
        displays = self.display_manager.get_all_displays()
        if displays:
            return self.response(status=302, text='', headers={
                'Location': '/display/%s' % len(displays),
            })
        return self.error(404, "Not Found. No display seems to be running. Try again in a few seconds or start a new one.")

    def handle_start_executable_display(self, parsed_path, body):
        try:
            display_id = int(parsed_path.path.split('/')[2])
        except (ValueError, IndexError):
            return self.error(404, "Invalid display ID. Must be an int.")
        if not body:
            return self.error(400, "No data provided")
        display = self.display_manager.get_display(display_id)
        if not display:
            return self.error(404, "Display ID not found. You need to start a display first.")
        try:
            data = json.loads(body)
            if data.get('executable') is None:
                return self.error(400, "Missing parameter: executable.")
            process = self.display_manager.start_executable(display_id, data.get('executable'))
            display.executable = data.get('executable')
            return self.response({"message": "OK", "display": display.display_id, "process": process.pid})
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.error(400, "Invalid JSON")
        except Exception as e:
            return self.error(500, f"Server error: {str(e)}")

    def serve_display_list(self):
        displays = [display.get_window_info()
                    for display in self.display_manager.get_all_displays()]
        return self.response(displays)

    def serve_display(self, parsed_path):
        try:
            display_id = int(parsed_path.path.split('/')[-1])
        except (ValueError, IndexError):
            return self.error(404, "Invalid display ID")
        if not self.display_manager.get_display(display_id):
            return self.error(404, "Display not found")
        with open(html_path, "r") as f:
            html_content = f.read().format(
                top=0, left=0, app_name=f"Display {display_id}", display_id=display_id)
        response = self.response(text=html_content)
        response.content_type = 'text/html'
        return response

    def handle_close_display(self, parsed_path):
        try:
            display_id = int(parsed_path.path.split('/')[-1])
        except (ValueError, IndexError):
            return self.error(404, "Invalid display ID")
        self.display_manager.remove_display(display_id)
        return self.response({"success": True})

    def handle_create_display(self, body):
        if not body:
            return self.error(400, "No data provided")
        try:
            data = json.loads(body)
            if data.get('width') is None or data.get('height') is None:
                return self.error(400, "Missing parameters width and height")
            display = self.display_manager.create_display(data.get('width'), data.get('height'))
            return self.response({"message": "OK", "display": display.display_id})
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.error(400, "Invalid JSON")
        except Exception as e:
            return self.error(500, f"Server error: {str(e)}")

    def handle_resize(self, parsed_path):
        try:
            display_id, width, height = parsed_path.path.split('/')[2:]
            display_id, width, height = int(display_id), int(width), int(height)
        except (ValueError, IndexError):
            return self.error(404, "Invalid display ID")
        self.display_manager.resize_display(display_id, width, height)
        return self.response({"success": True})