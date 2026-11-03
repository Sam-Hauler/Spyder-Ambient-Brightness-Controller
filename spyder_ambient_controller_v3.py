"""Spyder5 tray brightness controller v2 - Windows, Python 3.10+.
Install: py -3 -m pip install monitorcontrol pystray pillow
"""
import ctypes
import json
import math
import os
import queue
import shutil
import webbrowser
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

APP_ID = 'Spyder5AmbientBrightnessController'
APP_NAME = 'Spyder Ambient Brightness Controller'
ARGYLL_URL = 'https://argyllcms.com/downloadwin.html'
CONFIG_FILE = Path(os.environ.get('APPDATA', str(Path.home()))) / APP_ID / 'settings.json'
STARTUP_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
DEFAULTS = {
    'spotread': r'C:\Argyll_V3.5.0\bin\spotread.exe',
    'low_lux': 1.0, 'high_lux': 500.0, 'min_brightness': 20,
    'max_brightness': 80, 'smoothing': 25.0, 'transition_seconds': 3.0,
    'sensor_interval': 2.0, 'monitor_index': 0,
    'enabled_monitors': None, 'auto_on_launch': True,
    'start_minimized': True, 'full_view': False,
}
DDC_INTERVAL = 0.14
BG, PANEL, ENTRY, FG, MUTED, ACCENT, LINE = ('#15191f', '#202630', '#2a3340', '#f1f5f9', '#a4b2c3', '#F59E0B', '#394454')


def load_settings():
    data = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    tmp.replace(CONFIG_FILE)


def startup_command():
    exe = Path(sys.executable).resolve()
    if getattr(sys, 'frozen', False):
        return f'"{exe}" --background'
    if exe.name.lower() == 'python.exe' and exe.with_name('pythonw.exe').exists():
        exe = exe.with_name('pythonw.exe')
    return f'"{exe}" "{Path(__file__).resolve()}" --background'


def startup_enabled():
    if os.name != 'nt':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            return winreg.QueryValueEx(key, APP_ID)[0] == startup_command()
    except OSError:
        return False


def set_startup(enabled):
    if os.name != 'nt':
        raise RuntimeError('Windows startup requires Windows.')
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass


def find_spotread(configured=''):
    """Locate spotread without assuming a particular ArgyllCMS version."""
    candidates = [configured, shutil.which('spotread.exe')]
    for base in (Path('C:/'), Path(os.environ.get('ProgramFiles', 'C:/Program Files')),
                 Path(os.environ.get('LOCALAPPDATA', str(Path.home())))):
        try:
            candidates.extend(str(x) for x in base.glob('Argyll*/bin/spotread.exe'))
            candidates.extend(str(x) for x in base.glob('Argyll*/spotread.exe'))
        except OSError:
            pass
    for item in candidates:
        if item and Path(item).is_file():
            return str(Path(item).resolve())
    return None


