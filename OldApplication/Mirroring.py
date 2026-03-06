import sys
import time

print("--- Checkpoint 1: Imports starting ---")
try:
    import serial
    import serial.tools.list_ports
    from PIL import Image
    import mss
    print("--- Checkpoint 2: Imports successful ---")
except ImportError as e:
    print(f"--- ERROR: Missing library! {e} ---")
    print("Try running: py -m pip install pyserial pillow mss")
    sys.exit()

WIDTH, HEIGHT = 128, 64 
BAUD_RATE = 115200 
START_BYTE = b'\x02'
ACK_BYTE = b'\x06'

def select_port():
    print("--- Checkpoint 3: Looking for ports ---")
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("!!! No COM ports found. Is the keyboard plugged in?")
        return None

    print("\n--- Available COM Ports ---")
    for i, p in enumerate(ports):
        print(f"[{i}] {p.device} - {p.description}")
    
    try:
        selection = input("\nSelect Index and press Enter: ")
        return ports[int(selection)].device
    except Exception as e:
        print(f"Invalid selection: {e}")
        return None

def start_mirroring(port_name):
    print(f"--- Checkpoint 4: Connecting to {port_name} ---")
    try:
        ser = serial.Serial(port_name, BAUD_RATE, timeout=0.1)
        ser.reset_input_buffer()
        
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            print("--- SUCCESS: Mirroring started! ---")
            
            # Send initial frame to start handshake
            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            img = img.resize((WIDTH, HEIGHT), Image.NEAREST).convert("1")
            ser.write(START_BYTE + img.tobytes())

            while True:
                # Wait for Arduino's "OK" signal
                if ser.read(1) == ACK_BYTE:
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    img = img.resize((WIDTH, HEIGHT), Image.NEAREST).convert("1")
                    ser.write(START_BYTE + img.tobytes())
                
    except Exception as e:
        print(f"--- RUNTIME ERROR: {e} ---")
    finally:
        if 'ser' in locals(): ser.close()

if __name__ == "__main__":
    print("--- Script Starting ---")
    selected = select_port()
    if selected:
        start_mirroring(selected)
    else:
        print("No port selected. Exiting.")
    input("\nPress Enter to close this window...") # Keeps the window open