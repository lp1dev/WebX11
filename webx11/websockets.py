"""WebSocket transport.

One capture loop per display (shared by all its clients) and one sender
coroutine per client. Changed regions accumulate per client until it is ready
for more, so a slow link gets fewer, more up to date updates instead of an
ever growing queue of stale frames. Clients acknowledge each update they have
drawn; at most MAX_IN_FLIGHT updates are unacknowledged, which bounds the
latency end to end (WebSocket buffers, nginx, kernel socket buffers).

Encoding and capture run in worker threads so the event loop that injects
keyboard and mouse input is never blocked by a frame.
"""

import asyncio
import base64
import json
import time

import websockets

from webx11.capture import encode_update, union_rects
from webx11.input_dispatch import handle_input, handle_paste
from webx11.settings import SettingsManager

MAX_IN_FLIGHT = 2
ACK_TIMEOUT = 10.0
MAX_CLIPBOARD = 1024 * 1024
IDLE_FPS = 6
ACTIVE_SECONDS = 3.0
INPUT_REDRAW_DELAY = 0.004
BURST_SECONDS = 0.25
BURST_INTERVAL = 0.010


def _bbox(a, b):
    if a is None:
        return b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


class Client:
    def __init__(self, websocket, display):
        self.ws = websocket
        self.display = display
        self.pending = []
        self.keyframe = True
        self.lossless = False
        self.lossy = None  # bbox of regions last sent as JPEG
        self.wake = asyncio.Event()
        self.ack_event = asyncio.Event()
        self.seq = 0
        self.acked = 0
        self.closed = False
        self.wake.set()

    def add(self, rects, keyframe=False):
        if keyframe:
            self.keyframe = True
            self.pending = []
        else:
            self.pending = union_rects(self.pending + list(rects))
        self.wake.set()

    def request_lossless_refresh(self):
        if self.lossy is not None and not self.pending and not self.keyframe:
            self.pending = [self.lossy]
            self.lossless = True
            self.lossy = None
            self.wake.set()

    def take(self, width, height):
        if self.keyframe:
            rects, key = [(0, 0, width, height)], True
            self.lossy = None
        else:
            rects, key = self.pending, False
        lossless = self.lossless
        self.pending, self.keyframe, self.lossless = [], False, False
        return rects, key, lossless

    def on_ack(self, seq):
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return
        if seq > self.acked:
            self.acked = seq
            self.ack_event.set()

    @property
    def in_flight(self):
        return self.seq - self.acked


class DisplayStream:
    def __init__(self, handler, display):
        self.handler = handler
        self.display = display
        self.clients = []
        self.task = None
        self.cursor_message = None
        self.last_clipboard = None
        self.exited = False
        self.kick = asyncio.Event()
        self.last_activity = 0.0
        self.burst_until = 0.0

    def poke(self):
        """Input arrived: poll soon and at full rate for a while."""
        now = asyncio.get_running_loop().time()
        self.last_activity = now
        self.burst_until = now + BURST_SECONDS
        self.kick.set()

    async def broadcast_json(self, payload):
        message = json.dumps(payload)
        for c in list(self.clients):
            try:
                await c.ws.send(message)
            except Exception:
                pass

    async def run(self):
        settings = self.handler.settings
        interval = 1.0 / max(1, settings.fps)
        idle_interval = max(interval, 1.0 / IDLE_FPS)
        loop = asyncio.get_running_loop()
        idle_since = None
        last_slow_check = 0.0
        try:
            while self.clients and self.display.is_running:
                t0 = loop.time()
                try:
                    _, rects, keyframe = await asyncio.to_thread(self.display.poll_frame)
                except Exception as e:
                    print(f"Capture error on display {self.display.display_id}: {e}")
                    await asyncio.sleep(0.5)
                    continue

                if rects or keyframe:
                    idle_since = None
                    self.last_activity = t0
                    for c in self.clients:
                        c.add(rects, keyframe)
                else:
                    if idle_since is None:
                        idle_since = t0
                    elif settings.lossless_refresh_ms and \
                            (t0 - idle_since) * 1000 >= settings.lossless_refresh_ms:
                        for c in self.clients:
                            c.request_lossless_refresh()

                cursor = self.display.poll_cursor()
                if cursor:
                    png, xhot, yhot = cursor
                    self.cursor_message = {
                        'type': 'cursor', 'xhot': xhot, 'yhot': yhot,
                        'png': base64.b64encode(png).decode('ascii') if png else None,
                    }
                    await self.broadcast_json(self.cursor_message)

                clipboard_changed = self.display.poll_monitor_events()
                # size new / self-resizing app windows to the viewport
                self.display.enforce_layout()
                if clipboard_changed:
                    text = await asyncio.to_thread(self.display.read_clipboard)
                    if text is not None and text != self.last_clipboard and len(text) <= MAX_CLIPBOARD:
                        self.last_clipboard = text
                        await self.broadcast_json({'type': 'clipboard', 'text': text})

                if t0 - last_slow_check >= 1.0:
                    last_slow_check = t0
                    if not self.exited and self.display.check_app_exited():
                        self.exited = True
                        await self.broadcast_json({'type': 'app_exit'})

                now = loop.time()
                if now < self.burst_until:
                    period = BURST_INTERVAL  # right after input: catch the redraw quickly
                elif now - self.last_activity < ACTIVE_SECONDS:
                    period = interval
                else:
                    period = idle_interval
                delay = period - (now - t0)
                self.kick.clear()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self.kick.wait(), delay)
                        # input happened: give the app a moment to redraw
                        await asyncio.sleep(INPUT_REDRAW_DELAY)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0.002)
        except asyncio.CancelledError:
            pass
        finally:
            self.task = None

    async def sender(self, client):
        settings = self.handler.settings
        while not client.closed:
            await client.wake.wait()
            client.wake.clear()
            while client.in_flight >= MAX_IN_FLIGHT and not client.closed:
                client.ack_event.clear()
                try:
                    await asyncio.wait_for(client.ack_event.wait(), ACK_TIMEOUT)
                except asyncio.TimeoutError:
                    client.acked = client.seq  # acks lost: resync rather than stall
            if client.closed:
                break
            image = self.display.current_image
            if image is None:
                continue
            rects, keyframe, lossless = client.take(image.width, image.height)
            if not rects:
                continue
            client.seq += 1
            try:
                message, lossy = await asyncio.to_thread(
                    encode_update, image, rects, client.seq, keyframe,
                    settings.image_quality, settings.lossless_max_area, lossless)
            except Exception as e:
                print(f"Encode error: {e}")
                client.acked = client.seq
                continue
            for r in lossy:
                client.lossy = _bbox(client.lossy, r)
            try:
                await client.ws.send(message)
            except websockets.exceptions.ConnectionClosed:
                break


