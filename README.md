# WebX11

Stream any GNU/Linux GUI application directly to a web browser with low(-enough) latency and automatic window resizing. 

No VNC, no special client software, no websockify - just a simple HTTP server that makes desktop applications accessible through modern web protocols.

## Overview

WebX11 creates virtual X11 displays and streams them to web browsers using WebTransport (with WebSocket fallback). 

It's designed to be simple, fast, and easy to deploy, and only have dependencies you probably already have.

You're a sysadmin and need to open a GUI application on a remote server in a whim?

Need to expose a GUI app for a lab?

WebX11 has got you!

## Requirements

**System Dependencies:**
- `Xvfb` - Virtual framebuffer X server
- `openssl` (optional) - For generating TLS certificates (if you use WebTransport)

## Installation

```bash
pip install git+https://github.com/lp1dev/WebX11.git
```

```bash
pip install aioquic # Optional, for an installation with webtransport support required
```

Optionally, create a `settings.json` file (see Configuration below)

Run the server:
```bash
python -m webx11.server (your_executable)
```

**Key Features:**
- 🚀 **Low latency streaming** via WebTransport (HTTP/3) or WebSocket fallback
- 📐 **Automatic window resizing** based on browser viewport
- 🎯 **Direct X11 integration** - no VNC or intermediary protocols
- 🔌 **Simple HTTP API** for viewing creating and managing displays
- 📊 **Built-in FPS counter** (press F3 to toggle)

> **Note** : Regarding the latency. Realistically, you're not going to do some heavy gaming on this. But I'm currently streaming my terminal on my 2k screen and the 12 FPS I have are more than enough for this usage, especially with the low input latency!
> Also, if you're able to do some optimizations and gain a few FPS, you're very welcome to make a PR \o/!

## How It Works

1. WebX11 spawns virtual X displays using Xvfb
2. Captures window content and encodes it as an image
3. Streams frames via WebTransport streams (or WebSocket)
4. Sends input events (mouse/keyboard) back to X11
5. Automatically resizes the X display to match browser window

## Configuration

Create a `settings.json` file in the project root:

```json
{
  "resize_mode": "resize-x11",
  "transport": "webtransport",
  "image_quality": 80,
  "dpi": 300,
  "max_width": 3440,
  "max_height": 1440,
  "max_fps": 30,
  "can_start_executables": false
}
```

> **Note**: Keep *max_width* and *max_height* values superior to the maximum size your windows will be resized to.
High values do not actually have a significative impact on performance with the *resize-x11* parameter.

### Configuration Options

| Option | Type | Description |
|--------|------|-------------|
| `resize_mode` | string | `"resize-x11"` enables automatic X display resizing, `"none"` disables it, `"stretch"` stretches the image without actual resizing |
| `transport` | string | `"webtransport"` (if your browser supports it) or `"websocket"` |
| `image_quality` | number | WebP quality (1-100), affects bandwidth and visual quality |
| `dpi` | number | Display DPI setting |
| `max_width` | number | Maximum display width in pixels |
| `max_height` | number | Maximum display height in pixels |
| `max_fps` | number | Maximum frames per second (1-60) |
| `can_start_executables` | boolean | Allow starting executables via API (security consideration) |
| `webtransport_host` | string | Webtransport hostname (for SSL/TLS exchanges) |
| `host` | string | Localhost by default |
|  `cors_unsafe_allow_all` | string | Allow unsafe origins (*) in CORS. Do not enable this one if you don't know what it is |
|  `image_format` | string | WEBP by default, but feel free to test with aything else that is supported by Pillow | 

## Usage

### Starting the Server

```bash
python -m webx11.server
```

The server will start on:
- HTTP API: `http://localhost:8080`
- WebSocket: `ws://localhost:8081`
- WebTransport: `https://localhost:4433`

### Creating a Display

**Option 1: Using the HTTP API**

```bash
curl -X POST http://localhost:8080/display \
  -H "Content-Type: application/json" \
  -d '{"width": 1920, "height": 1080}'
```

Response:
```json
{
  "message": "OK",
  "display": 1
}
```

**Option 2: Starting with an Application** (if `can_start_executables: true`)

```bash
curl -X POST http://localhost:8080/display/1/run \
  -H "Content-Type: application/json" \
  -d '{"executable": "firefox"}'
```

### Accessing the Display

Open your browser and navigate to:
```
http://localhost:8080
```

or 

```
http://localhost:8080/display/{id}
```

To access a specific display using its id.

### Browser Requirements

**For WebTransport (recommended):**
- Chrome/Edge with command-line flags (see below)
- The browser must accept the self-signed certificate

