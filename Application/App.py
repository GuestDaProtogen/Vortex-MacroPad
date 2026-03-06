import sys
import time
import threading
import multiprocessing
import asyncio
import re
import warnings
import os

# --- Libraries ---
import serial
import serial.tools.list_ports
from PIL import Image
import mss
import numpy as np
import customtkinter as ctk
import pystray

# --- OPTIONAL: Hide discontinuity warnings (should not appear now) ---
warnings.filterwarnings("ignore", message="data discontinuity")

# --- MONKEY PATCH ---
np.fromstring = np.frombuffer

# --- MACROPAD KEY DICTIONARY ---
KEY_MAPPING = {
    "0": 0x27, "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21,
    "5": 0x22, "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26,
    "A": 0x04, "B": 0x05, "C": 0x06, "D": 0x07, "E": 0x08,
    "F": 0x09, "G": 0x0A, "H": 0x0B, "I": 0x0C, "J": 0x0D,
    "K": 0x0E, "L": 0x0F, "M": 0x10, "N": 0x11, "O": 0x12,
    "P": 0x13, "Q": 0x14, "R": 0x15, "S": 0x16, "T": 0x17,
    "U": 0x18, "V": 0x19, "W": 0x1A, "X": 0x1B, "Y": 0x1C, "Z": 0x1D,
    "PLAY": 0xF0, "NEXT": 0xF1, "PREV": 0xF2,
    "VOL+": 0xF3, "VOL-": 0xF4, "MUTE": 0xF5,
    ";": 0x33, "=": 0x2E, ",": 0x36, "-": 0x2D,
    ".": 0x37, "/": 0x38, "`": 0x35, "[": 0x2F,
    "]": 0x30, "'": 0x34, "\\": 0x31,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D,
    "F5": 0x3E, "F6": 0x3F, "F7": 0x40, "F8": 0x41,
    "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "ESC": 0x29, "TAB": 0x2B, "ENT": 0x28, "SPC": 0x2C,
    "BSP": 0x2A, "DEL": 0x4C, 
    "UP": 0x52, "DN": 0x51, "LF": 0x50, "RT": 0x4F,
    "CAPS": 0x39, "PRT": 0x46,
    "LCTL": 0xE0, "LSFT": 0xE1, "LALT": 0xE2, "LGUI": 0xE3
}

# --- CONFIG ---
WIDTH, HEIGHT = 128, 64 
BAUD_RATE = 115200 
START_BYTE = b'\x02'
ACK_BYTE = b'\x06'
UPDATE_INTERVAL = 0.04
SAMPLE_RATE = 48000      # Match Windows default
BUFFER_SIZE = 1024       # Low latency (~21ms)

# ============================================================================
# PC METRICS WORKERS
# ============================================================================
def pick_sc_loopback_once():
    import soundcard as sc
    try:
        spk = sc.default_speaker()
        if not spk:
            return None

        def guid_tail(dev_id: str) -> str:
            if not dev_id:
                return ""
            m = re.search(r"\}\.\{([0-9a-fA-F\-]+)\}", dev_id)
            return m.group(1).lower() if m else dev_id.lower()

        spk_guid = guid_tail(getattr(spk, "id", "") or "")

        candidates = sc.all_microphones(include_loopback=True)
        loopbacks = [m for m in candidates if getattr(m, "isloopback", False)]

        exact = [m for m in loopbacks if guid_tail(getattr(m, "id", "") or "") == spk_guid]
        if exact:
            return exact[0]

        name_match = [m for m in loopbacks if spk.name.lower() in (m.name or "").lower()]
        if name_match:
            return name_match[0]

        return loopbacks[0] if loopbacks else None
    except:
        return None

