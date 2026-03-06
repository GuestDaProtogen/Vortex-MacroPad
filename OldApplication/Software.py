import sys
import numpy as np
import serial
import serial.tools.list_ports
import time
import asyncio
import multiprocessing
import threading
import re
import warnings

# --- OPTIONAL: Hide discontinuity warnings (should not appear now) ---
warnings.filterwarnings("ignore", message="data discontinuity")

# --- MONKEY PATCH ---
np.fromstring = np.frombuffer

# --- CONFIG ---
BAUD_RATE = 115200
UPDATE_INTERVAL = 0.04
SAMPLE_RATE = 48000      # Match Windows default
BUFFER_SIZE = 1024       # Low latency (~21ms)

# -----------------------------
# DEVICE SELECTION
# -----------------------------
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

# -----------------------------
# VOLUME WORKER (PROCESS)
# -----------------------------
def _volume_worker(out_q):
    import pythoncom
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import POINTER, cast

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    last_pct = -1

    while True:
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

# -----------------------------
# AUDIO CAPTURE THREAD
# -----------------------------
def audio_capture_thread(levels, lock):
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

                        l_val = int(np.clip(l_peak * 18.0, 0, 8))
                        r_val = int(np.clip(r_peak * 18.0, 0, 8))

                        with lock:
                            levels['left'] = l_val
                            levels['right'] = r_val

        except Exception as e:
            print(f"Audio Error (Rebinding): {e}")
            with lock:
                levels['left'] = 0
                levels['right'] = 0
            time.sleep(1)

# -----------------------------
# MEDIA INFO (ASYNC)
# -----------------------------
_media_session = None
_media_last_refresh = 0

_last_timeline = {
    "base_position": 0,
    "base_time": 0,
    "duration": 0,
    "playing": False,
    "title": "No Media",
    "artist": "System Idle"
}


async def get_media_info():
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SM
    )
    import time

    global _media_session
    global _media_last_refresh
    global _last_timeline

    now = time.time()

    try:
        # Refresh session every 3 sec
        if _media_session is None or now - _media_last_refresh > 3:
            sessions = await SM.request_async()
            _media_session = sessions.get_current_session()
            _media_last_refresh = now

        if _media_session:
            # Simulated current elapsed
            simulated = _last_timeline["base_position"]
            if _last_timeline["playing"]:
                simulated += int(now - _last_timeline["base_time"])

            # Refresh timeline every 3 sec
            if now - _last_timeline["base_time"] > 3:
                props = await _media_session.try_get_media_properties_async()
                timeline = _media_session.get_timeline_properties()
                playback = _media_session.get_playback_info()

                _last_timeline["title"] = props.title or "No Title"
                _last_timeline["artist"] = props.artist or "No Artist"

                if timeline and timeline.position:
                    real_position = int(
                        timeline.position.total_seconds()
                    )

                    # Only resync if drift is big (seek/track change)
                    if abs(real_position - simulated) > 2:
                        _last_timeline["base_position"] = real_position
                        _last_timeline["base_time"] = now
                    else:
                        # Keep smooth clock (no snap)
                        _last_timeline["base_position"] = simulated
                        _last_timeline["base_time"] = now

                if timeline and timeline.max_seek_time:
                    _last_timeline["duration"] = int(
                        timeline.max_seek_time.total_seconds()
                    )

                _last_timeline["playing"] = (
                    playback.playback_status == 4
                )

        # ---- Smooth Local Clock ----
        elapsed = _last_timeline["base_position"]

        if _last_timeline["playing"]:
            elapsed += int(now - _last_timeline["base_time"])

        total = _last_timeline["duration"]

        curr_str = f"{elapsed // 60}:{elapsed % 60:02d}"
        total_str = f"{total // 60}:{total % 60:02d}"

        return (
            _last_timeline["title"],
            _last_timeline["artist"],
            curr_str,
            total_str
        )

    except:
        pass

    return "No Media", "System Idle", "0:00", "0:00"



# -----------------------------
# SERIAL DEVICE FINDER
# -----------------------------
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
        except:
            continue
    return None

# -----------------------------
# MAIN LOOP
# -----------------------------
async def main():
    levels = {'left': 0, 'right': 0}
    levels_lock = threading.Lock()
    vol_queue = multiprocessing.Queue()

    multiprocessing.Process(
        target=_volume_worker,
        args=(vol_queue,),
        daemon=True
    ).start()

    threading.Thread(
        target=audio_capture_thread,
        args=(levels, levels_lock),
        daemon=True
    ).start()

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

            title, artist, elapsed, total = await get_media_info()

            with levels_lock:
                l = levels['left']
                r = levels['right']

            payload = (
                f"MET:{title}|{artist}|{l}|{r}|"
                f"{current_vol}|{elapsed}|{total}\n"
            )
            ser.write(payload.encode("utf-8"))

            await asyncio.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        ser.close()

# -----------------------------
# ENTRY POINT
# -----------------------------
if __name__ == "__main__":
    multiprocessing.freeze_support()
    asyncio.run(main())
