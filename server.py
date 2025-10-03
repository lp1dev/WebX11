#!/usr/bin/env python3
"""
Main X11 Web Display Server with HTTP API and WebTransport
"""

import os
import sys
import time
import atexit
import asyncio
import subprocess
from sys import argv
from http.server import HTTPServer
from socketserver import ThreadingMixIn
from webx11.api_handler import APIHandler
from webx11.display import WindowDisplayManager
from webx11.settings import SettingsManager
from webx11 import websockets
from webx11 import webtransport

FPS = 30

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads"""
    def __init__(self, window_manager, *args, **kwargs):
        self.window_manager = window_manager
        super().__init__(*args, **kwargs)

def cleanup(window_manager, websocket_handler):
    """Cleanup function to stop all window displays on exit"""
    print("\nCleaning up...")
    websocket_handler.stop_window_broadcast()
    window_manager.stop_all()


def handler_factory(window_manager):
    """Create a handler factory with the managers"""
    def create_handler(*args, **kwargs):
        return APIHandler(window_manager, *args, **kwargs)
    return create_handler

async def main_async(executable_path):
    # Configuration
    HOST = 'localhost'
    HTTP_PORT = 8080
    WEBTRANSPORT_PORT = 4433
    WEBSOCKET_PORT = 8081
    
    print("Starting X11 Web Display Server with HTTP API...")
    print("=" * 50)
    
    # Parsing settings
    settings = SettingsManager('settings.json')

    # Check if Xvfb is available
    try:
        subprocess.run(['which', 'Xvfb'], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("Error: Xvfb is not installed or not in PATH")
        print("Install it with: sudo apt-get install xvfb")
        sys.exit(1)
    
    # Initialize managers
    window_manager = WindowDisplayManager()
    
    websocket_server, websocket_handler = await websockets.run_websocket_server(window_manager, HOST, WEBSOCKET_PORT)
    webtransport_server = await webtransport.run_webtransport_server(window_manager, WEBTRANSPORT_PORT)

    # Start window broadcast
    websocket_handler.start_window_broadcast(interval=0.05) # TODO Check optimizations for this interval
    # webtransport_server.start_window_broadcast(interval=1000/FPS)

    # Register cleanup function
    atexit.register(lambda: cleanup(window_manager, websocket_handler))
    
    print(f"✅ X11 Web Display Server with HTTP API started!")
    print(f"🌐 HTTP interface: http://localhost:{HTTP_PORT}")
    print(f"🔌 WebSocket server: ws://localhost:{WEBSOCKET_PORT}")
    if webtransport_server:
        print(f"🚀 WebTransport server: https://localhost:{WEBTRANSPORT_PORT}")
    print("\nAvailable HTTP Routes:")
    print("  GET  /              - Main interface")
    print("  GET  /applications  - List available applications")
    print("  POST /start         - Start an application")
    print("  GET  /windows       - List running windows") 
    print("  GET  /windows/{id}  - Access specific window")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 50)
    
    # Create and start HTTP server
    # TODO replace the current HTTP server with something more robust using jinja2 templates
    http_server = ThreadedHTTPServer(window_manager, ('0.0.0.0', HTTP_PORT), handler_factory(window_manager))
    
    # Run HTTP server in a separated thread
    import threading
    http_thread = threading.Thread(target=http_server.serve_forever)
    http_thread.daemon = True
    http_thread.start()

    

    # Creating the display
    display = window_manager.create_window_display()
    display.quality = settings.image_quality
    display.dpi = settings.dpi

    if not display:
        raise Exception("Failed to create window display")
            
    # Prepare environment with the new display
    env = os.environ.copy()
    env['DISPLAY'] = display.display_name
            
    # Start the application
    process = subprocess.Popen(
        executable_path,
        shell=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )
    time.sleep(1) # We are waiting for the window to be displayed, so that we can get its actual size
    display.smart_resize()
    try:
        await asyncio.Future()  # run forever
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        websocket_server.close()
        await websocket_server.wait_closed()
        # TODO: Make sure that the webtransport server is closed too
        http_server.shutdown()
        window_manager.stop_all()

def main():
    if len(argv) < 2:
        print(f"""usage: {argv[0]} executable_file""")
        return
    asyncio.run(main_async(argv[1]))

if __name__ == "__main__":
    main()