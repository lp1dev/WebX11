#!/usr/bin/env python3
"""
Main X11 Web Display Server with HTTP API and WebTransport
"""

import sys
import atexit
import asyncio
import subprocess
from sys import argv
from http.server import HTTPServer
from socketserver import ThreadingMixIn
from webx11.api_handler import APIHandler
from webx11.display import DisplayManager
from webx11.settings import SettingsManager
from webx11 import websockets
from webx11 import webtransport

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads"""
    def __init__(self, display_manager, *args, **kwargs):
        self.display_manager = display_manager
        super().__init__(*args, **kwargs)

def cleanup(display_manager, websocket_handler):
    """Cleanup function to stop all window displays on exit"""
    print("\nCleaning up...")
    websocket_handler.stop_window_broadcast()
    display_manager.stop_all()


def handler_factory(display_manager):
    """Create a handler factory with the managers"""
    def create_handler(*args, **kwargs):
        return APIHandler(display_manager, *args, **kwargs)
    return create_handler

async def main_async():
    # Configuration
    HOST = '0.0.0.0'
    HTTP_PORT = 8080
    WEBTRANSPORT_PORT = 4433
    WEBTRANSPORT_HOST = 'localhost' # needs to be a name
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
    display_manager = DisplayManager()
    
    websocket_server, websocket_handler = await websockets.run_websocket_server(display_manager, HOST, WEBSOCKET_PORT)
    webtransport_server = await webtransport.run_webtransport_server(display_manager, WEBTRANSPORT_HOST, WEBTRANSPORT_PORT)

    # Start window broadcast
    websocket_handler.start_window_broadcast(interval=round(1.0/settings.fps, 2))

    # Register cleanup function
    atexit.register(lambda: cleanup(display_manager, websocket_handler))
    
    print(f"✅ X11 Web Display Server with HTTP API started!")
    print(f"🌐 HTTP interface: http://{HOST}:{HTTP_PORT}")
    print(f"🔌 WebSocket server: ws://{HOST}:{WEBSOCKET_PORT}")
    if webtransport_server:
        print(f"🚀 WebTransport server: https://{WEBTRANSPORT_HOST}:{WEBTRANSPORT_PORT}")
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
    http_server = ThreadedHTTPServer(display_manager, (HOST, HTTP_PORT), handler_factory(display_manager))
    
    # Run HTTP server in a separated thread
    import threading
    http_thread = threading.Thread(target=http_server.serve_forever)
    http_thread.daemon = True
    http_thread.start()

    # If a parameter is passed, the executable is started
    process = None
    if len(sys.argv) > 1:
        # Creating the display
        display = display_manager.create_display(settings.max_width, settings.max_height)
        display.quality = settings.image_quality
        display.dpi = settings.dpi
        process = display.start_executable(argv[1])

    # Start the main loop
    try:
        await asyncio.Future()  # run forever
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if process:
            process.terminate()
        websocket_server.close()
        await websocket_server.wait_closed()
        # TODO: Make sure that the webtransport server is closed too
        http_server.shutdown()
        display_manager.stop_all()

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()