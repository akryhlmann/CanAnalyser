"""Print what the Kvaser CANlib driver sees from this Python process.

Run with:  python -m cananalyser.kvaser_diag
"""

from __future__ import annotations

import ctypes
import platform
import struct
import sys


def main() -> int:
    print(f"Python {platform.python_version()} {struct.calcsize('P') * 8}-bit on {platform.platform()}")
    if sys.platform != "win32":
        print("This check is for Windows (canlib32.dll).")
        return 1
    try:
        lib = ctypes.WinDLL("canlib32")
    except OSError as exc:
        print(f"canlib32.dll could not be loaded: {exc}\nInstall 'Kvaser Drivers for Windows'.")
        return 1

    path = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(lib._handle), path, len(path))
    print(f"canlib32.dll loaded from: {path.value}")

    lib.canInitializeLibrary()
    lib.canGetVersion.restype = ctypes.c_ushort
    version = lib.canGetVersion()
    print(f"CANlib version: {version >> 8}.{version & 0xFF}")

    count = ctypes.c_int(0)
    lib.canGetNumberOfChannels(ctypes.byref(count))
    print(f"Channels: {count.value}")
    for channel in range(count.value):
        name = ctypes.create_string_buffer(80)
        serial = ctypes.c_uint64()
        # 26 = canCHANNELDATA_DEVDESCR_ASCII, 7 = canCHANNELDATA_CARD_SERIAL_NO
        lib.canGetChannelData(channel, 26, name, ctypes.sizeof(name))
        lib.canGetChannelData(channel, 7, ctypes.byref(serial), ctypes.sizeof(serial))
        print(f"  {channel}: {name.value.decode(errors='replace')}  S/N {serial.value}")
    if count.value == 0:
        print("No channels at all: the driver is installed but not working.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
