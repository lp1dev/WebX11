import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import Xlib
import Xlib.display
import Xlib.threaded  # noqa: F401
from Xlib import X

from webx11.capture import FramebufferFile, XGetImageSource, dirty_rects, cursor_to_png
from webx11.settings import SettingsManager
from webx11.window import WindowScreenCapture, WindowInputHandler

MIN_WIDTH, MIN_HEIGHT = 200, 150


def _fbdir_root(needed_bytes, configured=None):
    """Pick a directory for Xvfb framebuffers: tmpfs (/dev/shm) when it has room."""
    candidates = [configured] if configured else []
    candidates += ['/dev/shm', tempfile.gettempdir()]
    for c in candidates:
        try:
            if c and os.path.isdir(c) and os.access(c, os.W_OK):
                if shutil.disk_usage(c).free > needed_bytes * 1.5:
                    return c
        except OSError:
            continue
    return tempfile.gettempdir()


class SingleWindowDisplay:
    def __init__(self, display_num, display_id, width=1920, height=1080, depth=24):
        self.settings = SettingsManager()
        width = max(MIN_WIDTH, min(int(width), self.settings.max_width))
        height = max(MIN_HEIGHT, min(int(height), self.settings.max_height))
        self.display_num = display_num
        self.display_name = f":{display_num}"
        self.display_id = display_id
        # screen_* is the Xvfb framebuffer size, width/height the visible area
        self.screen_width, self.screen_height = width, height
        self.width = width
        self.height = height
        self.x = 0
        self.y = 0
        self.depth = depth
        self.xvfb_process = None
        self.x11_display = None
        self.monitor_display = None
        self.screen_capture = None
        self.input_handler = None
        self.frame_source = None
        self.fbdir = None
        self.is_running = False
        self.has_updated = False
        self.last_frame = None
        self.maxwidth = width
        self.maxheight = height
        self.still_frames = 0
        self.executable = None
        self.process = None
        # incremental capture state (owned by the capture thread)
        self._prev_sums = None
        self._prev_image = None
        self.current_image = None
        self.frame_serial = 0
        self._capture_lock = threading.Lock()
        # monitoring
        self._cursor_serial = None
        self._seen_alive = False
        self._dead_checks = 0
        self.has_xfixes = False
        # layout enforcement: keep app windows matching the target size as they
        # appear or resize themselves (slow-starting apps map their window long
        # after launch, so a one-shot resize is not enough)
        self._layout_dirty = False
        self._target_dirty = False
        self._enforce_until = 0.0
        self._last_enforce = 0.0

    # ------------------------------------------------------------ lifecycle
    def start(self):
        try:
            fb_bytes = self.screen_width * self.screen_height * 4
            self.fbdir = tempfile.mkdtemp(prefix=f'webx11-{self.display_num}-',
                                          dir=_fbdir_root(fb_bytes, self.settings.fbdir))
            cmd = [
                'Xvfb', self.display_name,
                '-screen', '0', f'{self.screen_width}x{self.screen_height}x{self.depth}',
                '-fbdir', self.fbdir,
                '-ac', '-nolisten', 'tcp', '-dpi', str(self.settings.x_dpi),
            ]
            print('Starting', " ".join(cmd))
            self.xvfb_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            deadline = time.time() + 10
            while True:
                if self.xvfb_process.poll() is not None:
                    raise Exception(f"Xvfb exited (is {self.display_name} already in use?)")
                if os.path.exists(os.path.join(self.fbdir, 'Xvfb_screen0')) or time.time() > deadline - 8:
                    try:
                        Xlib.display.Display(self.display_name).close()
                        break
                    except Exception:
                        pass
                if time.time() > deadline:
                    raise Exception("Xvfb failed to start")
                time.sleep(0.05)
            if self.xvfb_process.poll() is not None:
                raise Exception(f"Xvfb exited (is {self.display_name} already in use?)")

            self.x11_display = Xlib.display.Display(self.display_name)
            self.input_handler = WindowInputHandler(self)
            self.screen_capture = WindowScreenCapture(self)
            self._open_frame_source()
            self._open_monitor()

            self.is_running = True
            print(f"Window display started on {self.display_name} (ID: {self.display_id})")
            return True

        except Exception as e:
            print(f"Failed to start window display: {e}")
            self.stop()
            return False

    def _open_frame_source(self):
        path = os.path.join(self.fbdir, 'Xvfb_screen0')
        for _ in range(40):
            if os.path.exists(path):
                try:
                    self.frame_source = FramebufferFile(path)
                    print(f"Display {self.display_id}: capturing from mmap'ed framebuffer {path}")
                    return
                except Exception as e:
                    print(f"Display {self.display_id}: framebuffer file unusable ({e})")
                    break
            time.sleep(0.05)
        print(f"Display {self.display_id}: falling back to XGetImage capture (slower)")
        self.frame_source = XGetImageSource(self.display_name)

    def _open_monitor(self):
        """Separate connection for cursor, clipboard and liveness checks.
        It also hides the X cursor so it is not baked into the framebuffer:
        the browser draws the real cursor shape instead."""
        self.monitor_display = Xlib.display.Display(self.display_name)
        root = self.monitor_display.screen().root
        # Watch for windows being created / mapped / resized so we can size late
        # or self-resizing app windows to the viewport (SubstructureNotify can be
        # selected by several clients, unlike SubstructureRedirect).
        try:
            root.change_attributes(event_mask=X.SubstructureNotifyMask)
            self.monitor_display.sync()
        except Exception as e:
            print(f"SubstructureNotify unavailable on {self.display_name}: {e}")
        try:
            if self.monitor_display.has_extension('XFIXES'):
                self.monitor_display.xfixes_query_version()
                root.xfixes_hide_cursor()
                clipboard = self.monitor_display.intern_atom('CLIPBOARD')
                self.monitor_display.xfixes_select_selection_input(root, clipboard, 1)  # SetSelectionOwnerNotifyMask
                self.monitor_display.sync()
                self.has_xfixes = True
        except Exception as e:
            print(f"XFIXES unavailable on {self.display_name}: {e}")

    def stop(self):
        self.is_running = False
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except Exception:
                pass
        for conn in (self.monitor_display, self.x11_display):
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        if self.frame_source:
            self.frame_source.close()
        if self.xvfb_process:
            self.xvfb_process.terminate()
            try:
                self.xvfb_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.xvfb_process.kill()
        if self.fbdir:
            shutil.rmtree(self.fbdir, ignore_errors=True)
        print(f"Window display stopped (ID: {self.display_id})")

    def get_display(self):
        return self.x11_display

    # -------------------------------------------------------------- capture
    def grab_image(self):
        if not self.frame_source:
            return None
        _, make = self.frame_source.grab(self.width, self.height)
        return make()

    def poll_frame(self):
        """Check the screen for changes and grab it when something changed.

        Runs in a worker thread. Per-band checksums are computed directly on
        the framebuffer, so an idle screen costs no copy and no image work.
        Returns (image, rects, keyframe); rects is [] when nothing changed.
        """
        with self._capture_lock:
            width, height = self.width, self.height
            fw = min(width, self.frame_source.width)
            fh = min(height, self.frame_source.height)
            prev = self._prev_image
            size_changed = prev is None or prev.size != (fw, fh)

            # Cheap probe: if nothing changed, skip the copy entirely.
            probe = self.frame_source.band_sums(height, width=width)
            if not size_changed and probe == self._prev_sums:
                return prev, [], False

            # Something changed: take ONE consistent snapshot and derive both the
            # per-band checksums and the image from it, so the dirty regions and
            # the pixels we store as 'previous' always agree.
            snap = self.frame_source.snapshot(width, height)
            sums = snap.band_sums()
            image = snap.image()
            if size_changed or not self._prev_sums or len(sums) != len(self._prev_sums):
                bands = None
            else:
                bands = [i for i, (a, b) in enumerate(zip(sums, self._prev_sums)) if a != b]
            self._prev_sums = sums
            self._prev_image = image
            self.current_image = image
            self.frame_serial += 1
            if size_changed:
                return image, [(0, 0, image.width, image.height)], True
            return image, dirty_rects(prev, image, bands), False

    def capture_window(self, compressed=False, force=False):
        """Legacy full-frame capture (kept for API compatibility)."""
        if self.screen_capture:
            return self.screen_capture.capture_window(force=force)
        return None

    # ----------------------------------------------------------- monitoring
    def poll_cursor(self):
        """Return (png_bytes, xhot, yhot) when the cursor shape changed, else None."""
        if not self.has_xfixes:
            return None
        try:
            reply = self.monitor_display.xfixes_get_cursor_image(self.monitor_display.screen().root)
        except Exception:
            return None
        if reply.cursor_serial == self._cursor_serial:
            return None
        self._cursor_serial = reply.cursor_serial
        try:
            png = cursor_to_png(reply)
        except Exception as e:
            print(f"Cursor conversion failed: {e}")
            return None
        return png, reply.xhot, reply.yhot

    _STRUCTURE_EVENTS = {
        'MapNotify', 'MapNotifyEvent', 'ConfigureNotify', 'ConfigureNotifyEvent',
        'CreateNotify', 'CreateNotifyEvent', 'UnmapNotify', 'UnmapNotifyEvent',
    }

    def poll_monitor_events(self):
        """Drain the monitor connection's event queue once.

        Returns True when the X clipboard owner changed. Also flags a layout
        pass when any window is created, mapped, unmapped or resized, so the
        capture loop can re-apply the target size to new app windows.
        """
        clipboard_changed = False
        try:
            while self.monitor_display.pending_events():
                name = self.monitor_display.next_event().__class__.__name__
                if name == 'SetSelectionOwnerNotify':
                    clipboard_changed = True
                elif name in self._STRUCTURE_EVENTS:
                    self._layout_dirty = True
        except Exception:
            pass
        return clipboard_changed

    # kept for API compatibility
    def poll_clipboard_changed(self):
        return self.poll_monitor_events()

    def apply_target_size(self):
        """Configure every real app window to the current target size.

        Runs on the capture thread through the monitor connection. Menus,
        tooltips and combo-box popups are override-redirect and are left
        untouched, so this never disturbs an open menu.
        """
        w, h = self.width, self.height
        try:
            root = self.monitor_display.screen().root
            for win in root.query_tree().children:
                try:
                    attrs = win.get_attributes()
                    if attrs.override_redirect or attrs.map_state != X.IsViewable:
                        continue
                    geo = win.get_geometry()
                    if geo.x == 0 and geo.y == 0 and geo.width == w and geo.height == h:
                        continue
                    win.configure(x=0, y=0, width=w, height=h, border_width=0)
                except Exception:
                    continue
            self.monitor_display.sync()
        except Exception as e:
            print(f"apply_target_size error: {e}")

    def enforce_layout(self):
        """Re-apply the target size when a window changed or just after a
        resize/launch (apps often assert their own size right after mapping).

        A window change applies immediately; the time-based launch window is
        throttled so it never adds X round-trips to every captured frame."""
        now = time.time()
        due = self._target_dirty or self._layout_dirty
        if not due and now < self._enforce_until and now - self._last_enforce >= 0.25:
            due = True
        if due:
            self._target_dirty = False
            self._layout_dirty = False
            self._last_enforce = now
            self.apply_target_size()

    def _has_visible_windows(self):
        try:
            root = self.monitor_display.screen().root
            for w in root.query_tree().children:
                try:
                    if w.get_attributes().map_state == X.IsViewable:
                        geo = w.get_geometry()
                        if geo.width > 1 and geo.height > 1:
                            return True
                except Exception:
                    continue
        except Exception:
            return True  # be conservative
        return False

    def check_app_exited(self):
        """True once the launched app is gone (process ended and no window left)."""
        if self.executable is None:
            return False
        process_alive = self.process is not None and self.process.poll() is None
        alive = process_alive or self._has_visible_windows()
        if alive:
            self._seen_alive = True
            self._dead_checks = 0
            return False
        if not self._seen_alive:
            return False
        self._dead_checks += 1
        return self._dead_checks >= 2

    def read_clipboard(self, selection='clipboard'):
        env = dict(os.environ, DISPLAY=self.display_name)
        try:
            out = subprocess.run(['xclip', '-o', '-selection', selection], env=env,
                                 capture_output=True, timeout=1.5)
            if out.returncode == 0:
                return out.stdout.decode('utf-8', errors='replace')
        except Exception as e:
            print(f"Clipboard read failed: {e}")
        return None

    def write_clipboard(self, text):
        env = dict(os.environ, DISPLAY=self.display_name)
        data = text.encode('utf-8')
        ok = False
        for selection in ('clipboard', 'primary'):
            try:
                proc = subprocess.Popen(['xclip', '-selection', selection, '-i'], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                proc.stdin.write(data)
                proc.stdin.close()
                ok = True
            except Exception as e:
                print(f"Clipboard write failed: {e}")
        # wait until xclip actually owns the selection, so a paste right after works
        deadline = time.time() + 0.5
        while ok and time.time() < deadline:
            if self.read_clipboard() == text:
                break
            time.sleep(0.02)
        return ok

    # --------------------------------------------------------------- resize
    def force_resize(self, height, width):
        """Set the target size. The actual window configure happens on the
        capture thread (apply_target_size), so only one thread ever drives this
        display's X connection for resizing. Enforced for a short window after,
        to catch apps that re-assert their own size just after being resized."""
        width = max(MIN_WIDTH, min(int(width), self.screen_width))
        height = max(MIN_HEIGHT, min(int(height), self.screen_height))
        with self._capture_lock:
            self.height = height
            self.width = width
            self.x = 0
            self.y = 0
        self._target_dirty = True
        self._enforce_until = time.time() + 1.5

    def smart_resize(self):
        """Automatically resize based on the inner windows sizes"""
        max_width, max_height = 0, 0
        for w in self.x11_display.screen().root.query_tree().children:
            try:
                geometry = w.get_geometry()
            except Exception:
                continue
            max_width = max(max_width, geometry.x + geometry.width)
            max_height = max(max_height, geometry.y + geometry.height)
        if max_width and max_height:
            with self._capture_lock:
                self.width = max(MIN_WIDTH, min(max_width, self.screen_width))
                self.height = max(MIN_HEIGHT, min(max_height, self.screen_height))
                self.x = 0
                self.y = 0

    def get_window_info(self):
        return {
            'id': self.display_id,
            'display': self.display_name,
            'width': self.width,
            'height': self.height,
            'executable': self.executable,
            'name': f"Window {self.display_id}"
        }


class DisplayManager:
    def __init__(self):
        self.displays = {}
        self.next_display_num = 2
        self.next_display_id = 1
        self.threadlock = threading.Lock()

    def create_display(self, width=1920, height=1080):
        print('create_display:: width, height', width, height)
        with self.threadlock:
            display_num = self.next_display_num
            # skip display numbers owned by another X server (stale or foreign)
            while os.path.exists(f'/tmp/.X{display_num}-lock') or \
                    os.path.exists(f'/tmp/.X11-unix/X{display_num}'):
                display_num += 1
            self.next_display_num = display_num + 1
            display_id = self.next_display_id
            self.next_display_id += 1
        display = SingleWindowDisplay(display_num, display_id, width, height)
        if display.start():
            with self.threadlock:
                self.displays[display_id] = display
            return display
        return None

    def start_executable(self, display_id, executable_path):
        display = self.get_display(display_id)
        if not display:
            raise Exception("Unknown display", display_id)
        env = os.environ.copy()
        env['DISPLAY'] = display.display_name
        process = subprocess.Popen(
            executable_path,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        display.process = process
        display.executable = executable_path
        # wait (up to 3 s) for a first window so the initial size is right
        deadline = time.time() + 3
        while time.time() < deadline:
            time.sleep(0.1)
            try:
                if display._has_visible_windows():
                    break
            except Exception:
                break
        display.smart_resize()
        # Keep enforcing the viewport size for a while: many apps (browsers,
        # Java tools) map their real window seconds after launch, or re-assert
        # their own size once; the capture loop keeps fitting it to the window.
        display._target_dirty = True
        display._enforce_until = time.time() + 8
        return process

    def remove_display(self, display_id):
        with self.threadlock:
            win = self.displays.pop(display_id, None)
        if win:
            win.stop()

    def resize_display(self, display_id, width, height):
        win = self.get_display(display_id)
        if win:
            win.force_resize(height, width)

    def get_display(self, display_id):
        return self.displays.get(display_id)

    def get_all_displays(self):
        return list(self.displays.values())

    def stop_all(self):
        with self.threadlock:
            displays = list(self.displays.values())
            self.displays.clear()
        for display in displays:
            display.stop()