class WebSocketHandler:
    def __init__(self, window_display_manager):
        self.window_manager = window_display_manager
        self.settings = SettingsManager()
        self.streams = {}
        # kept for API compatibility
        self.connected_clients = []
        self.window_update_task = None

    def _stream_for(self, display):
        stream = self.streams.get(display.display_id)
        if stream is None or stream.display is not display:
            stream = DisplayStream(self, display)
            self.streams[display.display_id] = stream
        return stream

    async def handle_websocket(self, websocket, path="/"):
        path = websocket.request.path
        try:
            display_id = int(path.strip('/').split('/')[-1])
        except (ValueError, IndexError):
            print(f"Invalid WebSocket path: {path}")
            return

        display = self.window_manager.get_display(display_id)
        if not display:
            print(f"Window {display_id} not found for WebSocket connection")
            await websocket.close(code=4404, reason='display not found')
            return

        stream = self._stream_for(display)
        client = Client(websocket, display)
        stream.clients.append(client)
        self.connected_clients.append({"websocket": websocket, "display_id": display_id})
        if stream.task is None:
            stream.task = asyncio.create_task(stream.run())
        sender = asyncio.create_task(stream.sender(client))
        print(f"WebSocket client connected for display {display_id} ({len(stream.clients)} client(s))")

        try:
            await websocket.send(self.settings.dump_json())
            if stream.cursor_message:
                await websocket.send(json.dumps(stream.cursor_message))
            async for message in websocket:
                if isinstance(message, bytes):
                    continue
                await self.handle_client_message(client, stream, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            client.closed = True
            client.wake.set()
            client.ack_event.set()
            sender.cancel()
            if client in stream.clients:
                stream.clients.remove(client)
            self.connected_clients = [c for c in self.connected_clients if c["websocket"] is not websocket]
            if display.input_handler and display.is_running:
                display.input_handler.release_all()
            print(f"WebSocket client disconnected from display {display_id}")

    async def handle_client_message(self, client, stream, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON message: {e}")
            return
        t = data.get('type')
        display = client.display
        if t == 'ack':
            client.on_ack(data.get('seq'))
        elif t == 'refresh':
            client.add([], keyframe=True)
        elif t == 'paste':
            stream.last_clipboard = data.get('text')
            await handle_paste(display, data)
        elif t == 'clipboard_get':
            text = await asyncio.to_thread(display.read_clipboard)
            await client.ws.send(json.dumps({'type': 'clipboard', 'text': text or ''}))
        elif t == 'quality':
            # JPEG quality for large changed regions (read per-frame by the encoder)
            try:
                v = int(data.get('value'))
            except (TypeError, ValueError):
                v = None
            if v is not None:
                self.settings.image_quality = max(10, min(100, v))
                client.add([], keyframe=True)   # re-send at the new fidelity now
        else:
            handle_input(display, data)
        if t not in ('ack', 'mousemove'):
            stream.poke()
        elif t == 'mousemove':
            stream.last_activity = asyncio.get_running_loop().time()

    # --- API compatibility with server.py ---------------------------------
    def start_window_broadcast(self, interval=2.0):
        pass

    def stop_window_broadcast(self):
        for stream in self.streams.values():
            if stream.task:
                stream.task.cancel()


async def run_websocket_server(window_manager, host='127.0.0.1', port=8081):
    websocket_handler = WebSocketHandler(window_manager)
    websocket_server = await websockets.serve(
        websocket_handler.handle_websocket,
        host,
        port,
        max_size=8 * 1024 * 1024,
        compression=None,  # frames are already compressed images
    )
    return websocket_server, websocket_handler
