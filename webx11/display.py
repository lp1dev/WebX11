import subprocess
import threading
import Xlib
import time
import base64
from webx11.window import WindowScreenCapture, WindowInputHandler
from io import BytesIO
import gzip

class SingleWindowDisplay:
    def __init__(self, display_num, window_id, width=1920, height=1080, depth=24):
        self.display_num = display_num
        self.display_name = f":{display_num}"
        self.window_id = window_id
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
            print(f"Window display started on {self.display_name} (ID: {self.window_id})")
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
        print(f"Window display stopped (ID: {self.window_id})")
    
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
    
    def get_window_info(self):
        """Get window information"""
        return {
            'id': self.window_id,
            'display': self.display_name,
            'width': self.width,
            'height': self.height,
            'name': f"Window {self.window_id}"
        }

class WindowDisplayManager:
    def __init__(self):
        self.windows = {}
        self.next_display_num = 2
        self.next_window_id = 1
        self.window_lock = threading.Lock()
        
    def create_window_display(self, width=1920, height=1080):
        """Create a new virtual display for a window"""
        print('create_window_display:: width, height', width, height)
        with self.window_lock:
            display_num = self.next_display_num
            self.next_display_num += 1
            
            window_id = self.next_window_id
            self.next_window_id += 1
            
            # Create window display
            window_display = SingleWindowDisplay(display_num, window_id, width, height)
            
            if window_display.start():
                self.windows[window_id] = window_display
                return window_display
            return None
    
    def remove_window_display(self, window_id):
        """Remove a window display"""
        with self.window_lock:
            if window_id in self.windows:
                win = self.windows[window_id]
                del self.windows[window_id]
                win.stop()

    def resize_window_display(self, window_id, width, height):
        print('resize_window_display:: width, height', width, height)
        print('in resize window display', window_id)
        """Force resize a window display"""
        with self.window_lock:
            if window_id in self.windows:
                print('Found window id', window_id)
                win = self.windows[window_id]
                win.force_resize(height, width)
    
    def get_window_display(self, window_id):
        """Get a window display by ID"""
        return self.windows.get(window_id)
    
    def get_all_windows(self):
        """Get all window displays"""
        return list(self.windows.values())
    
    def stop_all(self):
        """Stop all window displays"""
        with self.window_lock:
            for window in self.windows.values():
                window.stop()
            self.windows.clear()
