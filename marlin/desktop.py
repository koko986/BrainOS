"""Single-instance, resident desktop cockpit with explicit hide/show/exit."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
import webbrowser

from marlin.config import MarlinSettings


def instance_path(settings):
    return settings.database_path.resolve().with_suffix('.desktop.json')


def show_existing(settings) -> bool:
    try:
        info = json.loads(instance_path(settings).read_text(encoding='utf-8'))
        port = int(info['port'])
        if not 1024 <= port <= 65535:
            return False
        request = Request(f'http://127.0.0.1:{port}/api/desktop/show', data=b'{}',
                          headers={'Content-Type': 'application/json', 'X-Marlin-Token': info['token']})
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError, KeyError):
        return False


def launch(settings) -> int:
    if show_existing(settings):
        print('MARLIN is already running. Showing the cockpit.')
        return 0
    executable = Path(sys.executable)
    if sys.platform == 'win32' and executable.with_name('pythonw.exe').exists():
        executable = executable.with_name('pythonw.exe')
    command = [str(executable), '-m', 'marlin.desktop', str(settings.database_path.resolve())]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
    subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    print('Starting MARLIN in the background. Closing the cockpit hides it; Exit MARLIN stops it.')
    return 0


class DesktopController:
    def __init__(self, runtime, url):
        self.runtime = runtime
        self.url = url
        self.window = None
        self.tray = None
        self.server = None
        self.state_file = None
        self.exiting = False
        self.ready = False
        self._exit_lock = threading.Lock()
        self.force_terminate = False

    def control(self, action):
        if action == 'show':
            if not self.ready:
                return 'MARLIN is starting. The cockpit will appear shortly.'
            if self.window:
                self.window.show()
                self.window.restore()
            else:
                webbrowser.open(self.url)
            return 'Here I am.'
        if action == 'hide':
            if self.window:
                self.window.evaluate_js('if (typeof closeCamera === "function") closeCamera();')
                self.window.hide()
                return 'I am in the background. Say Hey MARLIN when you need me.'
            return 'Close the browser tab to hide the cockpit. I will keep listening.'
        if action == 'exit':
            if not self.exiting:
                self.exiting = True
                timer = threading.Timer(.6, self._exit)
                timer.daemon = False
                timer.start()
            return 'Shutting down MARLIN. Wake listening will stop.'
        raise ValueError('Unknown desktop action.')

    def closing(self):
        if self.exiting:
            return True
        self.control('hide')
        return False

    def _exit(self):
        if not self._exit_lock.acquire(blocking=False):
            return
        if self.force_terminate:
            watchdog = threading.Timer(3.0, lambda: os._exit(0))
            watchdog.daemon = True
            watchdog.start()
        print('Stopping MARLIN runtime...', flush=True)
        try:
            self.runtime.shutdown()
        except Exception as exc:
            print(f'Runtime shutdown warning: {exc}', flush=True)
        try:
            if self.server:
                self.server.should_exit = True
        except Exception as exc:
            print(f'Server shutdown warning: {exc}', flush=True)
        try:
            if self.state_file:
                Path(self.state_file).unlink(missing_ok=True)
        except Exception as exc:
            print(f'Instance cleanup warning: {exc}', flush=True)
        print('Stopping tray...', flush=True)
        try:
            if self.tray:
                self.tray.stop()
        except Exception as exc:
            print(f'Tray shutdown warning: {exc}', flush=True)
        print('Closing cockpit...', flush=True)
        try:
            if self.window:
                self.window.destroy()
        except Exception as exc:
            print(f'Cockpit shutdown warning: {exc}', flush=True)
        if self.force_terminate:
            os._exit(0)

    def start_tray(self):
        import pystray
        from PIL import Image, ImageDraw, ImageFont
        icon = Image.new('RGB', (64, 64), '#091216')
        draw = ImageDraw.Draw(icon)
        draw.rounded_rectangle((2, 2, 61, 61), radius=8, outline='#4a6d68', width=2)
        draw.line((8, 12, 20, 12), fill='#5cd8ca', width=2)
        draw.line((44, 52, 56, 52), fill='#5cd8ca', width=2)
        draw.ellipse((5, 9, 11, 15), fill='#5cd8ca')
        draw.ellipse((53, 49, 59, 55), fill='#f0c85c')
        try:
            font = ImageFont.truetype('segoeuib.ttf', 35)
        except OSError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), 'M', font=font)
        draw.text(((64 - (box[2] - box[0])) / 2, 12), 'M', fill='#d6e9df', font=font)
        self.tray = pystray.Icon('MARLIN', icon, 'MARLIN - listening in background', menu=pystray.Menu(
            pystray.MenuItem('Show MARLIN', lambda: self.control('show'), default=True),
            pystray.MenuItem('Hide cockpit', lambda: self.control('hide')),
            pystray.MenuItem('Exit MARLIN', lambda: self.control('exit')),
        ))
        self.tray.run_detached()


def run_host(settings):
    from marlin.cli import _available_port, _wait_for_server
    from marlin.runtime import MarlinRuntime
    from marlin.web import create_app
    import uvicorn

    mutex = None
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        identity = hashlib.sha256(str(settings.database_path.resolve()).lower().encode()).hexdigest()[:24]
        mutex = kernel.CreateMutexW(None, False, 'Local\\MARLIN-' + identity)
        if not mutex:
            raise OSError('Could not acquire MARLIN instance lock.')
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(mutex)
            for _ in range(30):
                if show_existing(settings):
                    return
                time.sleep(.5)
            return
    runtime = server = controller = None
    state_file = instance_path(settings)
    try:
        runtime = MarlinRuntime(settings)
        port = _available_port('127.0.0.1', settings.port)
        url = f'http://127.0.0.1:{port}'
        controller = DesktopController(runtime, url)
        controller.force_terminate = True
        runtime.desktop = controller
        app = create_app(runtime)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_config=None, access_log=False))
        controller.server = server
        controller.state_file = state_file
        threading.Thread(target=server.run, daemon=True, name='marlin-cockpit-server').start()
        _wait_for_server(url)
        state_file.write_text(json.dumps({'port': port, 'token': app.state.token, 'pid': os.getpid()}), encoding='utf-8')
        controller.start_tray()
        try:
            import webview
        except ImportError:
            controller.ready = True
            webbrowser.open(url)
            while not controller.exiting:
                time.sleep(.25)
        else:
            controller.window = webview.create_window('MARLIN V2', url, width=1360, height=860, min_size=(820, 560))
            controller.window.events.closing += controller.closing
            webview.start(lambda: setattr(controller, 'ready', True), private_mode=False, storage_path=str(settings.database_path.parent / 'desktop-profile'))
    finally:
        if controller and controller.tray:
            controller.tray.stop()
        if runtime:
            runtime.shutdown()
        if server:
            server.should_exit = True
        state_file.unlink(missing_ok=True)
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    config = MarlinSettings.from_env()
    if len(sys.argv) > 1:
        config.database_path = Path(sys.argv[1])
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    with config.database_path.with_suffix('.desktop.log').open('a', encoding='utf-8') as log:
        sys.stdout = sys.stderr = log
        run_host(config)
    # WebView2/.NET can retain non-daemon helper threads after window destruction.
    # Runtime, tray, instance file, and database contexts are already cleaned up.
    os._exit(0)
