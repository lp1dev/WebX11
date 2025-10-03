import websockets
import subprocess
import asyncio
import hashlib
import base64
import json
import time
import os
from datetime import datetime

IMAGES_SENT = 0
FPS = 30
LAST_FRAME = datetime.now()

# Datagram settings
MAX_DATAGRAM_SIZE = 1200  # Safe size for most networks
CHUNK_HEADER_SIZE = 16  # bytes for metadata (frame_id, chunk_index, total_chunks, etc.)

try:
    from aioquic.asyncio import QuicConnectionProtocol, serve
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import (
        H3Event,
        HeadersReceived,
        WebTransportStreamDataReceived,
        DatagramReceived,
    )
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent
    WEBTRANSPORT_AVAILABLE = True
except ImportError:
    WEBTRANSPORT_AVAILABLE = False
    print("Warning: aioquic not installed. WebTransport will not be available.")

def sha256(data: bytes) -> str:
    hash_bytes = hashlib.sha256(data).digest()
    return base64.b64encode(hash_bytes).decode('utf-8')

class WebTransportHandler:
    """Handler for a single WebTransport session"""
    def __init__(self, session_id, http, window_manager, window_id, protocol):
        self.session_id = session_id
        print('Session Id', session_id)
        self.http = http
        self.protocol = protocol 
        self.window_manager = window_manager
        self.window_id = window_id
        self.running = True
        self.frame_counter = 0
        
    def h3_event_received(self, event: H3Event):
        """Handle H3 events for this session"""
        if isinstance(event, DatagramReceived):
            asyncio.create_task(self.handle_datagram(event.data))
        elif isinstance(event, WebTransportStreamDataReceived):
            asyncio.create_task(self.handle_stream_data(event))
    
    async def handle_datagram(self, data: bytes):
        """Handle incoming datagram (control messages)"""
        try:
            message = json.loads(data.decode('utf-8'))
            await self.handle_client_message(message)
        except Exception as e:
            print(f"Error handling datagram: {e}")
    
    async def handle_stream_data(self, event):
        """Handle incoming stream data (control messages)"""
        try:
            message = json.loads(event.data.decode('utf-8'))
            await self.handle_client_message(message)
        except Exception as e:
            print(f"Error handling stream data: {e}")
    
    async def handle_client_message(self, data):
        """Handle incoming control messages"""
        msg_type = data.get('type')
        window_display = self.window_manager.get_window_display(self.window_id)
        
        if not window_display or not window_display.input_handler:
            return
        
        if msg_type == 'mousedown':
            await self.handle_mouse_event(data, True)
        elif msg_type == 'mouseup':
            await self.handle_mouse_event(data, False)
        elif msg_type == 'mousemove':
            await self.handle_mouse_move(data)
        elif msg_type == 'scroll':
            await self.handle_scroll_event(data)
        elif msg_type == 'keydown':
            await self.handle_key_event(data, True)
        elif msg_type == 'keyup':
            await self.handle_key_event(data, False)
        elif msg_type == 'text_input':
            await self.handle_text_input(data)
        elif msg_type == 'refresh':
            await self.send_window_update(force=True)
        elif msg_type == 'resize':
            if data.get('height') and data.get('width'):
                if window_display.height != data.get('height') or data.get('width') != window_display.width:
                    window_display.force_resize(data.get('height'), data.get('width'))
    
    async def handle_mouse_event(self, data, pressed):
        x, y = data.get('x'), data.get('y')
        button = data.get('button', 1)
        
        if x is not None and y is not None:
            window_display = self.window_manager.get_window_display(self.window_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_mouse_event(x, y, button, pressed)
                self.send_control_message({
                    'type': 'input_result',
                    'input_type': 'mousedown' if pressed else 'mouseup',
                    'success': success,
                    'x': x, 'y': y, 'button': button
                })
                if success:
                    await asyncio.sleep(0.1)
                    await self.send_window_update()
    
    async def handle_mouse_move(self, data):
        x, y = data.get('x'), data.get('y')
        if x is not None and y is not None:
            window_display = self.window_manager.get_window_display(self.window_id)
            if window_display and window_display.input_handler:
                try:
                    window_display.input_handler.root.warp_pointer(
                        x + window_display.x, y + window_display.y)
                    window_display.input_handler.display.sync()
                except Exception as e:
                    print(f"Mouse move error: {e}")
    
    async def handle_scroll_event(self, data):
        x, y = data.get('x'), data.get('y')
        delta_y = data.get('deltaY', 0)
        if x is not None and y is not None and delta_y != 0:
            window_display = self.window_manager.get_window_display(self.window_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_scroll_event(x, y, delta_y)
                self.send_control_message({
                    'type': 'input_result',
                    'input_type': 'scroll',
                    'success': success,
                    'x': x, 'y': y, 'deltaY': delta_y
                })
                if success:
                    await asyncio.sleep(0.1)
                    await self.send_window_update()
    
    async def handle_key_event(self, data, pressed):
        key = data.get('key')
        if key:
            window_display = self.window_manager.get_window_display(self.window_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_key_event_by_name(key, pressed)
                self.send_control_message({
                    'type': 'input_result',
                    'input_type': 'keydown' if pressed else 'keyup',
                    'success': success,
                    'key': key
                })
                if success and not pressed:
                    await asyncio.sleep(0.1)
                    await self.send_window_update()
    
    async def handle_text_input(self, data):
        text = data.get('text', '')
        if text:
            window_display = self.window_manager.get_window_display(self.window_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_text_input(text)
                self.send_control_message({
                    'type': 'input_result',
                    'input_type': 'text',
                    'success': success,
                    'text': text
                })
                if success:
                    await asyncio.sleep(0.1)
                    await self.send_window_update()
    
    def send_control_message(self, message):
        """Send control message via datagram"""
        try:
            data = json.dumps(message).encode('utf-8')
            self.http.send_datagram(self.session_id, data)
        except Exception as e:
            print(f"Error sending control message: {e}")
    
    async def send_updates_loop(self):
        """Continuously send window updates"""
        while self.running:
            await self.send_window_update()
            await asyncio.sleep(1.0 / (FPS * 2)) # 60 fps atm

    async def send_window_update(self, force=False):
        """Send window image via datagrams (chunked)"""
        global IMAGES_SENT, LAST_FRAME
        try:
            delta = datetime.now() - LAST_FRAME
            framerate_delta = 1000000 / FPS  # microseconds
            if delta.seconds == 0 and delta.microseconds < framerate_delta:
                return
            LAST_FRAME = datetime.now()
        
            window_display = self.window_manager.get_window_display(self.window_id)
            if window_display:
                window_image = window_display.capture_window(force=force)
                if window_image:
                    IMAGES_SENT += 1
                    self.frame_counter = (self.frame_counter + 1) % 65536

                    print(f"[Send image #{IMAGES_SENT} via datagrams (frame {self.frame_counter}) for window {self.window_id}, size: {len(window_image)} bytes]")
                
                    # Calculate chunk size (leaving room for header)
                    chunk_payload_size = MAX_DATAGRAM_SIZE - CHUNK_HEADER_SIZE - 30
                    total_chunks = (len(window_image) + chunk_payload_size - 1) // chunk_payload_size

                    # Send chunks
                    for chunk_index in range(total_chunks):
                        start = chunk_index * chunk_payload_size
                        end = min(start + chunk_payload_size, len(window_image))
                        chunk_data = window_image[start:end]
                    
                        # Create header
                        header = (
                            self.frame_counter.to_bytes(2, 'big') +
                            chunk_index.to_bytes(2, 'big') +
                            total_chunks.to_bytes(2, 'big') +
                            len(chunk_data).to_bytes(4, 'big') +
                            int(time.time() * 1000).to_bytes(8, 'big')
                        )

                        # Send datagram with header + chunk
                        self.http.send_datagram(self.session_id, header + chunk_data)
                        self.protocol.transmit()
                
                
                    # Yield to event loop to allow transmission
                    await asyncio.sleep(0)
                
        except Exception as e:
            print(f"Error sending window update: {e}")
            import traceback
            traceback.print_exc()
    
    def stop(self):
        """Stop the handler"""
        self.running = False

class WebTransportProtocol(QuicConnectionProtocol):
    """WebTransport protocol handler"""
    def __init__(self, *args, window_display_manager, **kwargs):
        super().__init__(*args, **kwargs)
        self.window_manager = window_display_manager
        self._http = None
        self._handler = None
        self._update_task = None
        
    def quic_event_received(self, event: QuicEvent):
        """Handle QUIC events"""
        if isinstance(event, ProtocolNegotiated):
            self._http = H3Connection(self._quic, enable_webtransport=True)
        elif isinstance(event, StreamReset) and self._handler is not None:
            self._handler.stop()
        
        if self._http is not None:
            output = self._http.handle_event(event)
            for h3_event in output:
                self._h3_event_received(h3_event)
    
    def _h3_event_received(self, event: H3Event):
        """Handle H3 events"""
        if isinstance(event, HeadersReceived):
            headers = dict(event.headers)
            if (headers.get(b":method") == b"CONNECT" and
                    headers.get(b":protocol") == b"webtransport"):
                self._handshake_webtransport(event.stream_id, headers)
            else:
                self._send_response(event.stream_id, 400, end_stream=True)
        
        if self._handler:
            self._handler.h3_event_received(event)
    
    def _handshake_webtransport(self, stream_id: int, request_headers: dict):
        """Handle WebTransport handshake"""
        path = request_headers.get(b":path", b"").decode('utf-8')

        
        try:
            window_id = int(path.strip('/').split('/')[-1])
            print(f"WebTransport session requested for window {window_id}")
        except (ValueError, IndexError):
            print(f"Invalid WebTransport path: {path}")
            self._send_response(stream_id, 404, end_stream=True)
            return
        
        if not self.window_manager.get_window_display(window_id):
            print(f"Window {window_id} not found")
            self._send_response(stream_id, 404, end_stream=True)
            return
        
        self._handler = WebTransportHandler(
            stream_id, self._http, self.window_manager, window_id, self
        )
        self._send_response(stream_id, 200)
        
        self._update_task = asyncio.create_task(self._handler.send_updates_loop())
        print(f"WebTransport session established for window {window_id}")
    
    def _send_response(self, stream_id: int, status_code: int, end_stream=False):
        """Send HTTP response"""
        headers = [(b":status", str(status_code).encode())]
        if status_code == 200:
            headers.append((b"sec-webtransport-http3-draft", b"draft02"))
        self._http.send_headers(
            stream_id=stream_id, headers=headers, end_stream=end_stream)

    async def wait_closed(self) -> None:
        """
        Wait for the connection to be closed.
        """
        print('Connection is closing')
        await super._closed.wait()

async def run_webtransport_server(window_manager, port):
    webtransport_server = None
    if WEBTRANSPORT_AVAILABLE:
        try:
            configuration = QuicConfiguration(
                alpn_protocols=H3_ALPN,
                is_client=False,
                max_datagram_frame_size=65536,
            )
            
            if not os.path.exists("certs/cert.pem") or not os.path.exists("certs/key.pem"):
                print("Generating self-signed certificate for WebTransport...")
                subprocess.run([
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-out", "certs/cert.pem", "-keyout", "key.pem", "-days", "365",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName = DNS:localhost"
                ], check=True, capture_output=True)
                
                subprocess.run([
                    "openssl", "x509", "-pubkey", "-out", "certs/pubkey.pem", "-in", "certs/cert.pem"
                ], capture_output=True, text=True, check=True)

                subprocess.run([
                    "openssl", "rsa", "-pubin", "-in", "certs/pubkey.pem", "-outform", "der", "-out", "certs/pubkey.der"
                ], check=True, capture_output=True)
                
            configuration.load_cert_chain("certs/cert.pem", "certs/key.pem")
            
            def create_protocol(*args, **kwargs):
                return WebTransportProtocol(
                    *args,
                    window_display_manager=window_manager,
                    **kwargs
                )
            
            webtransport_server = await serve(
                "localhost",
                "%s" % port,
                configuration=configuration,
                create_protocol=create_protocol,
                retry=True,                    
            )
            print(f"✅ WebTransport server started on port {port}")
            
            fingerprint = "None"
            with open("certs/pubkey.der", "rb") as f:
                fingerprint = sha256(f.read())
            
            print(f"\n🔐 Certificate fingerprint: {fingerprint}")
            print(f"   To use with Chrome, start with:")
            print(f"$CHROME_BINARY --origin-to-force-quic-on=localhost:{port} --ignore-certificate-errors-spki-list={fingerprint} --test-type\n")
            
        except Exception as e:
            print(f"⚠️  Failed to start WebTransport server: {e}")
            print("Continuing with WebSocket only...")
            import traceback
            traceback.print_exc()
    else:
        print("⚠️  WebTransport not available (install aioquic)")
    
    return webtransport_server