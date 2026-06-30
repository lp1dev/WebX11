import subprocess
import threading
import Xlib
import time
import os

from concurrent.futures import ThreadPoolExecutor

from Xlib import X
import Xlib.threaded
from webx11.window import WindowScreenCapture, WindowInputHandler
from webx11.settings import SettingsManager


class SingleWindowDisplay:
    def __init__(self, display_num, display_id, width=1920, height=1080, depth=24):
        self.display_num = display_num
        self.display_name = f":{display_num}"
        self.display_id = display_id
        self.width = width
        self.height = height
        self.x = 0
        self.y = 0
        self.depth = depth
        self.xvfb_process = None
        self.x11_display = None
        self.screen_capture = None
        self.input_handler = None
        self.is_running = False
        self.has_updated = False
        self.last_frame = None
        self.settings = SettingsManager()
        self.maxwidth = width
        self.maxheight = height
        self.executable = None

        # All captures for a display run on ONE thread so the mss/X connection
        # is never touched concurrently. Captures are already serialized per
        # display by the transport's await, so a single worker is correct and
        # keeps the heavy capture+encode off the asyncio event loop.
        self.capture_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"capture-{display_id}")

    def start(self):
        """Start the virtual display for this window"""
        try:
            print('DISPLAY NAME', self.display_name)
            cmd = [
                'Xvfb', self.display_name,
                '-screen', '0', f'{self.width}x{self.height}x{self.depth}',
                '-ac',
                '-nolisten', 'tcp'
            ]
            print('cmd is', " ".join(cmd))
            self.xvfb_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            # Wait for Xvfb to start
            for i in range(10):
                time.sleep(1)
                try:
                    test_display = Xlib.display.Display(self.display_name)
                    test_display.close()
                    break
                except Exception as e:
                    print(e)
                    if i == 9:
                        raise Exception("Xvfb failed to start")
                    continue

            # Connect to the display
            self.x11_display = Xlib.display.Display(self.display_name)
            self.screen_capture = WindowScreenCapture(self)
            self.input_handler = WindowInputHandler(self)

            self.is_running = True
            print(f"Window display started on {self.display_name} (ID: {self.display_id})")
            return True

        except Exception as e:
            print(f"Failed to start window display: {e}")
            if self.xvfb_process:
                self.xvfb_process.terminate()
            return False

    def stop(self):
        """Stop the virtual display"""
        self.is_running = False
        try:
            self.capture_executor.shutdown(wait=False)
        except Exception:
            pass
        if self.screen_capture:
            try:
                self.screen_capture.close()
            except Exception:
                pass
        if self.xvfb_process:
            self.xvfb_process.terminate()
            self.xvfb_process.wait()
        if self.x11_display:
            try:
                self.x11_display.close()
            except Xlib.error.ConnectionClosedError:
                print("Display closed.")
        print(f"Window display stopped (ID: {self.display_id})")

    def get_display(self):
        return self.x11_display

    def capture_window(self, compressed=False, force=False):
        """Capture the window content as a serialized tile frame (or None).

        Runs in the per-display capture executor. `compressed` is kept for
        signature compatibility but is unused: tiles are already image-encoded.
        """
        if not self.screen_capture:
            return None
        frame = self.screen_capture.capture_window(
            self.x, self.y, self.height, self.width,
            self.settings.image_quality, self.settings.dpi, force)
        self.has_updated = frame is not None
        if frame is not None:
            self.last_frame = frame
        return frame

    def capture_keyframe(self):
        """Full-frame keyframe that does not disturb the diff baseline. Used to
        seed a newly connected client without starving the others."""
        if not self.screen_capture:
            return None
        return self.screen_capture.capture_keyframe(
            self.x, self.y, self.height, self.width)

    def force_resize(self, height, width):
        # Always make sure that the size defined when starting the X server is
        # larger than the size you try to resize with.
        win = self.x11_display.screen().root
        self.height = height
        self.width = width
        self.x = 0
        self.y = 0
        print('force_resize:: expected new dimensions', height, width)
        print('force_resize:: geometry before', win.get_geometry())

        win.configure(x=0, y=0, width=width, height=height, border_width=0)
        win.change_attributes(win_gravity=X.NorthWestGravity, bit_gravity=X.StaticGravity)

        self.x11_display.sync()
        print('Win', win.get_wm_name(), win.get_geometry())

        children = self.x11_display.screen().root.query_tree().children
        for w in children:
            if w.get_wm_name() is not None:
                print('\tWinChild', w.get_wm_name(), w.get_geometry())
                w.configure(x=0, y=0, width=width, height=height, border_width=0)
                self.x11_display.sync()
        print('force_resize:: geometry after children update', win.get_geometry())

    def smart_resize(self):
        """Automatically resize based on the inner windows sizes"""
        max_width, max_height, max_x, max_y = 0, 0, 0, 0

        children = self.x11_display.screen().root.query_tree().children
        for w in children:
            try:
                geometry = w.get_geometry()
            except Exception:
                continue
            print("Smart resize", w, w.get_wm_name(), geometry)
            if geometry.width > max_width:
                max_width = geometry.width
            if geometry.height > max_height:
                max_height = geometry.height
            if geometry.x > max_x:
                max_x = geometry.x
            if geometry.y > max_y:
                max_y = geometry.y

        # No mapped window has appeared yet (e.g. the app is still starting up).
        # Collapsing to 0x0 would make every capture ask mss for a zero-size
        # region -> "can't allocate picture frame". Fall back to the full Xvfb
        # screen and let resize-x11 shrink it once the browser reports its size.
        if max_width <= 0 or max_height <= 0:
            max_width = self.width if self.width else self.maxwidth
            max_height = self.height if self.height else self.maxheight
            max_x, max_y = 0, 0

        self.height = max_height
        self.width = max_width
        self.x = max_x
        self.y = max_y
        print('smart resized to h/w x+y', self.height, self.width, self.x, self.y)

    def get_window_info(self):
        """Get window information"""
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
        """Create a new virtual display"""
        print('create_display:: width, height', width, height)
        with self.threadlock:
            display_num = self.next_display_num
            self.next_display_num += 1

            display_id = self.next_display_id
            self.next_display_id += 1

            display = SingleWindowDisplay(display_num, display_id, width, height)

            if display.start():
                self.displays[display_id] = display
                return display
            return None

    def start_executable(self, display_id, executable_path):
        with self.threadlock:
            if display_id in self.displays:
                display = self.displays[display_id]
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
                time.sleep(1)  # Wait for the window so we can read its actual size
                display.smart_resize()
                return process
            raise Exception("Unknown display", display_id)

    def remove_display(self, display_id):
        """Remove a window display"""
        with self.threadlock:
            if display_id in self.displays:
                win = self.displays[display_id]
                del self.displays[display_id]
                win.stop()

    def resize_display(self, display_id, width, height):
        print('resize_display:: width, height', width, height)
        """Force resize a display"""
        with self.threadlock:
            if display_id in self.displays:
                print('Found window id', display_id)
                win = self.displays[display_id]
                win.force_resize(height, width)

    def get_display(self, display_id):
        """Get a window display by ID"""
        return self.displays.get(display_id)

    def get_all_displays(self):
        """Get all displays"""
        return list(self.displays.values())

    def stop_all(self):
        """Stop all displays"""
        with self.threadlock:
            for display in self.displays.values():
                display.stop()
            self.displays.clear()
