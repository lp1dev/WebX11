import json
import time
import asyncio
import websockets
import base64
from datetime import datetime
from webx11.settings import SettingsManager

IMAGES_SENT = 0

class WebSocketHandler:
    def __init__(self, window_display_manager):
        self.window_manager = window_display_manager
        self.connected_clients = []
        self.window_update_task = None
        self.settings = SettingsManager()
        self.lastupdate = None
        
    async def handle_websocket(self, websocket, path="/"):
        path = websocket.request.path
        try:
            display_id = int(path.strip('/').split('/')[-1])
        except (ValueError, IndexError):
            print(f"Invalid WebSocket path: {path}")
            return
        
        window_display = self.window_manager.get_display(display_id)
        if not window_display:
            print(f"Window {display_id} not found for WebSocket connection")
            return
            
        client = {"websocket": websocket, "display_id": display_id}
        self.connected_clients.append(client)
        print(f"WebSocket client connected for window {display_id}. Total clients: {len(self.connected_clients)}")
        
        try:
            await self.send_settings(websocket)
            await self.send_window_update(websocket, display_id, force=True)
            async for message in websocket:
                await self.handle_client_message(websocket, message, display_id)
        except websockets.exceptions.ConnectionClosed:
            print(f"WebSocket connection closed for window {display_id}")
        finally:
            for client in self.connected_clients:
                if client.get('websocket') == websocket:
                    self.connected_clients.remove(client)
            print(f"WebSocket client disconnected for window {display_id}. Total clients: {len(self.connected_clients)}")
    
    async def send_settings(self, websocket):
        await websocket.send(self.settings.dump_json())

    async def handle_client_message(self, websocket, message, display_id):
        """Handle incoming WebSocket messages for a specific window"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')
            
            window_display = self.window_manager.get_display(display_id)
            if not window_display or not window_display.input_handler:
                return
            
            if msg_type == 'mousedown':
                await self.handle_mouse_event(websocket, data, True, display_id)
            elif msg_type == 'mouseup':
                await self.handle_mouse_event(websocket, data, False, display_id)
            elif msg_type == 'mousemove':
                await self.handle_mouse_move(websocket, data, display_id)
            elif msg_type == 'scroll':
                await self.handle_scroll_event(websocket, data, display_id)
            elif msg_type == 'keydown':
                await self.handle_key_event(websocket, data, True, display_id)
            elif msg_type == 'keyup':
                await self.handle_key_event(websocket, data, False, display_id)
            elif msg_type == 'text_input':
                await self.handle_text_input(websocket, data, display_id)
            elif msg_type == 'refresh':
                await self.send_window_update(websocket, display_id)
            elif msg_type == 'resize':
                if data.get('height') and data.get('width'):
                    if window_display.height != data.get('height') or data.get('width') != window_display.width:
                        window_display.force_resize(data.get('height'), data.get('width'))
                
        except json.JSONDecodeError as e:
            print(f"Invalid JSON message: {e}")
        except Exception as e:
            print(f"Error handling client message: {e}")
    
    async def handle_mouse_event(self, websocket, data, pressed, display_id):
        x = data.get('x')
        y = data.get('y')
        button = data.get('button', 1)
        
        if x is not None and y is not None:
            window_display = self.window_manager.get_display(display_id)
            if window_display and window_display.input_handler:
                window_display.input_handler.send_mouse_event(x, y, button, pressed)
    
    async def handle_mouse_move(self, websocket, data, display_id):
        x = data.get('x')
        y = data.get('y')
        
        if x is not None and y is not None:
            window_display = self.window_manager.get_display(display_id)
            if window_display and window_display.input_handler:
                try:
                    window_display.input_handler.root.warp_pointer(x + window_display.x, y + window_display.y)
                    window_display.input_handler.display.sync()
                except Exception as e:
                    print(f"Mouse move error: {e}")
    
    async def handle_scroll_event(self, websocket, data, display_id):
        x = data.get('x')
        y = data.get('y')
        delta_y = data.get('deltaY', 0)
        
        if x is not None and y is not None and delta_y != 0:
            window_display = self.window_manager.get_display(display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_scroll_event(x, y, delta_y)

    
    async def handle_key_event(self, websocket, data, pressed, display_id):
        key = data.get('key')
        
        if key:
            window_display = self.window_manager.get_display(display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_key_event_by_name(key, pressed)

    async def handle_text_input(self, websocket, data, display_id):
        text = data.get('text', '')
        
        if text:
            window_display = self.window_manager.get_display(display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_text_input(text)
    
    async def send_window_update(self, websocket, display_id, force=False):
        if self.lastupdate is None:
            self.lastupdate = datetime.now()
        self.lastupdate = datetime.now()
        global IMAGES_SENT
        try:
            window_display = self.window_manager.get_display(display_id)
            if window_display:
                window_image = window_display.capture_window(compressed=False, force=force)
                
                if window_image:
                    IMAGES_SENT += 1
                    print("[Send %s images via WebSocket (update) for window %s]" %(IMAGES_SENT, display_id), len(window_image))
                    await websocket.send(window_image)

        except Exception as e:
            print(f"Error sending window update for {display_id}: {e}")

    async def broadcast_window_updates(self, interval=2.0):
        while True:
            try:
                if self.connected_clients:
                    disconnected = []
                    for client in self.connected_clients:
                        await self.send_window_update(client.get('websocket'), client.get('display_id'), force=False)
                    for client in disconnected:
                        self.connected_clients.remove(client)
                            
            except Exception as e:
                print(f"Error in window broadcast: {e}")
            
            await asyncio.sleep(interval)
    
    def start_window_broadcast(self, interval=2.0):
        if self.window_update_task is None:
            self.window_update_task = asyncio.create_task(self.broadcast_window_updates(interval))
    
    def stop_window_broadcast(self):
        if self.window_update_task:
            self.window_update_task.cancel()
            self.window_update_task = None


async def run_websocket_server(window_manager, host='127.0.0.1', port=8081):
    # Initialize WebSocket handler
    websocket_handler = WebSocketHandler(window_manager)
    
    # Start WebSocket server
    websocket_server = await websockets.serve(
        websocket_handler.handle_websocket, 
        host, 
        port
    )

    return websocket_server, websocket_handler
