"""
Frame capture and incremental encoding.

Capture path
    Xvfb is started with ``-fbdir`` so its framebuffer lives in an mmap'ed
    XWD file. Reading it is a memcpy (~5 ms for 2560x1440) instead of an
    XGetImage round trip through python-xlib (~180 ms for the same size,
    which also blocked the asyncio loop that handles input).
    If the framebuffer file is not available we fall back to XGetImage on a
    dedicated X connection.

Encoding
    Each tick the new frame is compared with the previous one. Only the
    changed regions (dirty rectangles, computed per horizontal band) are
    encoded: small regions as PNG (crisp text), large ones as JPEG. When the
    screen goes idle, regions that were sent lossy are re-sent lossless.

Wire format (binary WebSocket message, little endian)
    header  : magic 'VX' | u8 version=2 | u8 flags | u16 width | u16 height
              | u16 rect_count | u32 seq                       (14 bytes)
    rect    : u16 x | u16 y | u16 w | u16 h | u8 fmt | u8 0 | u32 length
              followed by ``length`` bytes of image data         (14 bytes + data)
    flags   : bit0 = keyframe (client should clear before drawing)
    fmt     : 0 = JPEG, 1 = PNG
"""

import io
import mmap
import os
import struct
import zlib

from PIL import Image, ImageChops

XWD_HEADER = struct.Struct('>25I')
FRAME_HEADER = struct.Struct('<2sBBHHHI')
RECT_HEADER = struct.Struct('<HHHHBBI')
PROTOCOL_VERSION = 2
FMT_JPEG, FMT_PNG = 0, 1
FLAG_KEYFRAME = 1

BAND_HEIGHT = 32
MAX_RECTS = 24


class Snapshot:
    """One consistent copy of the visible region. The change checksums and the
    decoded image are both derived from the *same* bytes, so a pixel that
    changes mid-capture can never be stored as 'previous' without being sent
    (which was the cause of ghosted, never-refreshed regions)."""

    __slots__ = ('raw', 'width', 'height', 'stride', 'rawmode')

    def __init__(self, raw, width, height, stride, rawmode):
        self.raw = raw
        self.width = width
        self.height = height
        self.stride = stride
        self.rawmode = rawmode

    def band_sums(self, band=BAND_HEIGHT):
        step = self.stride * band
        total = self.stride * self.height
        raw = self.raw
        return [zlib.crc32(raw[i:i + step]) for i in range(0, total, step)]

    def image(self):
        return Image.frombuffer('RGB', (self.width, self.height), self.raw,
                                'raw', self.rawmode, self.stride, 1)