def _volume_worker(out_q, stop_event):
    import pythoncom
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import POINTER, cast

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    last_pct = -1

    while not stop_event.is_set():
        try:
            device_enumerator = AudioUtilities.GetDeviceEnumerator()
            default_device = device_enumerator.GetDefaultAudioEndpoint(0, 1)
            interface = default_device.Activate(
                IAudioEndpointVolume._iid_,
                CLSCTX_ALL,
                None
            )
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            scalar = volume.GetMasterVolumeLevelScalar()
            pct = int(round(scalar * 100))

            if pct != last_pct:
                out_q.put(pct)
                last_pct = pct
        except:
            pass
        time.sleep(0.2)

def audio_capture_thread(levels, lock, stop_event, rebind_event):
    import soundcard as sc
    while not stop_event.is_set():
        try:
            mic = pick_sc_loopback_once()
            if not mic:
                time.sleep(1)
                continue

            with mic.recorder(samplerate=SAMPLE_RATE) as recorder:
                while not stop_event.is_set():
                    if rebind_event.is_set():
                        rebind_event.clear()
                        break
                    data = recorder.record(numframes=BUFFER_SIZE)
                    if data.size > 0:
                        l_peak = np.max(np.abs(data[:, 0])) if data.shape[1] > 0 else 0
                        r_peak = np.max(np.abs(data[:, 1])) if data.shape[1] > 1 else 0

                        l_val = int(np.clip(l_peak * 18.0, 0, 8))
                        r_val = int(np.clip(r_peak * 18.0, 0, 8))

                        with lock:
                            levels['left'] = l_val
                            levels['right'] = r_val
        except Exception as e:
            with lock:
                levels['left'] = 0
                levels['right'] = 0
            time.sleep(1)

_media_session = None
_media_last_refresh = 0
_last_timeline = {
    "base_position": 0, "base_time": 0, "duration": 0, "playing": False,
    "title": "No Media", "artist": "System Idle"
}

async def get_media_info():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SM
    global _media_session, _media_last_refresh, _last_timeline
    now = time.time()
    try:
        if _media_session is None or now - _media_last_refresh > 3:
            sessions = await SM.request_async()
            _media_session = sessions.get_current_session()
            _media_last_refresh = now

        if _media_session:
            simulated = _last_timeline["base_position"]
            if _last_timeline["playing"]:
                simulated += int(now - _last_timeline["base_time"])

            if now - _last_timeline["base_time"] > 3:
                props = await _media_session.try_get_media_properties_async()
                timeline = _media_session.get_timeline_properties()
                playback = _media_session.get_playback_info()

                _last_timeline["title"] = props.title or "No Title"
                _last_timeline["artist"] = props.artist or "No Artist"

                if timeline and timeline.position:
                    real_position = int(timeline.position.total_seconds())
                    if abs(real_position - simulated) > 2:
                        _last_timeline["base_position"] = real_position
                        _last_timeline["base_time"] = now
                    else:
                        _last_timeline["base_position"] = simulated
                        _last_timeline["base_time"] = now

                if timeline and timeline.max_seek_time:
                    _last_timeline["duration"] = int(timeline.max_seek_time.total_seconds())

                _last_timeline["playing"] = (playback.playback_status == 4)

        elapsed = _last_timeline["base_position"]
        if _last_timeline["playing"]:
            elapsed += int(now - _last_timeline["base_time"])
        total = _last_timeline["duration"]

        return (
            _last_timeline["title"], _last_timeline["artist"],
            f"{elapsed // 60}:{elapsed % 60:02d}", f"{total // 60}:{total % 60:02d}"
        )
    except:
        pass
    return "No Media", "System Idle", "0:00", "0:00"

