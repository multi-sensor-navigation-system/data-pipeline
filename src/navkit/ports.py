"""Serial port discovery."""

import serial.tools.list_ports

# USB-serial bridges found on ESP32 dev boards, plus Espressif's native USB.
_HINTS = ("cp210", "ch340", "ch910", "ftdi", "esp32", "silicon", "espressif", "usb jtag")
_ESPRESSIF_VID = 0x303A


def candidates() -> list:
    """Ports that look like an ESP32 board, as pyserial ListPortInfo objects."""
    found = []
    for p in serial.tools.list_ports.comports():
        text = f"{p.description or ''} {p.manufacturer or ''}".lower()
        if p.vid == _ESPRESSIF_VID or any(h in text for h in _HINTS):
            found.append(p)
    return found


def find_esp32_port() -> str | None:
    """Pick the board's port: the only candidate, or ask when there are several."""
    ports = [p.device for p in candidates()]
    if len(ports) == 1:
        return ports[0]
    if ports:
        print("Multiple ESP32-like ports found:")
        for i, c in enumerate(ports):
            print(f"  [{i}] {c}")
        idx = int(input("Select index: "))
        return ports[idx]
    return None