class FramebufferFile:
    """Read-only view on an Xvfb ``-fbdir`` framebuffer (XWD layout)."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        try:
            self.mm = mmap.mmap(self.fd, 0, prot=mmap.PROT_READ)
            h = XWD_HEADER.unpack_from(self.mm, 0)
        except Exception:
            os.close(self.fd)
            raise
        header_size, ncolors = h[0], h[19]
        self.width, self.height = h[4], h[5]
        self.bits_per_pixel, self.stride = h[11], h[12]
        if self.bits_per_pixel != 32:
            self.close()
            raise ValueError(f"unsupported framebuffer depth: {self.bits_per_pixel} bpp")
        self.offset = header_size + ncolors * 12
        # XWD byte_order 0 = LSBFirst: a 0x00RRGGBB pixel is stored B,G,R,X
        self.rawmode = 'BGRX' if h[7] == 0 else 'XRGB'

    def band_sums(self, height, band=None, width=None):
        """crc32 of each band, computed in place (no copy). Cheap change probe."""
        band = band or BAND_HEIGHT
        height = min(height, self.height)
        view = memoryview(self.mm)
        step = self.stride * band
        start, end = self.offset, self.offset + self.stride * height
        try:
            return [zlib.crc32(view[i:min(i + step, end)]) for i in range(start, end, step)]
        finally:
            view.release()

    def snapshot(self, width, height):
        """One atomic copy of the visible region (sums + image come from it)."""
        width, height = min(width, self.width), min(height, self.height)
        raw = self.mm[self.offset:self.offset + self.stride * height]  # single memcpy
        return Snapshot(raw, width, height, self.stride, self.rawmode)

    def grab(self, width, height):
        """Return (raw_bytes, PIL image) for the top-left width x height area."""
        width, height = min(width, self.width), min(height, self.height)

        def make():
            # RGB is not a shared-memory mode, so PIL decodes (copies) the
            # pixels out of the mapping and the view can be released.
            view = memoryview(self.mm)[self.offset:self.offset + self.stride * height]
            try:
                return Image.frombuffer('RGB', (width, height), view, 'raw', self.rawmode, self.stride, 1)
            finally:
                view.release()
        return None, make

    def close(self):
        try:
            self.mm.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass


class XGetImageSource:
    """Fallback capture through XGetImage on its own X connection."""

    def __init__(self, display_name):
        import Xlib.display
        from Xlib import X
        self._X = X
        self.display = Xlib.display.Display(display_name)
        screen = self.display.screen()
        self.root = screen.root
        self.width, self.height = screen.width_in_pixels, screen.height_in_pixels

    def _get(self, width, height):
        width, height = min(width, self.width), min(height, self.height)
        raw = self.root.get_image(0, 0, width, height, self._X.ZPixmap, 0xffffffff).data
        self._cached = (width, height, raw)
        return raw

    def band_sums(self, height, band=None, width=None):
        band = band or BAND_HEIGHT
        raw = self._get(width or self.width, height)
        w = min(width or self.width, self.width)
        step = w * 4 * band
        return [zlib.crc32(raw[i:i + step]) for i in range(0, len(raw), step)]

    def snapshot(self, width, height):
        width, height = min(width, self.width), min(height, self.height)
        raw = self.root.get_image(0, 0, width, height, self._X.ZPixmap, 0xffffffff).data
        return Snapshot(raw, width, height, width * 4, 'BGRX')

    def grab(self, width, height):
        cached = getattr(self, '_cached', None)
        self._cached = None
        if cached and cached[0] == min(width, self.width) and cached[1] == min(height, self.height):
            width, height, raw = cached
        else:
            raw = self._get(width, height)
            width, height = min(width, self.width), min(height, self.height)
        return raw, lambda: Image.frombytes('RGB', (width, height), raw, 'raw', 'BGRX')

    def close(self):
        try:
            self.display.close()
        except Exception:
            pass


def _merge(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _area(r):
    return (r[2] - r[0]) * (r[3] - r[1])


def dirty_rects(prev, cur, bands=None):
    """List of (l, t, r, b) boxes where ``cur`` differs from ``prev``.

    ``bands`` optionally restricts the search to these band indexes (rows
    [i*BAND_HEIGHT, (i+1)*BAND_HEIGHT)), as found by comparing checksums.
    """
    width, height = cur.size
    if bands is None:
        bands = range((height + BAND_HEIGHT - 1) // BAND_HEIGHT)
    rects = []
    for i in bands:
        y, y2 = i * BAND_HEIGHT, min((i + 1) * BAND_HEIGHT, height)
        if y >= height:
            continue
        box = (0, y, width, y2)
        band = ImageChops.difference(prev.crop(box), cur.crop(box)).getbbox()
        if not band:
            continue
        r = (band[0], band[1] + y, band[2], band[3] + y)
        if rects:
            last = rects[-1]
            touching = r[1] - last[3] <= 2
            overlap_x = r[0] <= last[2] + 16 and r[2] >= last[0] - 16
            if touching and overlap_x:
                rects[-1] = _merge(last, r)
                continue
        rects.append(r)
    if not rects:
        return []
    bbox = rects[0]
    for r in rects[1:]:
        bbox = _merge(bbox, r)
    if len(rects) > MAX_RECTS or sum(_area(r) for r in rects) > 0.7 * _area(bbox):
        return [bbox]
    return rects


def union_rects(rects):
    """Coalesce a pending rect list so it never grows without bound."""
    if len(rects) <= MAX_RECTS:
        return rects
    box = rects[0]
    for r in rects[1:]:
        box = _merge(box, r)
    return [box]


def clip_rects(rects, width, height):
    out = []
    for l, t, r, b in rects:
        l, t = max(0, l), max(0, t)
        r, b = min(width, r), min(height, b)
        if r > l and b > t:
            out.append((l, t, r, b))
    return out


def encode_update(image, rects, seq, keyframe=False, quality=80,
                  lossless_max_area=160_000, force_lossless=False):
    """Encode ``rects`` of ``image`` into one binary update message.

    Returns (message_bytes, lossy_rects) where lossy_rects are the regions
    that were sent as JPEG (candidates for a later lossless refresh).
    """
    width, height = image.size
    rects = clip_rects(rects, width, height)
    parts = []
    lossy = []
    for rect in rects:
        l, t, r, b = rect
        region = image.crop(rect)
        buf = io.BytesIO()
        if force_lossless or _area(rect) <= lossless_max_area:
            region.save(buf, format='PNG', compress_level=1)
            fmt = FMT_PNG
        else:
            region.save(buf, format='JPEG', quality=quality, optimize=False, subsampling=2)
            fmt = FMT_JPEG
            lossy.append(rect)
        data = buf.getvalue()
        parts.append(RECT_HEADER.pack(l, t, r - l, b - t, fmt, 0, len(data)))
        parts.append(data)
    header = FRAME_HEADER.pack(b'VX', PROTOCOL_VERSION, FLAG_KEYFRAME if keyframe else 0,
                               width, height, len(rects), seq & 0xffffffff)
    return header + b''.join(parts), lossy


def encode_full_frame(image, image_format='JPEG', quality=80):
    """Single full image (legacy full-frame path)."""
    buf = io.BytesIO()
    if (image_format or 'JPEG').upper() == 'PNG':
        image.save(buf, format='PNG', compress_level=1)
    else:
        image.save(buf, format='JPEG', quality=quality, optimize=False, subsampling=2)
    return buf.getvalue()


def cursor_to_png(cursor_reply):
    """Convert an XFixes GetCursorImage reply (premultiplied ARGB) to PNG bytes."""
    import array
    w, h = cursor_reply.width, cursor_reply.height
    if w == 0 or h == 0:
        return None
    pixels = array.array('I', cursor_reply.cursor_image[:w * h])
    if pixels.itemsize != 4:
        pixels = array.array('L', cursor_reply.cursor_image[:w * h])
    raw = pixels.tobytes()
    if pixels.itemsize != 4:
        return None
    # little-endian ARGB words are B,G,R,A in memory, alpha-premultiplied
    img = Image.frombuffer('RGBa', (w, h), raw, 'raw', 'BGRa', 0, 1).convert('RGBA')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()