async def metrics_loop(ser_port_name, stop_event, rebind_event, cmd_queue, log_callback):
    levels = {'left': 0, 'right': 0}
    levels_lock = threading.Lock()
    vol_queue = multiprocessing.Queue()
    
    vol_thread_stop = threading.Event()
    vol_thread = threading.Thread(target=_volume_worker, args=(vol_queue, vol_thread_stop), daemon=True)
    vol_thread.start()

    audio_thread_stop = threading.Event()
    audio_thread = threading.Thread(target=audio_capture_thread, args=(levels, levels_lock, audio_thread_stop, rebind_event), daemon=True)
    audio_thread.start()

    log_callback("Starting PC Metrics Loop...")
    try:
        ser = serial.Serial(ser_port_name, BAUD_RATE, timeout=0.1)
        ser.reset_input_buffer()
    except Exception as e:
        log_callback(f"Failed to open port: {e}")
        vol_thread_stop.set()
        audio_thread_stop.set()
        return

    current_vol = 50
    try:
        while not stop_event.is_set():
            # Send any pending commands
            while not cmd_queue.empty():
                cmd = cmd_queue.get()
                ser.write(cmd.encode("utf-8"))
        
            while not vol_queue.empty():
                current_vol = vol_queue.get()

            title, artist, elapsed, total = await get_media_info()

            with levels_lock:
                l = levels['left']
                r = levels['right']

            payload = f"MET:{title}|{artist}|{l}|{r}|{current_vol}|{elapsed}|{total}\n"
            ser.write(payload.encode("utf-8"))

            await asyncio.sleep(UPDATE_INTERVAL)
    except Exception as e:
        log_callback(f"Metrics Error: {e}")
    finally:
        ser.close()
        vol_thread_stop.set()
        audio_thread_stop.set()
        log_callback("PC Metrics Loop Stopped.")

# ============================================================================
# MIRRORING WORKER
# ============================================================================
def mirroring_worker(ser_port_name, stop_event, log_callback):
    log_callback("Starting Screen Mirroring Loop...")
    try:
        ser = serial.Serial(ser_port_name, BAUD_RATE, timeout=0.1)
        ser.reset_input_buffer()
        with mss.mss() as sct:
            monitor = sct.monitors[1] 
            
            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            img = img.resize((WIDTH, HEIGHT), Image.NEAREST).convert("1")
            ser.write(START_BYTE + img.tobytes())

            while not stop_event.is_set():
                if ser.read(1) == ACK_BYTE:
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    img = img.resize((WIDTH, HEIGHT), Image.NEAREST).convert("1")
                    ser.write(START_BYTE + img.tobytes())
                else:
                    time.sleep(0.001)
                    
    except Exception as e:
        log_callback(f"Mirroring Error: {e}")
    finally:
        if 'ser' in locals():
            ser.close()
        log_callback("Screen Mirroring Loop Stopped.")