def read_spyder(path):
    """Read any instrument supported by Argyll's ambient mode.

    Lux is the portable result. XYZ is optional, model-specific diagnostic data.
    The -c 1 option is configurable below for setups with multiple instruments.
    """
    exe = find_spotread(path)
    if not exe:
        raise FileNotFoundError('ArgyllCMS spotread.exe not found. Install ArgyllCMS or set its path in Full view.')
    result = subprocess.run([exe, '-D', '3', '-c', '1', '-a', '-N', '-O'],
                            capture_output=True, text=True, errors='replace', timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output = result.stdout + '\n' + result.stderr
    lux = re.search(r'\bAmbient\s*=\s*([-+\d.eE]+)\s*Lux\b', output, re.I)
    if not lux:
        raise RuntimeError('No ambient lux reading. Check instrument compatibility, USB driver, and spotread output: ' + output[-450:].strip())
    xyz = re.search(r'spyd2_GetAmbientReading:\s*returning\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)', output, re.I)
    return (tuple(float(v) for v in xyz.groups()) if xyz else None), float(lux.group(1))


def brightness_for_lux(lux, cfg):
    lo, hi = cfg['low_lux'], cfg['high_lux']
    fraction = 0.0 if lux <= lo else 1.0 if lux >= hi else math.log(lux / lo) / math.log(hi / lo)
    return round(cfg['min_brightness'] + fraction * (cfg['max_brightness'] - cfg['min_brightness']))


def validate(cfg):
    cfg = dict(cfg)
    for key in ('low_lux', 'high_lux', 'smoothing', 'transition_seconds', 'sensor_interval'):
        cfg[key] = float(cfg[key])
        if not math.isfinite(cfg[key]):
            raise ValueError(f'{key} must be a finite number')
    for key in ('min_brightness', 'max_brightness'):
        cfg[key] = int(cfg[key])
    if not 0 < cfg['low_lux'] < cfg['high_lux']:
        raise ValueError('Lux thresholds must satisfy 0 < low < high.')
    if not 0 <= cfg['min_brightness'] <= cfg['max_brightness'] <= 100:
        raise ValueError('Brightness must be between 0 and 100%.')
    if not 0 < cfg['smoothing'] <= 100:
        raise ValueError('Smoothing must be between 0 and 100%.')
    if not 0.1 <= cfg['transition_seconds'] <= 120:
        raise ValueError('Transition must be between 0.1 and 120 seconds.')
    if not 1 <= cfg['sensor_interval'] <= 120:
        raise ValueError('Sensor interval must be between 1 and 120 seconds.')
    return cfg


def work_area(root):
    """Primary monitor work area excludes the Windows taskbar."""
    if os.name == 'nt':
        class RECT(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                        ('right', ctypes.c_long), ('bottom', ctypes.c_long)]
        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class App:
    def __init__(self, root, background=False):
        self.root = root
        self.cfg = validate(load_settings())
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.ddc_lock = threading.Lock()
        self.shutdown = threading.Event()
        self.sensor_stop = threading.Event()
        self.wake = threading.Event()
        self.sensor_active = False
        self.auto = False
        self.lux = None
        self.xyz = None
        self.smooth = None
        self.target = None
        self.monitors = {}  # index -> {actual, last_write, error}
        self.monitor_vars = {}
        self.monitor_labels = {}
        self.tray_icon = None
        self.tray_ready = False
        self.full = bool(self.cfg.get('full_view', False))
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.style_gui()
        self.build_gui()
        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)
        self.root.after(100, self.drain)
        self.root.after(250, self.position_window)
        if background or self.cfg.get('start_minimized', True):
            self.root.withdraw()
        self.start_tray()
        self.root.after(900, self.initial_argyll_check)
        threading.Thread(target=self.transition_loop, daemon=True).start()
        self.detect_monitors()
        if self.cfg.get('auto_on_launch', True):
            self.start_sensor()
            self.set_auto(True)
        self.root.overrideredirect(True)

    def initial_argyll_check(self):
        found = find_spotread(self.cfg.get('spotread', ''))
        if found:
            if found != self.cfg.get('spotread'):
                self.cfg['spotread'] = found
                self.vars['spotread'].set(found)
                try:
                    save_settings(self.cfg)
                except OSError:
                    pass
        else:
            self.sensor_status.config(text='ArgyllCMS not found — open Full view to install')
            self.set_auto(False)
            self.stop_sensor()
            if messagebox.askyesno('ArgyllCMS required',
                    'This controller needs ArgyllCMS spotread.exe to read your sensor.\n\nOpen the official Windows download page?'):
                webbrowser.open(ARGYLL_URL)

    def style_gui(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        s.configure('.', background=BG, foreground=FG, font=('Segoe UI', 10))
        s.configure('TFrame', background=BG)
        s.configure('Panel.TFrame', background=PANEL)
        s.configure('TLabel', background=BG, foreground=FG)
        s.configure('Muted.TLabel', background=BG, foreground=MUTED)
        s.configure('Panel.TLabel', background=PANEL, foreground=FG)
        s.configure('TButton', background=ENTRY, foreground=FG, borderwidth=0, padding=(10, 7))
        s.map('TButton', background=[('active', LINE)])
        s.configure('Accent.TButton', background='#176a9e', foreground='white', padding=(12, 8))
        s.map('Accent.TButton', background=[('active', '#2385bf')])
        s.configure('TCheckbutton', background=BG, foreground=FG)
        s.map('TCheckbutton', background=[('active', BG)], foreground=[('active', FG)])
        s.configure('TEntry', fieldbackground=ENTRY, foreground=FG, insertcolor=FG, bordercolor=LINE)
        s.configure('TSeparator', background=LINE)

    def build_gui(self):
        self.outer = ttk.Frame(self.root, padding=16)
        self.outer.pack(fill='both', expand=True)
        title = ttk.Frame(self.outer)
        title.pack(fill='x')
        ttk.Label(title, text='Spyder Brightness', font=('Segoe UI', 15, 'bold')).pack(side='left')
        self.full_btn = ttk.Button(title, text='Compact view' if self.full else 'Full view', command=self.toggle_full)
        self.full_btn.pack(side='right')
        ttk.Label(self.outer, text='AMBIENT LIGHT', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(16, 0))
        self.lux_label = ttk.Label(self.outer, text='-- Lux', font=('Segoe UI', 30, 'bold'), foreground=ACCENT)
        self.lux_label.pack(anchor='w')
        self.sensor_status = ttk.Label(self.outer, text='Starting sensor...', style='Muted.TLabel')
        self.sensor_status.pack(anchor='w', pady=(0, 8))
        bar = ttk.Frame(self.outer)
        bar.pack(fill='x', pady=(0, 10))
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='Automatic brightness', variable=self.auto_var,
                        command=lambda: self.set_auto(self.auto_var.get())).pack(side='left')
        ttk.Button(bar, text='Refresh', command=self.detect_monitors).pack(side='right')
        ttk.Separator(self.outer).pack(fill='x', pady=(2, 8))
        ttk.Label(self.outer, text='MONITORS', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.monitors_frame = ttk.Frame(self.outer)
        self.monitors_frame.pack(fill='x', pady=(5, 4))
        self.ddc_status = ttk.Label(self.outer, text='Detecting monitors...', style='Muted.TLabel', wraplength=350)
        self.ddc_status.pack(anchor='w', pady=(0, 8))
        actions = ttk.Frame(self.outer)
        actions.pack(fill='x', pady=(4, 2))
        ttk.Button(actions, text='Hide to tray', command=self.hide_window).pack(side='left')
        ttk.Button(actions, text='Save settings', style='Accent.TButton', command=self.apply_settings).pack(side='right')

        self.advanced = ttk.Frame(self.outer)
        ttk.Separator(self.advanced).pack(fill='x', pady=10)
        ttk.Label(self.advanced, text='ADVANCED SETTINGS', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.vars = {}
        rows = [
            ('low_lux', 'Low lux'), ('high_lux', 'High lux'),
            ('min_brightness', 'Minimum brightness %'), ('max_brightness', 'Maximum brightness %'),
            ('smoothing', 'Smoothing %'), ('transition_seconds', 'Transition seconds'),
            ('sensor_interval', 'Sensor interval seconds'), ('spotread', 'ArgyllCMS spotread.exe path'),
        ]
        for key, label in rows:
            line = ttk.Frame(self.advanced)
            line.pack(fill='x', pady=2)
            ttk.Label(line, text=label, width=25).pack(side='left')
            v = tk.StringVar(value=str(self.cfg[key]))
            self.vars[key] = v
            ttk.Entry(line, textvariable=v).pack(side='left', fill='x', expand=True)
        argyll_tools = ttk.Frame(self.advanced)
        argyll_tools.pack(fill='x', pady=(4, 6))
        ttk.Button(argyll_tools, text='Detect ArgyllCMS', command=self.check_argyll).pack(side='left')
        ttk.Button(argyll_tools, text='Download ArgyllCMS', command=lambda: webbrowser.open(ARGYLL_URL)).pack(side='left', padx=8)
        ttk.Label(self.advanced, text='MANUAL MONITOR BRIGHTNESS', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 4))
        manual = ttk.Frame(self.advanced)
        manual.pack(fill='x')
        self.manual_monitor = ttk.Combobox(manual, state='readonly', width=15)
        self.manual_monitor.pack(side='left', padx=(0, 5))
        self.manual_var = tk.DoubleVar(value=50)
        ttk.Scale(manual, from_=0, to=100, variable=self.manual_var).pack(side='left', fill='x', expand=True)
        ttk.Button(manual, text='Apply', command=self.manual_apply).pack(side='right', padx=(5, 0))
        ttk.Label(self.advanced, text='WINDOWS & TRAY', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 4))
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(self.advanced, text='Start with Windows sign-in', variable=self.startup_var,
                        command=self.change_startup).pack(anchor='w')
        self.launch_auto_var = tk.BooleanVar(value=self.cfg.get('auto_on_launch', True))
        ttk.Checkbutton(self.advanced, text='Automatically enable brightness on launch', variable=self.launch_auto_var).pack(anchor='w')
        self.minimized_var = tk.BooleanVar(value=self.cfg.get('start_minimized', True))
        ttk.Checkbutton(self.advanced, text='Launch hidden in system tray', variable=self.minimized_var).pack(anchor='w')
        ttk.Button(self.advanced, text='Start sensor', command=self.start_sensor).pack(anchor='w', pady=(8, 3))
        ttk.Button(self.advanced, text='Stop sensor', command=self.stop_sensor).pack(anchor='w')
        self.xyz_label = ttk.Label(self.advanced, text='XYZ: --', style='Muted.TLabel')
        self.xyz_label.pack(anchor='w', pady=(10, 4))
        ttk.Label(self.advanced, text='ACTIVITY', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.log_widget = tk.Text(self.advanced, height=6, bg=ENTRY, fg=FG, insertbackground=FG,
                                  relief='flat', font=('Consolas', 9), wrap='word')
        self.log_widget.pack(fill='x', pady=4)
        ttk.Button(self.advanced, text='Exit completely', command=self.exit_app).pack(anchor='e', pady=4)
        self.update_full()

    def update_full(self):
        if self.full:
            self.advanced.pack(fill='both', expand=True, pady=(6, 0))
        else:
            self.advanced.pack_forget()
        self.full_btn.config(text='Compact view' if self.full else 'Full view')
        self.root.after_idle(self.position_window)

    def toggle_full(self):
        self.full = not self.full
        self.update_full()

    def position_window(self):
        if self.shutdown.is_set():
            return
        self.root.update_idletasks()
        width = 520 if self.full else 380
        height = min(self.root.winfo_reqheight() + 8, work_area(self.root)[3] - work_area(self.root)[1] - 25)
        left, top, right, bottom = work_area(self.root)
        x = max(left, right - width - 12)
        y = max(top, bottom - height - 12)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.root.minsize(width, min(height, 280))
        self.root.maxsize(width, height)

    def log(self, text):
        self.log_widget.insert('end', time.strftime('%H:%M:%S ') + text + '\n')
        self.log_widget.see('end')
        if int(self.log_widget.index('end-1c').split('.')[0]) > 250:
            self.log_widget.delete('1.0', '51.0')

    def get_monitor(self, index, write=None):
        from monitorcontrol import get_monitors
        with self.ddc_lock:
            mons = get_monitors()
            if not 0 <= index < len(mons):
                raise RuntimeError(f'Monitor {index + 1} is unavailable')
            with mons[index] as mon:
                if write is None:
                    return int(mon.get_luminance())
                mon.set_luminance(max(0, min(100, int(write))))
                return int(write)

    def detect_monitors(self):
        def worker():
            try:
                from monitorcontrol import get_monitors
                with self.ddc_lock:
                    mons = get_monitors()
                    count = len(mons)
                self.events.put(('monitors', count))
            except Exception as exc:
                self.events.put(('error', 'Monitor detection: ' + str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def selected(self):
        with self.lock:
            return sorted(i for i in self.monitors if i in self.cfg.get('enabled_monitors', []))

    def monitor_toggle(self, index):
        selected = sorted(i for i, var in self.monitor_vars.items() if var.get())
        with self.lock:
            self.cfg['enabled_monitors'] = selected
            for i in self.monitors:
                self.monitors[i]['actual'] = None
            self.wake.set()
        self.log('Enabled monitors: ' + (', '.join(str(i + 1) for i in selected) or 'none'))
        try:
            save_settings(self.cfg)
        except OSError as exc:
            self.events.put(('error', f'Save failed: {exc}'))

    def read_monitors(self):
        def worker():
            for i in list(self.monitors):
                try:
                    value = self.get_monitor(i)
                    self.events.put(('read', (i, value)))
                except Exception as exc:
                    self.events.put(('monitor_error', (i, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def manual_apply(self):
        index = self.manual_monitor.current()
        if index < 0:
            messagebox.showinfo('Select monitor', 'Choose a monitor first.')
            return
        if self.auto:
            messagebox.showinfo('Automatic brightness', 'Pause automatic brightness before manual control.')
            return
        value = round(self.manual_var.get())
        if not messagebox.askyesno('Confirm brightness', f'Set monitor {index + 1} to {value}%?'):
            return
        def worker():
            try:
                applied = self.get_monitor(index, value)
                self.events.put(('read', (index, applied)))
            except Exception as exc:
                self.events.put(('monitor_error', (index, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def check_argyll(self):
        configured = self.vars['spotread'].get().strip()
        found = find_spotread(configured)
        if found:
            self.vars['spotread'].set(found)
            messagebox.showinfo('ArgyllCMS found', f'Found spotread.exe at:\n{found}\n\nSave settings to keep this path.')
        else:
            if messagebox.askyesno('ArgyllCMS not found',
                    'Could not find spotread.exe in the configured location, PATH, or common folders.\n\nOpen the official ArgyllCMS download page?'):
                webbrowser.open(ARGYLL_URL)

    def start_sensor(self):
        with self.lock:
            if self.sensor_active:
                return
            self.sensor_active = True
        self.sensor_stop.clear()
        self.sensor_status.config(text='Reading ambient sensor...')
        threading.Thread(target=self.sensor_loop, daemon=True).start()

    def stop_sensor(self):
        with self.lock:
            self.sensor_active = False
        self.sensor_stop.set()
        self.set_auto(False)
        self.sensor_status.config(text='Stopping sensor...')

    def sensor_loop(self):
        while not self.shutdown.is_set():
            with self.lock:
                if not self.sensor_active:
                    break
                cfg = dict(self.cfg)
            try:
                xyz, lux = read_spyder(cfg['spotread'])
                self.events.put(('sensor', (xyz, lux)))
                with self.lock:
                    if self.sensor_active and self.auto:
                        raw = brightness_for_lux(lux, cfg)
                        f = cfg['smoothing'] / 100.0
                        self.smooth = float(raw) if self.smooth is None else self.smooth + (raw - self.smooth) * f
                        self.target = round(self.smooth)
                        self.wake.set()
            except Exception as exc:
                self.events.put(('sensor_error', str(exc)))
            if self.sensor_stop.wait(cfg['sensor_interval']):
                break
        self.events.put(('sensor_stopped', None))

    def set_auto(self, enabled):
        with self.lock:
            self.auto = bool(enabled)
            self.target = None
            self.smooth = None
            for state in self.monitors.values():
                state['actual'] = None
        self.wake.set()
        self.events.put(('auto', self.auto))
        if enabled and not self.sensor_active:
            self.start_sensor()

    def transition_loop(self):
        while not self.shutdown.is_set():
            self.wake.wait(0.15)
            self.wake.clear()
            with self.lock:
                if not self.auto or not self.sensor_active or self.target is None:
                    continue
                target = self.target
                duration = self.cfg['transition_seconds']
                enabled = [i for i in self.cfg.get('enabled_monitors', []) if i in self.monitors]
            for index in enabled:
                if self.shutdown.is_set():
                    return
                with self.lock:
                    if not self.auto or not self.sensor_active or index not in self.cfg.get('enabled_monitors', []):
                        continue
                    state = self.monitors.get(index)
                    if state is None:
                        continue
                    actual = state['actual']
                    last_write = state['last_write']
                try:
                    if actual is None:
                        actual = self.get_monitor(index)
                        with self.lock:
                            if index in self.monitors:
                                self.monitors[index]['actual'] = actual
                    if actual == target:
                        continue
                    interval = max(DDC_INTERVAL, duration / max(1, abs(target - actual)))
                    if time.monotonic() - last_write < interval:
                        continue
                    with self.lock:
                        if not self.auto or index not in self.cfg.get('enabled_monitors', []):
                            continue
                        target = self.target
                        if target is None or target == actual:
                            continue
                    next_value = actual + (1 if target > actual else -1)
                    applied = self.get_monitor(index, next_value)
                    with self.lock:
                        if index in self.monitors:
                            self.monitors[index]['actual'] = applied
                            self.monitors[index]['last_write'] = time.monotonic()
                    self.events.put(('auto_set', (index, applied)))
                except Exception as exc:
                    self.events.put(('monitor_error', (index, str(exc))))
                    self.shutdown.wait(1.0)

    def apply_settings(self):
        try:
            cfg = dict(self.cfg)
            for key, var in self.vars.items():
                cfg[key] = var.get()
            cfg['auto_on_launch'] = self.launch_auto_var.get()
            cfg['start_minimized'] = self.minimized_var.get()
            cfg['full_view'] = self.full
            cfg = validate(cfg)
            save_settings(cfg)
            with self.lock:
                self.cfg = cfg
                self.target = None
                self.smooth = None
            self.wake.set()
            self.log('Settings saved.')
            self.ddc_status.config(text='Settings saved')
        except Exception as exc:
            messagebox.showerror('Settings error', str(exc))

    def change_startup(self):
        try:
            set_startup(self.startup_var.get())
            self.log('Windows startup ' + ('enabled' if self.startup_var.get() else 'disabled'))
        except Exception as exc:
            self.startup_var.set(startup_enabled())
            messagebox.showerror('Startup error', str(exc))

    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new('RGB', (64, 64), (20, 26, 34))
            draw = ImageDraw.Draw(image)
            draw.ellipse((9, 9, 55, 55), fill=(121, 196, 255))
            draw.ellipse((22, 22, 42, 42), fill=(20, 26, 34))
            menu = pystray.Menu(
                pystray.MenuItem('Open controller', lambda icon, item: self.events.put(('tray_open', None)), default=True),
                pystray.MenuItem('Enable automatic brightness', lambda icon, item: self.events.put(('tray_auto', True))),
                pystray.MenuItem('Pause automatic brightness', lambda icon, item: self.events.put(('tray_auto', False))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Exit', lambda icon, item: self.events.put(('tray_exit', None))),
            )
            self.tray_icon = pystray.Icon(APP_ID, image, APP_NAME, menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
            self.tray_ready = True
        except Exception as exc:
            self.events.put(('error', f'System tray unavailable: {exc}'))
            self.root.deiconify()

    def show_window(self):
        self.root.deiconify()
        self.position_window()
        self.root.lift()
        self.root.focus_set()

    def hide_window(self):
        if self.tray_ready:
            self.root.withdraw()
        else:
            self.root.iconify()

    def drain(self):
        try:
            while True:
                event, data = self.events.get_nowait()
                if event == 'tray_open':
                    self.show_window()
                elif event == 'tray_auto':
                    self.set_auto(data)
                elif event == 'tray_exit':
                    self.exit_app()
                    return
                elif event == 'sensor':
                    xyz, lux = data
                    self.xyz, self.lux = xyz, lux
                    self.lux_label.config(text=f'{lux:.1f} Lux')
                    self.xyz_label.config(text=(f'X: {xyz[0]:.6f}   Y: {xyz[1]:.6f}   Z: {xyz[2]:.6f}' if xyz else 'XYZ: unavailable for this sensor'))
                    self.sensor_status.config(text='Ambient sensor connected')
                    self.log(f'{lux:.1f} Lux' + (f' | XYZ {xyz[0]:.4f}, {xyz[1]:.4f}, {xyz[2]:.4f}' if xyz else ''))
                elif event == 'sensor_error':
                    self.sensor_status.config(text='Sensor error — open Full view for details')
                    self.log('Sensor error: ' + data)
                elif event == 'sensor_stopped':
                    if not self.sensor_active:
                        self.sensor_status.config(text='Sensor stopped')
                elif event == 'monitors':
                    count = data
                    for child in self.monitors_frame.winfo_children():
                        child.destroy()
                    self.monitor_vars.clear()
                    self.monitor_labels.clear()
                    with self.lock:
                        old = self.monitors
                        self.monitors = {i: old.get(i, {'actual': None, 'last_write': 0.0}) for i in range(count)}
                        saved = self.cfg.get('enabled_monitors')
                        if saved is None:
                            saved = [min(self.cfg.get('monitor_index', 0), count - 1)] if count else []
                        saved = [i for i in saved if isinstance(i, int) and 0 <= i < count]
                        self.cfg['enabled_monitors'] = saved
                    for i in range(count):
                        line = ttk.Frame(self.monitors_frame)
                        line.pack(fill='x', pady=3)
                        var = tk.BooleanVar(value=i in saved)
                        self.monitor_vars[i] = var
                        ttk.Checkbutton(line, text=f'Monitor {i + 1}', variable=var,
                                        command=lambda n=i: self.monitor_toggle(n)).pack(side='left')
                        label = ttk.Label(line, text='Reading...', style='Muted.TLabel')
                        label.pack(side='right')
                        self.monitor_labels[i] = label
                    self.manual_monitor['values'] = [f'Monitor {i + 1}' for i in range(count)]
                    if count:
                        self.manual_monitor.current(0)
                    self.ddc_status.config(text=f'{count} DDC/CI monitor(s) detected' if count else 'No DDC/CI monitors found')
                    self.read_monitors()
                    self.wake.set()
                elif event == 'read':
                    i, value = data
                    if i in self.monitor_labels:
                        self.monitor_labels[i].config(text=f'{value}%')
                    with self.lock:
                        if i in self.monitors and self.monitors[i]['actual'] is None:
                            self.monitors[i]['actual'] = value
                elif event == 'auto_set':
                    i, value = data
                    if i in self.monitor_labels:
                        self.monitor_labels[i].config(text=f'{value}%')
                elif event == 'monitor_error':
                    i, error = data
                    if i in self.monitor_labels:
                        self.monitor_labels[i].config(text='DDC error')
                    self.log(f'Monitor {i + 1}: {error}')
                elif event == 'auto':
                    self.auto_var.set(data)
                    self.log('Automatic brightness ' + ('ON' if data else 'paused'))
                elif event == 'error':
                    self.ddc_status.config(text=data)
                    self.log(data)
        except queue.Empty:
            pass
        if not self.shutdown.is_set():
            self.root.after(100, self.drain)

    def exit_app(self):
        self.shutdown.set()
        self.sensor_stop.set()
        with self.lock:
            self.sensor_active = False
            self.auto = False
        self.wake.set()
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        self.root.destroy()


def acquire_single_instance():
    if os.name != 'nt':
        return None
    k = ctypes.windll.kernel32
    handle = k.CreateMutexW(None, False, 'Local\\Spyder5AmbientBrightnessController')
    if handle and k.GetLastError() == 183:
        k.CloseHandle(handle)
        return False
    return handle


def main():
    mutex = acquire_single_instance()
    if mutex is False:
        return
    root = tk.Tk()
    App(root, background='--background' in sys.argv)
    root.mainloop()
    if mutex and os.name == 'nt':
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    main()
