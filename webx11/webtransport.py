import subprocess
import asyncio
import hashlib
import base64
import json
import time
import os
from datetime import datetime
from webx11.settings import SettingsManager

IMAGES_SENT = 0
LAST_FRAME = datetime.now()

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
    def __init__(self, session_id, http, window_manager, display_id, protocol):
        self.session_id = session_id
        print('Session Id', session_id)
        self.http = http
        self.protocol = protocol 
        self.window_manager = window_manager
        self.display_id = display_id
        self.running = True
        self.frame_counter = 0
        self.settings = SettingsManager('settings.json')
        
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
        print('got message', data)
        """Handle incoming control messages"""
        msg_type = data.get('type')
        window_display = self.window_manager.get_display(self.display_id)
        
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
                    await asyncio.sleep(0.1)
                    await self.send_window_update()
    
    async def handle_mouse_event(self, data, pressed):
        x, y = data.get('x'), data.get('y')
        button = data.get('button', 1)
        
        if x is not None and y is not None:
            window_display = self.window_manager.get_display(self.display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_mouse_event(x, y, button, pressed)
    
    async def handle_mouse_move(self, data):
        x, y = data.get('x'), data.get('y')
        if x is not None and y is not None:
            window_display = self.window_manager.get_display(self.display_id)
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
            window_display = self.window_manager.get_display(self.display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_scroll_event(x, y, delta_y)
    
    async def handle_key_event(self, data, pressed):
        key = data.get('key')
        if key:
            window_display = self.window_manager.get_display(self.display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_key_event_by_name(key, pressed)
    
    async def handle_text_input(self, data):
        text = data.get('text', '')
        if text:
            window_display = self.window_manager.get_display(self.display_id)
            if window_display and window_display.input_handler:
                success = window_display.input_handler.send_text_input(text)
    
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
            await asyncio.sleep(round(1.0 / self.settings.fps, 2))

    async def send_window_update(self, force=False):
        """Send window image via WebTransport stream"""
        global IMAGES_SENT, LAST_FRAME
        try:
            delta = datetime.now() - LAST_FRAME
            framerate_delta = 1000000 / self.settings.fps  # microseconds
            if delta.seconds == 0 and delta.microseconds < framerate_delta:
                return
            LAST_FRAME = datetime.now()
        
            window_display = self.window_manager.get_display(self.display_id)
            if window_display:
                window_image = window_display.capture_window(compressed=False, force=force)
                if window_image:
                    IMAGES_SENT += 1
                    self.frame_counter = (self.frame_counter + 1) % 65536

                    print(f"[Send image #{IMAGES_SENT} via stream (frame {self.frame_counter}) for window {self.display_id}, size: {len(window_image)} bytes]")

                    # Create a new unidirectional stream for this frame
                    stream_id = self.http.create_webtransport_stream(
                        session_id=self.session_id, is_unidirectional=True
                    )
                    
                    # Create header with frame metadata
                    header = (
                        self.frame_counter.to_bytes(2, 'big') +
                        len(window_image).to_bytes(4, 'big') +
                        int(time.time() * 1000).to_bytes(8, 'big')
                    )
                    
                    # Send header + complete frame data on the stream
                    self.protocol._quic.send_stream_data(
                        stream_id=stream_id,
                        data=header + window_image,
                        end_stream=True
                    )
                    
                    # Transmit the data
                    self.protocol.transmit()
                
                    # Yield to event loop
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
            display_id = int(path.strip('/').split('/')[-1])
            print(f"WebTransport session requested for window {display_id}")
        except (ValueError, IndexError):
            print(f"Invalid WebTransport path: {path}")
            self._send_response(stream_id, 404, end_stream=True)
            return
        
        if not self.window_manager.get_display(display_id):
            print(f"Window {display_id} not found")
            self._send_response(stream_id, 404, end_stream=True)
            return
        
        self._handler = WebTransportHandler(
            stream_id, self._http, self.window_manager, display_id, self
        )
        self._send_response(stream_id, 200)
        
        self._update_task = asyncio.create_task(self._handler.send_updates_loop())
        print(f"WebTransport session established for window {display_id}")
    
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

async def run_webtransport_server(window_manager, host, port):
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
                    "-subj", f"/CN={host}", "-addext", "subjectAltName = DNS:localhost" # Watch out, here the CN must be localhost if you're running in local! I changed it to {host}
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
                host,
                "%s" % port,
                configuration=configuration,
                create_protocol=create_protocol,
                retry=True,                    
            )
            print(f"✅ WebTransport server started on {host} {port}")
            
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