# ============================================================================
# APP GUI
# ============================================================================
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class VortexApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Vortex Macro Pad")
        self.geometry("850x600")
        self.minsize(700, 500)
        
        # System Tray Integration
        icon_path = r"d:\Data\Github\Vortex-MacroPad\Application\Assets\icon.ico"
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)
            self.tray_icon_img = Image.open(icon_path)
        else:
            self.tray_icon_img = Image.new('RGB', (64, 64), color=(0,0,0))
            
        self.protocol("WM_DELETE_WINDOW", self.quit_window)
        
        self.active_mode = "Metrics"
        self.is_running = False
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.rebind_event = threading.Event()
        import queue
        self.cmd_queue = queue.Queue()
        
        self.async_loop = None

        # Setup persistent tray icon
        self.setup_tray()

        # Grid config
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Base Layout
        self.sidebar_frame = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(5, weight=1)

        self.main_frame = ctk.CTkFrame(self, corner_radius=10)
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.main_frame.grid_rowconfigure(2, weight=1) # Log box area
        self.main_frame.grid_columnconfigure(0, weight=1)

        # -- Sidebar Logo and Title --
        self.logo_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.logo_frame.grid(row=0, column=0, padx=10, pady=(20, 10), sticky="ew")
        
        logo_path = r"d:\Data\Github\Vortex-MacroPad\Application\Assets\vortex logo png.png"
        if os.path.exists(logo_path):
            img = Image.open(logo_path)
            self.logo_img = ctk.CTkImage(light_image=img, dark_image=img, size=(40, 40))
            self.logo_icon = ctk.CTkLabel(self.logo_frame, image=self.logo_img, text="")
            self.logo_icon.pack(side="left", padx=(0, 10))
            
        self.title_inner = ctk.CTkFrame(self.logo_frame, fg_color="transparent")
        self.title_inner.pack(side="left")
        self.title_vortex = ctk.CTkLabel(self.title_inner, text="VORTEX", font=("Audiowide", 20, "bold"), text_color="white")
        self.title_vortex.pack(anchor="w")
        self.title_mp = ctk.CTkLabel(self.title_inner, text="Macro Pad", font=("Noto Sans", 12, "bold"))
        self.title_mp.pack(anchor="w", pady=(0,0))

        self.mode_label = ctk.CTkLabel(self.sidebar_frame, text="Operation Mode:", anchor="w")
        self.mode_label.grid(row=1, column=0, padx=20, pady=(10, 0))

        self.mode_var = ctk.StringVar(value="Metrics")
        self.rad_metrics = ctk.CTkRadioButton(self.sidebar_frame, text="Media Control", variable=self.mode_var, value="Metrics", command=self.on_mode_change)
        self.rad_metrics.grid(row=2, column=0, pady=10, padx=20, sticky="w")
        
        self.rad_mirror = ctk.CTkRadioButton(self.sidebar_frame, text="Screen Mirror", variable=self.mode_var, value="Mirror", command=self.on_mode_change)
        self.rad_mirror.grid(row=3, column=0, pady=10, padx=20, sticky="w")
        
        # Audio Rebind Button
        self.rebind_btn = ctk.CTkButton(self.sidebar_frame, text="Rebind Audio", command=self.trigger_rebind)
        self.rebind_btn.grid(row=4, column=0, pady=20, padx=20, sticky="ew")

        # -- Main Area --
        
        # Connection
        self.conn_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.conn_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 10))
        self.conn_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.conn_frame, text="Select COM Port:").grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.port_combobox = ctk.CTkComboBox(self.conn_frame, values=["Checking ports..."])
        self.port_combobox.grid(row=0, column=1, padx=10, sticky="ew")

        self.refresh_btn = ctk.CTkButton(self.conn_frame, text="Refresh", width=80, command=self.refresh_ports)
        self.refresh_btn.grid(row=0, column=2, padx=(10, 0))

        # Device Config Settings
        self.cfg_frame = ctk.CTkFrame(self.main_frame)
        self.cfg_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=10)
        self.cfg_frame.grid_columnconfigure((0,1,2,3), weight=1)
        
        ctk.CTkLabel(self.cfg_frame, text="Device Configuration", font=("", 14, "bold")).grid(row=0, column=0, columnspan=4, pady=5)
        
        # RGB Mode
        ctk.CTkLabel(self.cfg_frame, text="RGB Mode:").grid(row=1, column=0, padx=5, sticky="e")
        self.rgb_mode_var = ctk.StringVar(value="0") # 0=Cycle, 1=Custom, 2=Off
        self.rgb_mode_cb = ctk.CTkComboBox(self.cfg_frame, values=["Cycle", "Custom", "Off"], command=self.send_config)
        self.rgb_mode_cb.set("Cycle")
        self.rgb_mode_cb.grid(row=1, column=1, padx=5, sticky="w")
        
        # Brightness
        ctk.CTkLabel(self.cfg_frame, text="Brightness:").grid(row=1, column=2, padx=5, sticky="e")
        self.bright_slider = ctk.CTkSlider(self.cfg_frame, from_=5, to=255, command=self.send_config)
        self.bright_slider.set(150)
        self.bright_slider.grid(row=1, column=3, padx=5, sticky="w")
        
        # RGB Color
        ctk.CTkLabel(self.cfg_frame, text="Custom RGB (R,G,B):").grid(row=2, column=0, padx=5, sticky="e")
        self.col_frame = ctk.CTkFrame(self.cfg_frame, fg_color="transparent")
        self.col_frame.grid(row=2, column=1, padx=5, sticky="w")
        self.r_var = ctk.StringVar(value="0")
        self.g_var = ctk.StringVar(value="255")
        self.b_var = ctk.StringVar(value="255")
        ctk.CTkEntry(self.col_frame, textvariable=self.r_var, width=40).pack(side="left", padx=2)
        ctk.CTkEntry(self.col_frame, textvariable=self.g_var, width=40).pack(side="left", padx=2)
        ctk.CTkEntry(self.col_frame, textvariable=self.b_var, width=40).pack(side="left", padx=2)
        ctk.CTkButton(self.cfg_frame, text="Apply Color", width=80, command=self.send_config).grid(row=2, column=2, padx=5, sticky="w")
        
        # App Mode
        ctk.CTkLabel(self.cfg_frame, text="Device Mode:").grid(row=3, column=0, padx=5, sticky="e")
        self.dev_mode_cb = ctk.CTkComboBox(self.cfg_frame, values=["Rhythm", "Media"], command=self.send_config)
        self.dev_mode_cb.set("Media")
        self.dev_mode_cb.grid(row=3, column=1, padx=5, sticky="w")
        
        # Keys config (Dropdown mapped using KEY_MAPPING)
        ctk.CTkLabel(self.cfg_frame, text="Button Mapping:").grid(row=4, column=0, padx=5, sticky="e")
        self.k_frame = ctk.CTkFrame(self.cfg_frame, fg_color="transparent")
        self.k_frame.grid(row=4, column=1, columnspan=3, padx=5, sticky="w")
        
        self.k1_var = ctk.StringVar(value="A")
        self.k2_var = ctk.StringVar(value="S")
        self.k3_var = ctk.StringVar(value="L")
        self.k4_var = ctk.StringVar(value=";")
        
        keys_list = list(KEY_MAPPING.keys())
        
        for i, var in enumerate([self.k1_var, self.k2_var, self.k3_var, self.k4_var]):
            ctk.CTkLabel(self.k_frame, text=f"K{i+1}:").pack(side="left")
            w = ctk.CTkComboBox(self.k_frame, values=keys_list, variable=var, width=70, command=self.send_config)
            w.pack(side="left", padx=(0,10))
            
        self.sync_btn = ctk.CTkButton(self.k_frame, text="Apply All Configs", command=self.send_config)
        self.sync_btn.pack(side="left", padx=10)

        # Log Output
        self.log_box = ctk.CTkTextbox(self.main_frame, state="disabled")
        self.log_box.grid(row=2, column=0, sticky="nsew", padx=20, pady=(10, 20))

        # Control Panel
        self.ctrl_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.ctrl_frame.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 20))
        self.ctrl_frame.grid_columnconfigure((0, 1), weight=1)

        self.start_btn = ctk.CTkButton(self.ctrl_frame, text="Start Application", height=40, font=("", 14, "bold"), command=self.toggle_execution)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        
        self.min_btn = ctk.CTkButton(self.ctrl_frame, text="Minimize to Tray", height=40, command=self.hide_window)
        self.min_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        # Initialization
        self.log("App initialized. Welcome to Vortex Control Hub.")
        self.refresh_ports()
        
        self.after(500, self.auto_start)

    def auto_start(self):
        port_selection = self.port_combobox.get()
        if port_selection and port_selection != "No ports found":
            self.toggle_execution()
        
    def setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem('Restore', self.show_window, default=True),
            pystray.MenuItem('Quit', self.quit_window)
        )
        self.tray_icon = pystray.Icon("Vortex", self.tray_icon_img, "Vortex Macro Pad", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide_window(self):
        self.withdraw()

    def show_window(self, icon=None, item=None):
        self.after(0, self.deiconify)

    def quit_window(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
        self.stop_event.set()
        self.after(0, self.destroy)

    def send_config(self, *args):
        if not self.is_running:
            return
            
        try:
            am = 0 if self.dev_mode_cb.get() == "Rhythm" else 1
            rm_str = self.rgb_mode_cb.get()
            rm = 0
            if rm_str == "Custom": rm = 1
            elif rm_str == "Off": rm = 2
            
            r = int(self.r_var.get())
            g = int(self.g_var.get())
            b = int(self.b_var.get())
            br = int(self.bright_slider.get())
            
            k1 = KEY_MAPPING.get(self.k1_var.get(), 0x04)
            k2 = KEY_MAPPING.get(self.k2_var.get(), 0x16)
            k3 = KEY_MAPPING.get(self.k3_var.get(), 0x0F)
            k4 = KEY_MAPPING.get(self.k4_var.get(), 0x33)
            
            cfg_str = f"CFG:{am}|{k1}|{k2}|{k3}|{k4}|{rm}|{r}|{g}|{b}|{br}\n"
            self.cmd_queue.put(cfg_str)
            self.log("Scheduled config update.")
        except Exception as e:
            self.log(f"Config Error: Invalid values. {e}")

    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def trigger_rebind(self):
        self.rebind_event.set()
        self.log("Rebinding audio input...")

    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        devices = []
        for p in ports:
            devices.append(f"{p.device} - {p.description}")
        
        if devices:
            self.port_combobox.configure(values=devices)
            self.port_combobox.set(devices[0])
            self.log(f"Discovered {len(devices)} COM ports.")
        else:
            self.port_combobox.configure(values=["No ports found"])
            self.port_combobox.set("No ports found")
            self.log("No COM ports found.")

    def on_mode_change(self):
        if self.is_running:
            self.log("Warning: Stop active process to apply mode changes.")

    def run_metrics_in_thread(self, port_name):
        self.async_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.async_loop)
        self.async_loop.run_until_complete(metrics_loop(port_name, self.stop_event, self.rebind_event, self.cmd_queue, self.log))

    def toggle_execution(self):
        if self.is_running:
            # STOP Process
            self.log("Stopping...")
            self.stop_event.set()
            if self.worker_thread and self.worker_thread.is_alive():
                self.after(500, self.check_stopped)
            else:
                self.finish_stop()
        else:
            # START Process
            port_selection = self.port_combobox.get()
            if not port_selection or port_selection == "No ports found":
                self.log("Cannot start. No valid COM port selected.")
                return
            
            port = port_selection.split(" - ")[0]
            
            self.active_mode = self.mode_var.get()
            self.log(f"Starting {self.active_mode} mode on {port}...")
            
            self.is_running = True
            self.stop_event.clear()
            self.rebind_event.clear()
            while not self.cmd_queue.empty(): self.cmd_queue.get()
            
            self.start_btn.configure(text="Stop Application", fg_color="red", hover_color="#8B0000")
            self.rad_metrics.configure(state="disabled")
            self.rad_mirror.configure(state="disabled")

            if self.active_mode == "Metrics":
                self.worker_thread = threading.Thread(target=self.run_metrics_in_thread, args=(port,), daemon=True)
                self.worker_thread.start()
            else:
                self.worker_thread = threading.Thread(target=mirroring_worker, args=(port, self.stop_event, self.log), daemon=True)
                self.worker_thread.start()
                
            # Send config immediately upon start
            self.after(1000, self.send_config)

    def check_stopped(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(500, self.check_stopped)
        else:
            self.finish_stop()

    def finish_stop(self):
        self.is_running = False
        self.log(f"{self.active_mode} mode stopped.")
        self.start_btn.configure(text="Start Application", fg_color=['#3a7ebf', '#1f538d'], hover_color=['#325882', '#14375e'])
        self.rad_metrics.configure(state="normal")
        self.rad_mirror.configure(state="normal")
        
    def on_closing(self):
        self.stop_event.set()
        self.destroy()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = VortexApp()
    app.mainloop()
