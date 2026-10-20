#!/usr/bin/env python3
"""
Main X11 Web Display Server with HTTP API and WebTransport
"""

import sys
import atexit
import asyncio
import subprocess
from sys import argv
from webx11.http_server import run_server
from webx11.display import DisplayManager
from webx11.settings import SettingsManager

def cleanup(display_manager, websocket_handler):
    """Cleanup function to stop all window displays on exit"""
    print("\nCleaning up...")
    websocket_handler.stop_window_broadcast()
    display_manager.stop_all()


async def main_async():
    # Configuration
    HTTP_PORT = 8080
    WEBTRANSPORT_PORT = 4433

    print("Starting X11 Web Display Server with HTTP API...")
    print("=" * 50)

    # Parsing settings
    settings = SettingsManager()

    HOST = settings.host
    WEBTRANSPORT_HOST = settings.webtransport_host
    if settings.transport == 'webtransport':
        from webx11 import webtransport

    # Check if Xvfb is available
    try:
        subprocess.run(['which', 'Xvfb'], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("Error: Xvfb is not installed or not in PATH")
        print("Install it with: sudo apt-get install xvfb")
        sys.exit(1)

    # Initialize managers
    display_manager = DisplayManager()

    http_server, websocket_handler = await run_server(display_manager, HOST, HTTP_PORT)

    webtransport_server = None
    if settings.transport == 'webtransport':
        webtransport_server = await webtransport.run_webtransport_server(display_manager, WEBTRANSPORT_HOST, WEBTRANSPORT_PORT)

    # Start window broadcast
    websocket_handler.start_window_broadcast(interval=round(1.0/settings.fps, 2))

    # Register cleanup function
    atexit.register(lambda: cleanup(display_manager, websocket_handler))

    print(f"X11 Web Display Server with HTTP API started!")
    print(f"HTTP interface: http://{HOST}:{HTTP_PORT}")
    print(f"WebSocket endpoint: ws://{HOST}:{HTTP_PORT}/ws/{{display_id}}")
    if webtransport_server:
        print(f"WebTransport server: https://{WEBTRANSPORT_HOST}:{WEBTRANSPORT_PORT}")
    print("\nAvailable HTTP Routes:")
    print("  GET  /              - Main interface")
    print("  GET  /displays      - List running displays")
    print("  POST /display       - Start a display")
    print("  GET  /display/{id}  - Access specific display")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 50)

    # If a parameter is passed, the executable is started
    process = None
    if len(sys.argv) > 1:
        display = display_manager.create_display(settings.max_width, settings.max_height)
        process = display_manager.start_executable(display.display_id, argv[1])

    # Start the main loop
    try:
        await asyncio.Future()  # run forever
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if process:
            process.terminate()
        await http_server.cleanup()
        # TODO: Make sure that the webtransport server is closed too
        display_manager.stop_all()

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
