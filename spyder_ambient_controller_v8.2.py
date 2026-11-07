"""Spyder ambient brightness controller v8 with direct Windows Night light - Windows, Python 3.10+.
Install: py -3 -m pip install monitorcontrol pystray pillow
Night light uses the tested direct registry backend; no Cargo or wnl.exe.
Format reference: https://github.com/kvnxiao/win-nightlight-cli/blob/main/docs/nightlight-registry-format.md
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
    'night_follow': False, 'night_min_kelvin': 2700, 'night_max_kelvin': 6500,
}
DDC_INTERVAL = 0.14
BG, PANEL, ENTRY, FG, MUTED, ACCENT, LINE = ('#15191f', '#202630', '#2a3340', '#f1f5f9', '#a4b2c3', '#F59E0B', '#394454')


MAGIC = b'CB\x01\x00'
BASE = r'Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current'
NAMES = {'settings': 'windows.data.bluelightreduction.settings',
         'state': 'windows.data.bluelightreduction.bluelightreductionstate'}


def varint(number):
    if number < 0:
        raise ValueError('Negative unsigned integer')
    out = bytearray()
    while number >= 128:
        out.append((number & 127) | 128)
        number >>= 7
    out.append(number)
    return bytes(out)


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError('Truncated registry data')
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def uint(self):
        result = 0
        for shift in range(0, 70, 7):
            b = self.take(1)[0]
            result |= (b & 127) << shift
            if b < 128:
                return result
        raise ValueError('Invalid varint')

    def struct(self, depth=0):
        if depth > 12:
            raise ValueError('Unexpected nesting')
        fields = {}
        while True:
            header = self.take(1)[0]
            kind, field = header & 31, header >> 5
            if kind == 0:
                if header != 0:
                    raise ValueError('Invalid struct terminator')
                return fields
            if field == 6:
                field = self.take(1)[0]
            elif field == 7:
                field = int.from_bytes(self.take(2), 'little')
            if field in fields:
                raise ValueError('Duplicate field')
            start = self.pos
            if kind in (2, 3, 14):
                self.take(1)
            elif kind in (4, 5, 6, 15, 16, 17):
                self.uint()
            elif kind in (7, 8):
                self.take(4 if kind == 7 else 8)
            elif kind == 10:
                self.struct(depth + 1)
            elif kind == 11:
                if self.take(1)[0] != 14:
                    raise ValueError('Unsupported list element type; no changes made')
                self.take(self.uint())
            else:
                raise ValueError(f'Unsupported Bond type {kind}; no changes made')
            fields[field] = (kind, self.data[start:self.pos])


def parse_struct(data):
    reader = Reader(data)
    fields = reader.struct()
    if reader.pos != len(data):
        raise ValueError('Unexpected trailing bytes')
    return fields


def encode_struct(fields):
    out = bytearray()
    for field, (kind, raw) in sorted(fields.items()):
        if field < 6:
            out.append((field << 5) | kind)
        elif field <= 255:
            out.extend(bytes([192 | kind, field]))
        else:
            out.append(224 | kind)
            out.extend(field.to_bytes(2, 'little'))
        out.extend(raw)
    return bytes(out) + b'\x00'


def require(fields, field, kind):
    if field not in fields or fields[field][0] != kind:
        raise ValueError(f'Unexpected schema at field {field}; no changes made')
    return fields[field][1]


def uint_value(raw):
    reader = Reader(raw)
    value = reader.uint()
    if reader.pos != len(raw):
        raise ValueError('Unexpected integer data')
    return value


class Record:
    def __init__(self, data):
        # Do not guess at offsets or patch an unknown CloudStore version.
        if not data.startswith(MAGIC):
            raise ValueError('Unrecognized CloudStore header. Use Copy diagnostics and send the report; no changes made.')
        self.outer = parse_struct(data[4:])
        self.container = parse_struct(require(self.outer, 1, 10))
        self.modified = uint_value(require(self.container, 0, 6))
        self.wrapper = parse_struct(require(self.container, 1, 10))
        blob = Reader(require(self.wrapper, 1, 11))
        if blob.take(1) != b'\x0e':
            raise ValueError('Expected byte list')
        payload = blob.take(blob.uint())
        if blob.pos != len(blob.data) or not payload.startswith(MAGIC):
            raise ValueError('Unrecognized inner payload')
        self.fields = parse_struct(payload[4:])

    def encode(self, modified):
        payload = MAGIC + encode_struct(self.fields)
        self.wrapper[1] = (11, b'\x0e' + varint(len(payload)) + payload)
        self.container[0] = (6, varint(modified))
        self.container[1] = (10, encode_struct(self.wrapper))
        self.outer[1] = (10, encode_struct(self.container))
        return MAGIC + encode_struct(self.outer)


def decode_state(settings, state):
    temp_raw = settings.fields.get(40, (15, varint(6500 * 2)))
    if temp_raw[0] != 15:
        raise ValueError('Unexpected temperature field type')
    zigzag = uint_value(temp_raw[1])
    kelvin = (zigzag >> 1) ^ -(zigzag & 1)
    if not 1200 <= kelvin <= 6500:
        raise ValueError(f'Unexpected temperature: {kelvin} K')
    active = 0 in state.fields
    if active and state.fields[0] != (16, b'\x00'):
        raise ValueError('Unexpected active-state field')
    usable = state.fields.get(30, (2, b'\x01'))
    if usable[0] != 2 or usable[1] not in (b'\x00', b'\x01'):
        raise ValueError('Unexpected availability field')
    if usable[1] == b'\x00':
        raise ValueError('Windows reports Night light unavailable on this device')
    return {'active': active, 'kelvin': kelvin}


class Backend:
    def __init__(self):
        self.highest = {'settings': 0, 'state': 0}
        self.original = None
        self.backup_path = None
        self.report = ''

    def read_raw(self, name):
        if os.name != 'nt':
            raise RuntimeError('This test requires Windows 11.')
        import winreg
        path = BASE + '\\default$' + NAMES[name] + '\\' + NAMES[name]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                data, kind = winreg.QueryValueEx(key, 'Data')
            if kind != winreg.REG_BINARY:
                raise ValueError('Expected REG_BINARY')
            return data
        except FileNotFoundError as exc:
            raise RuntimeError('Night light registry entry not found. Open Windows Night light settings once, then retry.') from exc

    def read(self):
        raw = {name: self.read_raw(name) for name in NAMES}
        self.report = '\n'.join(f'{name}: {len(data)} bytes\n{data.hex(" ")}' for name, data in raw.items())
        records = {name: Record(data) for name, data in raw.items()}
        state = decode_state(records['settings'], records['state'])
        return raw, records, state

    def backup(self):
        if self.original is not None:
            return
        raw, _records, state = self.read()
        directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NightLightFormatTest'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'backup-{time.time_ns()}.json'
        path.write_text(json.dumps({'original': state, 'registry_hex': {k: v.hex() for k, v in raw.items()}}, indent=2), encoding='utf-8')
        # A failed backup prevents the write.
        self.original, self.backup_path = state, path

    def write(self, name, update):
        import winreg
        # Reread to preserve schedules and unrelated fields changed by Windows.
        record = Record(self.read_raw(name))
        update(record.fields)
        stamp = max(int(time.time()), record.modified + 2, self.highest[name] + 2)
        data = record.encode(stamp)
        Record(data)  # Validate the complete record before writing.
        path = BASE + '\\default$' + NAMES[name] + '\\' + NAMES[name]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'Data', 0, winreg.REG_BINARY, data)
        self.highest[name] = stamp

    def temperature(self, kelvin):
        kelvin = int(kelvin)
        if not 1200 <= kelvin <= 6500:
            raise ValueError('Temperature must be 1200–6500 K')
        self.backup()
        active = self.read()[2]['active']
        def update(fields):
            fields[40] = (15, varint(kelvin * 2))
            fields[70] = (2, bytes([int(active)]))
        try:
            self.write('settings', update)
            if active:
                time.sleep(0.075)
        finally:
            # Clear temporary preview, including after a partially failed write.
            self.write('settings', lambda fields: fields.pop(70, None))

    def toggle(self, enabled):
        self.backup()
        self.write('settings', lambda fields: fields.pop(70, None))
        def update(fields):
            if enabled:
                fields[0] = (16, b'\x00')
            else:
                fields.pop(0, None)
            fields[10] = (16, b'\x02')  # ZigZag manual cause = 1.
            fields[20] = (6, varint(int((time.time() + 11644473600) * 10000000)))
        self.write('state', update)

    def restore(self):
        if self.original is None:
            raise ValueError('No changes have been made in this session.')
        # Restore intended values with fresh timestamps, not stale raw records.
        self.temperature(self.original['kelvin'])
        self.toggle(self.original['active'])


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
    for key in ('night_min_kelvin', 'night_max_kelvin'):
        cfg[key] = int(cfg[key])
        if not 1200 <= cfg[key] <= 6500:
            raise ValueError(f'{key} must be between 1200 and 6500 K.')
    if cfg['night_min_kelvin'] > cfg['night_max_kelvin']:
        raise ValueError('Minimum-brightness color temperature cannot exceed maximum-brightness temperature.')
    cfg['night_follow'] = bool(cfg.get('night_follow', False))
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
        self.monitor_sliders = {}
        self.monitor_slider_values = {}
        self.monitor_slider_widgets = {}
        self.night_backend = Backend()
        self.night_busy = False
        self.night_ready = False
        self.night_state = None
        self.night_lock = threading.Lock()
        self.night_follow_last = 0.0
        self.night_follow_applied = None
        self.night_follow_inflight = False
        self.last_log = ""
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
        self.root.after(350, self.refresh_nightlight)
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
        s.configure('Preset.TButton', padding=(3, 5), font=('Segoe UI', 9))
        s.configure('Dark.Horizontal.TScale', background='#384354',
                    troughcolor='#10141a', bordercolor='#10141a',
                    lightcolor='#384354', darkcolor='#384354')
        s.map('Dark.Horizontal.TScale', background=[('active', '#4b5b70')])
        s.configure('Dark.TCombobox', fieldbackground='#384354', background='#384354',
                    foreground=FG, arrowcolor=FG, bordercolor='#10141a',
                    lightcolor='#384354', darkcolor='#384354')
        s.map('Dark.TCombobox',
              fieldbackground=[('readonly', '#384354')],
              background=[('active', '#4b5b70'), ('readonly', '#384354')],
              foreground=[('readonly', FG)],
              selectbackground=[('readonly', '#384354')],
              selectforeground=[('readonly', FG)])
        self.root.option_add('*TCombobox*Listbox.background', '#384354')
        self.root.option_add('*TCombobox*Listbox.foreground', FG)
        self.root.option_add('*TCombobox*Listbox.selectBackground', '#4b5b70')
        self.root.option_add('*TCombobox*Listbox.selectForeground', FG)

    def build_gui(self):
        # Scroll the body when Full view is taller than the desktop work area.
        shell = ttk.Frame(self.root)
        shell.pack(fill='both', expand=True)
        self.body_canvas = tk.Canvas(shell, bg=BG, highlightthickness=0)
        self.body_canvas.pack(side='left', fill='both', expand=True)
        self.outer = ttk.Frame(self.body_canvas, padding=16)
        self.body_window = self.body_canvas.create_window((0, 0), window=self.outer, anchor='nw')
        self.outer.bind('<Configure>', lambda event: self.body_canvas.configure(
            scrollregion=self.body_canvas.bbox('all')))
        self.body_canvas.bind('<Configure>', lambda event: self.body_canvas.itemconfigure(
            self.body_window, width=event.width))
        self.root.bind('<MouseWheel>', self.scroll_body)
        title = ttk.Frame(self.outer)
        title.pack(fill='x')
        ttk.Label(title, text='Spyder Brightness', font=('Segoe UI', 15, 'bold')).pack(side='left')
        self.full_btn = ttk.Button(title, text='Compact view' if self.full else 'Full view', command=self.toggle_full)
        self.full_btn.pack(side='right')
        ttk.Label(self.outer, text='AMBIENT LIGHT', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(16, 0))
        self.lux_label = ttk.Label(self.outer, text='-- Lux', font=('Segoe UI', 30, 'bold'), foreground=ACCENT)
        self.lux_label.pack(anchor='w')
        self.sensor_status = ttk.Label(self.outer, text='Starting sensor...', style='Muted.TLabel', wraplength=345)
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
        self.build_nightlight_follow()
        self.build_nightlight_override()
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
            ('sensor_interval', 'Sensor interval seconds'),
            ('night_min_kelvin', 'Color temp at minimum brightness (K)'),
            ('night_max_kelvin', 'Color temp at maximum brightness (K)'),
            ('spotread', 'ArgyllCMS spotread.exe path'),
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
        ttk.Label(self.advanced, text='WINDOWS & TRAY', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 4))
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(self.advanced, text='Start with Windows sign-in', variable=self.startup_var,
                        command=self.change_startup).pack(anchor='w')
        self.launch_auto_var = tk.BooleanVar(value=self.cfg.get('auto_on_launch', True))
        ttk.Checkbutton(self.advanced, text='Automatically enable brightness on launch', variable=self.launch_auto_var).pack(anchor='w')
        self.minimized_var = tk.BooleanVar(value=self.cfg.get('start_minimized', True))
        ttk.Checkbutton(self.advanced, text='Launch hidden in system tray', variable=self.minimized_var).pack(anchor='w')
        sensor_buttons = ttk.Frame(self.advanced)
        sensor_buttons.pack(fill='x', pady=(8, 3))
        ttk.Button(sensor_buttons, text='Start sensor', command=self.start_sensor).pack(side='left')
        ttk.Button(sensor_buttons, text='Stop sensor', command=self.stop_sensor).pack(side='left', padx=(8, 0))
        ttk.Button(self.advanced, text='Exit completely', command=self.exit_app).pack(anchor='e', pady=4)
        self.update_full()

    def scroll_body(self, event):
        if self.outer.winfo_reqheight() > self.body_canvas.winfo_height():
            self.body_canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')

    def update_full(self):
        if self.full:
            self.advanced.pack(fill='both', expand=True, pady=(6, 0))
        else:
            self.advanced.pack_forget()
        self.full_btn.config(text='Compact view' if self.full else 'Full view')
        self.body_canvas.yview_moveto(0)
        self.root.after_idle(self.position_window)

    def toggle_full(self):
        self.full = not self.full
        self.update_full()

    def position_window(self):
        if self.shutdown.is_set():
            return
        self.root.update_idletasks()
        width = 520 if self.full else 380
        height = min(self.outer.winfo_reqheight() + 8, work_area(self.root)[3] - work_area(self.root)[1] - 25)
        left, top, right, bottom = work_area(self.root)
        x = max(left, right - width - 12)
        y = max(top, bottom - height - 12)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.root.minsize(width, min(height, 280))
        self.root.maxsize(width, height)

    def log(self, text):
        # Retain the latest message internally; no activity log in the GUI.
        self.last_log = text

    def build_nightlight_follow(self):
        ttk.Separator(self.outer).pack(fill='x', pady=(2, 8))
        self.night_follow_var = tk.BooleanVar(value=self.cfg.get('night_follow', False))
        ttk.Checkbutton(self.outer, text='Night light follows brightness',
                        variable=self.night_follow_var, command=self.change_night_follow).pack(anchor='w', pady=(2, 8))

    def change_night_follow(self):
        enabled = bool(self.night_follow_var.get())
        with self.lock:
            self.cfg['night_follow'] = enabled
            self.night_follow_applied = None
            self.night_follow_last = 0.0
        try:
            save_settings(self.cfg)
        except OSError as exc:
            self.events.put(('error', 'Settings save failed: ' + str(exc)))
        if enabled:
            self.wake.set()
        self.update_night_override_state()

    def build_nightlight_override(self):
        ttk.Label(self.outer, text='Windows Night light manual override',
                  style='Muted.TLabel').pack(anchor='w', pady=(10, 4))
        row = ttk.Frame(self.outer)
        row.pack(fill='x')
        self.night_enabled = tk.BooleanVar(value=False)
        self.night_toggle = ttk.Checkbutton(row, text='Windows Night light',
            variable=self.night_enabled, command=self.toggle_nightlight, state='disabled')
        self.night_toggle.pack(side='left')
        self.night_refresh = ttk.Button(row, text='Refresh', command=self.refresh_nightlight)
        self.night_refresh.pack(side='right')
        slider_row = ttk.Frame(self.outer)
        slider_row.pack(fill='x', pady=(5, 0))
        self.night_kelvin = tk.DoubleVar(value=4000)
        self.night_value = ttk.Label(slider_row, text='4000 K', width=8)
        self.night_value.pack(side='right', padx=(8, 0))
        self.night_slider = ttk.Scale(slider_row, from_=1200, to=6500,
            variable=self.night_kelvin, style='Dark.Horizontal.TScale',
            command=lambda value: self.night_value.config(text=f'{round(float(value))} K'),
            state='disabled')
        self.night_slider.pack(side='left', fill='x', expand=True)
        self.night_slider.bind('<ButtonRelease-1>', self.apply_nightlight_temperature)
        self.night_slider.bind('<KeyRelease>', self.nightlight_key)
        presets = ttk.Frame(self.outer)
        presets.pack(fill='x', pady=(5, 2))
        self.night_presets = []
        for kelvin in (2700, 3400, 4000, 5000, 6500):
            button = ttk.Button(presets, text=f'{kelvin} K', width=6, style='Preset.TButton',
                command=lambda k=kelvin: self.nightlight_preset(k), state='disabled')
            button.pack(side='left', fill='x', expand=True, padx=(0, 3))
            self.night_presets.append(button)
        self.night_status = ttk.Label(self.outer, text='Reading Windows Night light…',
                                      style='Muted.TLabel', wraplength=340)
        self.night_status.pack(anchor='w', pady=(2, 8))
        self.update_night_override_state()

    def update_night_override_state(self):
        if not hasattr(self, 'night_toggle'):
            return
        manual = self.night_ready and not self.night_busy and not self.cfg.get('night_follow', False)
        self.night_toggle.configure(state='normal' if self.night_ready and not self.night_busy else 'disabled')
        state = 'normal' if manual else 'disabled'
        self.night_slider.configure(state=state)
        for button in self.night_presets:
            button.configure(state=state)

    def nightlight_preset(self, kelvin):
        if self.night_ready and not self.night_busy and not self.cfg.get('night_follow', False):
            self.night_kelvin.set(kelvin)
            self.night_value.config(text=f'{kelvin} K')
            self.run_nightlight('temperature', kelvin)

    def nightlight_key(self, event):
        if event.keysym in ('Left', 'Right', 'Up', 'Down', 'Home', 'End', 'Prior', 'Next'):
            self.apply_nightlight_temperature()

    def refresh_nightlight(self):
        self.run_nightlight('read')

    def toggle_nightlight(self):
        # The Windows Night light switch always controls the master on/off state.
        enabled = bool(self.night_enabled.get())
        with self.lock:
            if self.night_state is not None:
                self.night_state['active'] = enabled
            self.night_follow_applied = None
        self.run_nightlight('toggle', enabled)

    def apply_nightlight_temperature(self, event=None):
        if self.night_ready and not self.night_busy and not self.cfg.get('night_follow', False):
            self.run_nightlight('temperature', round(self.night_kelvin.get()))

    def run_nightlight(self, operation, value=None):
        if self.night_busy or self.shutdown.is_set() or (operation == 'temperature' and self.cfg.get('night_follow', False)):
            return
        self.night_busy = True
        self.night_toggle.configure(state='disabled')
        self.night_slider.configure(state='disabled')
        self.night_refresh.configure(state='disabled')
        for button in self.night_presets:
            button.configure(state='disabled')
        self.night_status.config(text='Reading Night light…' if operation == 'read' else 'Applying Night light…')
        def worker():
            error = None
            state = None
            try:
                with self.night_lock:
                    if operation == 'toggle':
                        self.night_backend.toggle(value)
                    elif operation == 'temperature':
                        self.night_backend.temperature(value)
                    if operation != 'read':
                        time.sleep(0.15)
                    state = self.night_backend.read()[2]
            except Exception as exc:
                error = str(exc)
                # Recover actual state after a partially completed operation.
                try:
                    state = self.night_backend.read()[2]
                except Exception:
                    pass
            self.events.put(('nightlight', (state, error)))
        threading.Thread(target=worker, daemon=True).start()

    def finish_nightlight(self, result):
        state, error = result
        self.night_busy = False
        self.night_ready = state is not None
        self.night_refresh.configure(state='normal')
        widget_state = 'normal' if self.night_ready else 'disabled'
        self.night_toggle.configure(state=widget_state)
        self.night_slider.configure(state=widget_state)
        for button in self.night_presets:
            button.configure(state=widget_state)
        if state:
            self.night_state = state
            self.night_enabled.set(state['active'])
            self.night_kelvin.set(state['kelvin'])
            self.night_value.config(text=f"{state['kelvin']} K")
        elif self.night_state:
            self.night_enabled.set(self.night_state['active'])
        if error:
            self.night_status.config(text='Night light: ' + error)
        else:
            self.night_status.config(text=('On · release slider to apply' if state['active'] else
                'Off · temperature is saved for next use'))
        self.update_night_override_state()
        self.root.after_idle(self.position_window)

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
        self.update_monitor_slider_states()
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

    def update_monitor_slider_states(self):
        # Automatic control owns sliders only for monitors selected for automation.
        for index, slider in self.monitor_slider_widgets.items():
            managed = self.auto and self.monitor_vars.get(index) is not None and self.monitor_vars[index].get()
            slider.configure(state='disabled' if managed else 'normal')

    def monitor_slider_release(self, index):
        if index not in self.monitor_sliders:
            return
        if self.auto and self.monitor_vars.get(index) is not None and self.monitor_vars[index].get():
            return
        value = max(0, min(100, round(self.monitor_sliders[index].get())))
        self.monitor_slider_values[index].configure(text=f'{value}%')
        def worker():
            try:
                applied = self.get_monitor(index, value)
                self.events.put(('manual_set', (index, applied)))
            except Exception as exc:
                self.events.put(('monitor_error', (index, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def monitor_slider_preview(self, index):
        if index in self.monitor_slider_values:
            self.monitor_slider_values[index].configure(text=f'{round(self.monitor_sliders[index].get())}%')

    def update_monitor_slider(self, index, value):
        if index in self.monitor_sliders:
            self.monitor_sliders[index].set(value)
        if index in self.monitor_slider_values:
            self.monitor_slider_values[index].configure(text=f'{value}%')

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
            self.update_follow_nightlight()
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

    def update_follow_nightlight(self):
        with self.lock:
            if not (self.cfg.get('night_follow') and self.auto and self.sensor_active
                    and self.night_ready and self.night_state and self.night_state.get('active')
                    and not self.night_busy):
                return
            if self.night_follow_inflight or time.monotonic() - self.night_follow_last < 1.2:
                return
            values = [self.monitors[i]['actual'] for i in self.cfg.get('enabled_monitors', [])
                      if i in self.monitors and self.monitors[i]['actual'] is not None]
            if not values:
                return
            # Windows Night light is system-wide: use mean brightness of selected monitors.
            brightness = sum(values) / len(values)
            lo = self.cfg['min_brightness']
            hi = self.cfg['max_brightness']
            fraction = max(0.0, min(1.0, (brightness - lo) / max(1, hi - lo)))
            kelvin = round(self.cfg['night_min_kelvin'] + fraction *
                           (self.cfg['night_max_kelvin'] - self.cfg['night_min_kelvin']))
            # Quantize to avoid excessive Windows CloudStore registry writes.
            kelvin = max(1200, min(6500, round(kelvin / 100) * 100))
            if self.night_follow_applied is not None and abs(kelvin - self.night_follow_applied) < 100:
                return
            self.night_follow_inflight = True
            self.night_follow_last = time.monotonic()
        def worker():
            error = None
            try:
                with self.night_lock:
                    state = self.night_backend.read()[2]
                    # Follow mode adjusts temperature only; never overrides the master switch.
                    if state['active']:
                        self.night_backend.temperature(kelvin)
            except Exception as exc:
                error = str(exc)
            self.events.put(('night_follow_result', (kelvin, error)))
        threading.Thread(target=worker, daemon=True).start()

    def apply_settings(self):
        try:
            cfg = dict(self.cfg)
            for key, var in self.vars.items():
                cfg[key] = var.get()
            cfg['auto_on_launch'] = self.launch_auto_var.get()
            cfg['start_minimized'] = self.minimized_var.get()
            cfg['full_view'] = self.full
            cfg['night_follow'] = bool(self.night_follow_var.get())
            cfg = validate(cfg)
            # Save a plain snapshot: avoid a concurrent toggle modifying the config
            # while it is being serialized.
            save_settings(dict(cfg))
            with self.lock:
                self.cfg = cfg
                self.target = None
                self.smooth = None
            self.night_follow_applied = None
            self.wake.set()
            self.update_night_override_state()
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
            from pathlib import Path
            import sys
            from PIL import Image
            
            base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            image = Image.open(base_path / "tray_icon2.png").convert("RGBA")
            image = image.resize((64, 64), Image.Resampling.LANCZOS)
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
        self.root.update_idletasks()
        self.root.lift()
        # A temporary topmost pulse also raises an already-open, obscured window.
        # Release it shortly afterward so the controller is not always on top.
        self.root.attributes('-topmost', True)
        self.root.focus_force()
        if os.name == 'nt':
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
            user32.GetAncestor.restype = wintypes.HWND
            user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
            user32.SetForegroundWindow.restype = wintypes.BOOL
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
            if hwnd:
                user32.SetForegroundWindow(hwnd)
        pending = getattr(self, '_foreground_release', None)
        if pending is not None:
            self.root.after_cancel(pending)
        self._foreground_release = self.root.after(200, self.release_foreground)
        self.refresh_nightlight()

    def release_foreground(self):
        self._foreground_release = None
        if not self.shutdown.is_set():
            self.root.attributes('-topmost', False)

    def hide_window(self):
        if self.tray_ready:
            self.root.withdraw()
        else:
            self.root.iconify()

    def drain(self):
        try:
            while True:
                event, data = self.events.get_nowait()
                if event == 'night_follow_result':
                    kelvin, error = data
                    with self.lock:
                        self.night_follow_inflight = False
                        if error is None:
                            self.night_follow_applied = kelvin
                    if error:
                        self.night_status.config(text='Automatic Night light: ' + error)
                    elif self.cfg.get('night_follow') and self.night_state and self.night_state.get('active'):
                        self.night_kelvin.set(kelvin)
                        self.night_value.config(text=f'{kelvin} K')
                        self.night_status.config(text=f'Following monitor brightness · {kelvin} K')
                elif event == 'nightlight':
                    self.finish_nightlight(data)
                elif event == 'tray_open':
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
                    self.sensor_status.config(text='Ambient sensor connected')
                    self.log(f'{lux:.1f} Lux' + (f' | XYZ {xyz[0]:.4f}, {xyz[1]:.4f}, {xyz[2]:.4f}' if xyz else ''))
                elif event == 'sensor_error':
                    self.sensor_status.config(text='Sensor error: ' + data)
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
                    self.monitor_sliders.clear()
                    self.monitor_slider_values.clear()
                    self.monitor_slider_widgets.clear()
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
                        line.pack(fill='x', pady=(4, 7))
                        var = tk.BooleanVar(value=i in saved)
                        self.monitor_vars[i] = var
                        ttk.Checkbutton(line, text=f'Monitor {i + 1}', variable=var,
                                        command=lambda n=i: self.monitor_toggle(n)).pack(side='left')
                        value_label = ttk.Label(line, text='--%', width=5, anchor='e', style='Muted.TLabel')
                        value_label.pack(side='right')
                        self.monitor_slider_values[i] = value_label
                        slider_var = tk.DoubleVar(value=50)
                        self.monitor_sliders[i] = slider_var
                        slider = ttk.Scale(line, from_=0, to=100, variable=slider_var,
                                           style='Dark.Horizontal.TScale',
                                           command=lambda _value, n=i: self.monitor_slider_preview(n))
                        slider.pack(side='left', fill='x', expand=True, padx=(8, 5))
                        slider.bind('<ButtonRelease-1>', lambda event, n=i: self.monitor_slider_release(n))
                        slider.bind('<KeyRelease>', lambda event, n=i: self.monitor_slider_release(n))
                        self.monitor_slider_widgets[i] = slider
                        self.monitor_labels[i] = value_label
                    self.update_monitor_slider_states()
                    self.ddc_status.config(text=f'{count} DDC/CI monitor(s) detected' if count else 'No DDC/CI monitors found')
                    self.read_monitors()
                    self.wake.set()
                elif event == 'read':
                    i, value = data
                    if i in self.monitor_labels:
                        self.update_monitor_slider(i, value)
                    with self.lock:
                        if i in self.monitors and self.monitors[i]['actual'] is None:
                            self.monitors[i]['actual'] = value
                elif event == 'manual_set':
                    i, value = data
                    self.update_monitor_slider(i, value)
                    with self.lock:
                        if i in self.monitors:
                            self.monitors[i]['actual'] = value
                            self.monitors[i]['last_write'] = time.monotonic()
                elif event == 'auto_set':
                    i, value = data
                    if i in self.monitor_labels:
                        self.update_monitor_slider(i, value)
                elif event == 'monitor_error':
                    i, error = data
                    if i in self.monitor_labels:
                        self.monitor_labels[i].config(text='DDC error')
                    self.ddc_status.config(text=f'Monitor {i + 1}: {error}')
                    self.log(f'Monitor {i + 1}: {error}')
                elif event == 'auto':
                    self.auto_var.set(data)
                    self.update_monitor_slider_states()
                    self.log('Automatic brightness ' + ('ON' if data else 'paused'))
                elif event == 'error':
                    self.ddc_status.config(text=data)
                    self.log(data)
        except queue.Empty:
            pass
        if not self.shutdown.is_set():
            self.root.after(100, self.drain)

    def exit_app(self):
        # Let a pending registry preview finish and clean up before exiting.
        if self.night_busy:
            self.root.after(150, self.exit_app)
            return
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