**Starting Chrome with WebTransport support:**
```bash
google-chrome \
  --origin-to-force-quic-on=localhost:4433 \
  --ignore-certificate-errors-spki-list=<CERTIFICATE_FINGERPRINT> \
  --test-type
```

The certificate fingerprint is printed when the server starts.

**For WebSocket fallback:**
- Any modern browser (Firefox, Safari, Chrome, Edge)
- No special configuration needed

## HTTP API Reference

### `GET /displays`
List all active displays

**Response:**
```json
[
  {
    "display_id": 1,
    "width": 1920,
    "height": 1080,
    "windows": [...]
  }
]
```

### `POST /display`
Create a new display

> **Note**: Make sure the display you create is **always** bigger than the max resize area possible. 
The X server will crash on resize otherwise.

Starting with a very large display size is not an issue, the first automatic resize will resize it into a smaller display. 

**Request:**
```json
{
  "width": 1920,
  "height": 1080
}
```

**Response:**
```json
{
  "message": "OK",
  "display": 1 #Internal display ID
}
```

### `DELETE /display/{id}`

Close a display

**Response:**
```json
{
    "success": true
}
```

### `POST /display/{id}/run`
Start an executable on a display (requires `can_start_executables: true`)

**Request:**
```json
{
  "executable": "firefox"
}
```

**Response:**
```json
{
  "message": "OK",
  "display": 1, #Internal display ID
  "process": 12345 #PID
}
```

### `POST /resize/{display_id}/{width}/{height}`
Manually resize a display

**Response:**
```json
{
  "success": true
}
```

### `GET /display/{id}`

Access the web interface for a display

Returns an HTML page with the interactive display viewer.

If the server is started with no executable as a parameter, a display needs to be created via the HTTP API.

## Keyboard Support

**Built-in shortcuts:**
- `F3` - Toggle FPS counter

### Frame Streaming

**WebTransport Mode:**
- Each frame is sent on a separate unidirectional stream
- Control messages (input) use datagrams
- 10-30 FPS depending on configuration
- Lower latency than WebSocket

**WebSocket Mode:**
- Frames sent as binary blobs
- 8~25 FPS depending on configuration
- Control messages as JSON
- Works everywhere, no special setup needed

## Performance Tips

1. **Lower `image_quality`** (60-80) for better performance on slow networks
2. **Reduce `max_fps`** (15-20) if CPU usage is high
3. **Use WebTransport** when possible for lowest latency
4. **Match `max_width/max_height`** to your typical use case
5. **Enable resize mode** to adapt to different screen sizes

## Security Considerations

This project is a small project, the code has not been designed with the utmost security in mind and as of today does not include authentication.
It should **NOT** be exposed to the internet, especially on sensitive infrastructures! 
The WebSockets and HTTP APIs use no encryption, keep that in mind.

- **TLS Certificates**: WebTransport uses self-signed certificates by default
- **can_start_executables**: Set to `false` in production to prevent arbitrary code execution
- **No authentication**: Consider adding authentication for production deployments
- **No encryption** : Bring your own, by putting a HTTPS/WSS reverse proxy in front of WebX11
- **Network exposure**: By default, only binds to localhost

## Troubleshooting

### WebTransport not connecting
- Ensure Chrome is started with the correct flags
- Check that port 4433 is not blocked
- Verify the certificate fingerprint matches the generated certificates

### Black screen or no frames
- Check that Xvfb is installed and working
- Verify that your executable is correctly running on the display
- Check console logs for errors
- Try reducing image quality

### Keyboard not working
- Click on the display area to ensure focus
- Check browser console for errors
- International keyboards should work automatically but some layouts are still a WIP

### High CPU usage
- Reduce `max_fps` in settings
- Lower `image_quality`
- Check if multiple displays are running

## Development

### Project Structure
```
webx11/
├── main.py              # Entry point
├── display.py           # X11 display management
├── webtransport.py      # WebTransport server
├── websocket.py         # WebSocket server
├── api.py               # HTTP API handlers
├── settings.py          # Configuration management
└── partials/
    └── display.html     # Client web interface
```

### Running Tests
```bash
python -m pytest tests/
```

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

[GNU GPLv3](./LICENSE.md)

## Acknowledgments

- Built with [aioquic](https://github.com/aiortc/aioquic) for WebTransport support
- Uses [python-xlib](https://github.com/python-xlib/python-xlib) for X11 integration
- Inspired by the need for simple, clientless desktop streaming

## Support

For issues, questions, or feature requests, please open an issue on GitHub.
