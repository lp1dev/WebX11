from pathlib import Path

from aiohttp import web

from webx11.api_handler import APIHandler
from webx11.websockets import WebSocketHandler


def create_application(display_manager):
    """Serve the HTTP API and WebSocket upgrades from the same listener."""
    api_handler = APIHandler(display_manager)
    websocket_handler = WebSocketHandler(display_manager)
    app = web.Application()

    async def favicon(request):
        return web.FileResponse(Path(__file__).parent / 'static' / 'favicon.svg',
                                headers={'Content-Type': 'image/svg+xml'})

    async def favicon_ico(request):
        raise web.HTTPFound('/favicon.svg')

    app.router.add_get('/favicon.svg', favicon)
    app.router.add_get('/favicon.ico', favicon_ico)
    app.router.add_get('/ws/{display_id}', websocket_handler.handle_websocket)
    app.router.add_route('*', '/{path:.*}', api_handler.handle_request)

    async def shutdown(app):
        websocket_handler.stop_window_broadcast()
        for client in list(websocket_handler.connected_clients):
            await client['websocket'].close(code=1001, message=b'Server shutting down')

    app.on_shutdown.append(shutdown)
    return app, websocket_handler


async def run_server(display_manager, host='127.0.0.1', port=8080):
    app, websocket_handler = create_application(display_manager)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner, websocket_handler