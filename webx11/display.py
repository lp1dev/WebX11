import subprocess
import threading
import Xlib
import time
import base64
import io
from PIL import Image

from Xlib import X, XK
from webx11.window import WindowScreenCapture, WindowInputHandler
from io import BytesIO
import gzip

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
        self.quality = 30
        self.dpi = 200
        self.maxwidth = width
        self.maxheight = height
        
    def start(self):
        """Start the virtual display for this window"""
        try:
            # Start Xvfb
            self.xvfb_process = subprocess.Popen([
                'Xvfb', self.display_name, 
                '-screen', '0', f'{self.width}x{self.height}x{self.depth}',
                '-ac',
                '-nolisten', 'tcp'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Wait for Xvfb to start
            for i in range(10):
                time.sleep(0.5)
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
        """Capture the window content"""
        if self.screen_capture:
            capture = self.screen_capture.capture_window(self.x, self.y, self.height, self.width, self.quality, self.dpi, force)
            # print('Force, capture', force, capture)
            if capture != self.last_frame or force:
                self.last_frame = capture
                self.has_updated = True
                if compressed:
                    with BytesIO() as out:
                        with gzip.GzipFile(fileobj=out, mode="w") as f:
                            f.write(capture)
                    # image_b64 = base64.b64encode(out.getvalue()).decode('utf-8')
                    # image_data = f"data:image/jpg;base64,{image_b64}"
                    #
                        print('Compressed/Uncompressed', len(out.getvalue()), len(capture))
                        return out.getvalue()
                return capture
            self.has_updated = False
        return None
    
    def force_resize(self, height, width):
        win = self.x11_display.screen().root
        self.height = height
        self.width = width
        self.x = 0
        self.y = 0
        print('force_resize:: expected new dimensions', height, width)

        print('force_resize:: geometry before', win.get_geometry())
        # win.configure(x=0, y=0, width=width, height=height, border_width=0)
        # win.change_attributes(win_gravity=X.NorthWestGravity, bit_gravity=X.StaticGravity)

        self.x11_display.sync()
        print('Win', win.get_wm_name(), win.get_geometry())

        children = self.x11_display.screen().root.query_tree().children
        for w in children:
            if w.get_wm_name() is not None:
                print('\tWinChild', w.get_wm_name(), w.get_geometry())
                w.configure(x=0, y=0, width=width, height=height, border_width=0)
                # w.change_attributes(win_gravity=X.NorthWestGravity, bit_gravity=X.StaticGravity)
                self.x11_display.sync()
        print('force_resize:: geometry after children update', win.get_geometry())

    
    def smart_resize(self):
        """Automatically resize based on the inner windows sizes"""

        max_width, max_height, max_x, max_y = 0, 0, 0, 0

        children = self.x11_display.screen().root.query_tree().children
        for w in children:
            geometry = w.get_geometry()
            print("Smart resize", w, w.get_wm_name(), geometry)
            if geometry.width > max_width:
                max_width = geometry.width
            if geometry.height > max_height:
                max_height = geometry.height
            if geometry.x > max_x:
                max_x = geometry.x
            if geometry.y > max_y:
                max_y = geometry.y

        self.height = max_height
        self.width = max_width
        self.x = max_x
        self.y = max_y
        print('smart resized to h/w x+y', self.height, self.width, self.x, self.y)
    
    def get_windows(self):
        """ List the windows in a display """
        children = self.x11_display.screen().root.query_tree().children
        # Window controls on https://github.com/python-xlib/python-xlib/blob/4e8bbf8fc4941e5da301a8b3db8d27e98de68666/Xlib/xobject/drawable.py#L668
        for w in children:
            print(w, w.get_wm_name())
            # TODO : The code below is a working poc to see how to interact with windows
            #       I noticed that it is not possible to take a screenshot of a window that is not the front window and having multiple windows might cause issues
            # geometry = w.get_geometry()
            # print(geometry)
            # if geometry.width > 1 and geometry.height > 1:
            #     w.raise_window()
            #     time.sleep(1) # We wait for the window to come to the front
            #     raw = w.get_image(0, 0, geometry.width, geometry.height, X.ZPixmap, 0xffffffff)
            #     image = Image.frombytes("RGB", (geometry.width, geometry.height), raw.data, "raw", "BGRX")
            #     buffer = io.BytesIO()

            #     image.save(buffer, format='WEBP', dpi=[200, 200], quality=50)
                # print(base64.b64encode(buffer.getvalue()))
            # print(raw)
        return

    def get_window_info(self):
        """Get window information"""
        return {
            'id': self.display_id,
            'display': self.display_name,
            'width': self.width,
            'height': self.height,
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
            
            # Create display
            display = SingleWindowDisplay(display_num, display_id, width, height)
            
            if display.start():
                self.displays[display_id] = display
                return display
            return None
    
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
