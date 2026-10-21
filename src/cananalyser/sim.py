"""Traffic simulators for developing and demoing without hardware.

Each simulator runs on a python-can *virtual* bus channel inside this
process; point the monitor at the same channel (interface "virtual").
"""

from __future__ import annotations

import math
import struct
import threading
import time

import can

from cananalyser.decoders.canopen import (
    NMT_PRE_OPERATIONAL,
    NMT_RESET_COMMUNICATION,
    NMT_RESET_NODE,
    NMT_START,
    NMT_STOP,
)


def j1939_id(priority: int, pgn: int, sa: int, da: int = 0xFF) -> int:
    pf = (pgn >> 8) & 0xFF
    ps = da if pf < 240 else pgn & 0xFF
    return (priority << 26) | ((pgn >> 16) << 24) | (pf << 16) | (ps << 8) | sa


class _Simulator:
    def __init__(self, channel: str):
        self.channel = channel
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.bus: can.BusABC | None = None

    def start(self) -> None:
        self.bus = can.Bus(interface="virtual", channel=self.channel)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=type(self).__name__, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.bus:
            self.bus.shutdown()
            self.bus = None

    def _send(self, can_id: int, data: bytes, extended: bool) -> None:
        self.bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=extended))

    def _run(self) -> None:
        raise NotImplementedError


class J1939Simulator(_Simulator):
    """An engine ECU at address 0x00 broadcasting a few PGNs and two DTCs."""

    SA = 0x00

    def _run(self) -> None:
        name = (1 << 63) | (0 << 60) | (0 << 40) | (123 << 21) | 4711  # func 0 = engine
        self._send(j1939_id(6, 60928, self.SA), name.to_bytes(8, "little"), True)
        t0 = time.monotonic()
        tick = 0
        while not self._stop.wait(0.1):
            t = time.monotonic() - t0
            rpm = 1200 + 600 * math.sin(t / 3)
            eec1 = bytes([0xF3, 0x7D, 125 + 40]) + struct.pack("<H", int(rpm / 0.125)) + b"\xff\xff\xff"
            self._send(j1939_id(3, 61444, self.SA), eec1, True)
            speed = 50 + 30 * math.sin(t / 5)
            ccvs = bytes([0xF4]) + struct.pack("<H", int(speed * 256)) + b"\xff" * 5
            self._send(j1939_id(6, 65265, self.SA), ccvs, True)
            if tick % 10 == 0:
                coolant = int(85 + 5 * math.sin(t / 10)) + 40
                oil = int((95 + 273) / 0.03125)
                et1 = bytes([coolant, 0xFF]) + struct.pack("<H", oil) + b"\xff" * 4
                self._send(j1939_id(6, 65262, self.SA), et1, True)
                self._send_dm1()
            tick += 1

    def _send_dm1(self) -> None:
        # Two active DTCs -> 10 bytes -> sent with BAM.
        lamps = bytes([0b0000_0100, 0xFF])  # amber warning lamp on
        dtc1 = struct.pack("<I", 110 | (15 << 16) | (3 << 24))  # SPN 110 FMI 15, OC 3
        dtc2 = struct.pack("<I", 190 | (2 << 16) | (1 << 24))  # SPN 190 FMI 2, OC 1
        payload = lamps + dtc1 + dtc2
        packets = (len(payload) + 6) // 7
        cm = bytes([32]) + struct.pack("<H", len(payload)) + bytes([packets, 0xFF]) + (65226).to_bytes(3, "little")
        self._send(j1939_id(7, 60416, self.SA), cm, True)
        for seq in range(packets):
            chunk = payload[seq * 7 : seq * 7 + 7].ljust(7, b"\xff")
            self._send(j1939_id(7, 60160, self.SA), bytes([seq + 1]) + chunk, True)


class CANopenSimulator(_Simulator):
    """A CANopen node plus a minimal master that configures it at boot.

    The master remaps TPDO1 to three objects, which is *not* what the
    example EDS says, to show how observed SDO configuration overrides an
    outdated EDS. The node obeys NMT commands sent by the monitor.
    """

    NODE = 5
    MASTER_CONFIG = (  # (index, subindex, value, size)
        (0x1A00, 0, 0, 1),
        (0x1A00, 1, 0x60000110, 4),
        (0x1A00, 2, 0x60000210, 4),
        (0x1A00, 3, 0x60000308, 4),
        (0x1A00, 0, 3, 1),
    )

    def _run(self) -> None:
        self.state = 0x7F
        self._boot()
        t0 = time.monotonic()
        next_hb = next_pdo = time.monotonic()
        while not self._stop.is_set():
            msg = self.bus.recv(0.01)
            if msg is not None:
                self._handle(msg)
            now = time.monotonic()
            if now >= next_hb:
                self._send(0x700 + self.NODE, bytes([self.state]), False)
                next_hb = now + 1.0
            if now >= next_pdo:
                next_pdo = now + 0.1
                if self.state == 0x05:
                    t = now - t0
                    speed = int(1500 + 500 * math.sin(t / 2))
                    current = int(-200 + 400 * math.sin(t / 4))
                    self._send(0x180 + self.NODE, struct.pack("<HhB", speed, current, int(t) % 4), False)

    def _boot(self) -> None:
        self.state = 0x7F
        self._send(0x700 + self.NODE, b"\x00", False)
        for index, sub, value, size in self.MASTER_CONFIG:
            cmd = 0x23 | ((4 - size) << 2)
            req = bytes([cmd]) + struct.pack("<HB", index, sub) + value.to_bytes(4, "little")
            self._send(0x600 + self.NODE, req, False)
            self._send(0x580 + self.NODE, bytes([0x60]) + struct.pack("<HB", index, sub) + b"\0" * 4, False)

    def _handle(self, msg: can.Message) -> None:
        if msg.arbitration_id != 0x000 or len(msg.data) < 2:
            return
        if msg.data[1] not in (0, self.NODE):
            return
        command = msg.data[0]
        if command == NMT_START:
            self.state = 0x05
        elif command == NMT_STOP:
            self.state = 0x04
        elif command == NMT_PRE_OPERATIONAL:
            self.state = 0x7F
        elif command in (NMT_RESET_NODE, NMT_RESET_COMMUNICATION):
            if command == NMT_RESET_NODE:
                # Pretend a fault was latched before the reset, then cleared.
                self._send(0x080 + self.NODE, bytes([0x10, 0x32, 0x04, 0, 0, 0, 0, 0]), False)
                self._send(0x080 + self.NODE, bytes(8), False)
            self._boot()
            return
        self._send(0x700 + self.NODE, bytes([self.state]), False)


SIMULATORS = {"j1939": J1939Simulator, "canopen": CANopenSimulator}
