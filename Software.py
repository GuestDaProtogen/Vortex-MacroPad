import sys
import numpy as np
import serial
import serial.tools.list_ports
import time
import asyncio
import multiprocessing
import threading
import re

# --- THE ABSOLUTE MONKEY PATCH ---
# Fixes compatibility issues with newer numpy versions
np.fromstring = np.frombuffer

# --- CONFIG ---
BAUD_RATE = 115200
UPDATE_INTERVAL = 0.04 
SAMPLE_RATE = 44100
BUFFER_SIZE = 1024

last_send = 0

# --- HELPER: ROBUST DEVICE SELECTION ---
def pick_sc_loopback_once():
    """
    Robustly finds the loopback microphone corresponding to the 
    current Windows Default Output Device (Speakers).
    """
    import soundcard as sc
    try:
        spk = sc.default_speaker()
        if not spk: return None

        # Helper to extract GUID from soundcard ID string
        def guid_tail(dev_id: str) -> str:
            if not dev_id: return ""
            m = re.search(r"\}\.\{([0-9a-fA-F\-]+)\}", dev_id)
            return m.group(1).lower() if m else dev_id.lower()

        spk_guid = guid_tail(getattr(spk, "id", "") or "")

        candidates = sc.all_microphones(include_loopback=True)
        loopbacks = [m for m in candidates if getattr(m, "isloopback", False)]

        # Try GUID match
        exact = [m for m in loopbacks if guid_tail(getattr(m, "id", "") or "") == spk_guid]
        if exact: return exact[0]

        # Try Name match
        name_match = [m for m in loopbacks if spk.name.lower() in (m.name or "").lower()]
        if name_match: return name_match[0]

        return loopbacks[0] if loopbacks else None
    except Exception as e:
        print(f"Device selection error: {e}")
        return None

# --- VOLUME WORKER ---
def _volume_worker(out_q):
    import pythoncom
    import time
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import POINTER, cast

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    last_pct = -1
    
    while True:
        try:
            device_enumerator = AudioUtilities.GetDeviceEnumerator()
            default_device = device_enumerator.GetDefaultAudioEndpoint(0, 1)
            interface = default_device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            
            scalar = volume.GetMasterVolumeLevelScalar()
            pct = int(round(scalar * 100))
            
            if pct != last_pct:
                out_q.put(pct)
                last_pct = pct
        except: pass
        time.sleep(0.2)

# --- AUDIO CAPTURE THREAD ---
def audio_capture_thread(levels_dict):
    import soundcard as sc
    print("Starting Audio Capture Thread...")
    while True:
        try:
            mic = pick_sc_loopback_once()
            if not mic:
                time.sleep(2)
                continue

            print(f"Connected to VU Source: {mic.name}")
            with mic.recorder(samplerate=SAMPLE_RATE) as recorder:
                while True:
                    data = recorder.record(numframes=BUFFER_SIZE)
                    if data.size > 0:
                        l_peak = np.max(np.abs(data[:, 0])) if data.shape[1] > 0 else 0
                        r_peak = np.max(np.abs(data[:, 1])) if data.shape[1] > 1 else 0
                        levels_dict['left'] = int(np.clip(l_peak * 18.0, 0, 8))
                        levels_dict['right'] = int(np.clip(r_peak * 18.0, 0, 8))
        except Exception as e:
            print(f"Audio Error (Rebinding): {e}")
            levels_dict['left'] = 0
            levels_dict['right'] = 0
            time.sleep(1)

# --- MEDIA INFO (ASYNC) ---
async def get_media_info():
    """
    Fetches Title, Artist, Elapsed Time, and Total Duration.
    """
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SM
    try:
        sessions = await SM.request_async()
        curr = sessions.get_current_session()
        if curr:           
            # 1. Metadata
            props = await curr.try_get_media_properties_async()
            title = props.title or "No Title"
            artist = props.artist or "No Artist"

            # 2. Timeline Properties
            # Note: Not all apps populate this (Spotify does, YouTube Chrome often doesn't)
            timeline = curr.get_timeline_properties()
            
            curr_str = "0:00"
            total_str = "0:00"

            if timeline:
                # 'position' and 'max_seek_time' are datetime.timedelta objects in python winrt
                # Position (Elapsed)
                if timeline.position:
                    seconds = int(timeline.position.total_seconds())
                    curr_str = f"{seconds // 60}:{seconds % 60:02d}"
                
                # Duration (Total)
                if timeline.max_seek_time:
                    seconds = int(timeline.max_seek_time.total_seconds())
                    total_str = f"{seconds // 60}:{seconds % 60:02d}"

            return title, artist, curr_str, total_str

    except Exception:
        pass
    
    return "No Media", "System Idle", "0:00", "0:00"

def find_port():
    print("Searching for MACROPAD_STATION...")
    for p in serial.tools.list_ports.comports():
        try:
            with serial.Serial(p.device, BAUD_RATE, timeout=1) as ser:
                time.sleep(2)
                ser.write(b"IDENTIFY\n")
                if "MACROPAD_STATION" in ser.readline().decode(): 
                    print(f"Found on {p.device}")
                    return p.device
        except: continue
    return None

async def main():
    manager = multiprocessing.Manager()
    levels = manager.dict({'left': 0, 'right': 0})
    vol_queue = multiprocessing.Queue()

    multiprocessing.Process(target=_volume_worker, args=(vol_queue,), daemon=True).start()
    threading.Thread(target=audio_capture_thread, args=(levels,), daemon=True).start()

    port = find_port()
    if not port:
        print("Device not found.")
        return

    ser = serial.Serial(port, BAUD_RATE)
    current_vol = 50

    print("Service Running...")

    try:
        while True:
            while not vol_queue.empty():
                current_vol = vol_queue.get()
            
            
            # Unpack 4 values now
            title, artist, elapsed, total = await get_media_info()
            l, r = levels['left'], levels['right']
            
            # Updated Payload with Time Info
            # Format: MET:Title|Artist|L|R|Vol|Elapsed|Total
            payload = f"MET:{title}|{artist}|{l}|{r}|{current_vol}|{elapsed}|{total}\n"
            
            ser.write(payload.encode('utf-8'))
            await asyncio.sleep(UPDATE_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        ser.close()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass