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
                self._on_reset_node()
            self._boot()
            return
        self._send(0x700 + self.NODE, bytes([self.state]), False)

    def _on_reset_node(self) -> None:
        # Pretend a fault was latched before the reset, then cleared.
        self._send(0x080 + self.NODE, bytes([0x10, 0x32, 0x04, 0, 0, 0, 0, 0]), False)
        self._send(0x080 + self.NODE, bytes(8), False)


def pack_bits(fields, length: int = 8) -> bytes:
    """Pack (start_bit, bit_length, value) fields LSB-first; unused bits are 1."""
    value = (1 << (8 * length)) - 1
    for start, bits, field in fields:
        mask = ((1 << bits) - 1) << start
        value = (value & ~mask) | ((field << start) & mask)
    return value.to_bytes(length, "little")


def _js1h_motion(t: float, phase: float) -> dict[str, float]:
    """Slow Lissajous movement of base and grip axes, in percent."""
    return {
        "X": 100 * math.sin(t / 2 + phase),
        "Y": 100 * math.sin(t / 3 + phase),
        "G1": 100 * math.sin(t / 1.5 + phase),
        "G2": 100 * math.cos(t / 2.5 + phase),
        "G3": 60 * math.sin(t / 4 + phase),
    }


# The simulated grip Y / grip 2 sensor reports a voltage error for
# FAULT_ON seconds out of every FAULT_PERIOD.
FAULT_PERIOD, FAULT_ON = 15.0, 3.0


def _fault_active(t: float) -> bool:
    return t % FAULT_PERIOD > FAULT_PERIOD - FAULT_ON


class JS1HJ1939Simulator(_Simulator):
    """Danfoss JS1-H joystick at SA 0x13 sending BJM1/EJM1 and BJM3/EJM3."""

    SA = 0x13

    def _run(self) -> None:
        name = (1 << 63) | (25 << 21) | 1  # arbitrary NAME
        self._send(j1939_id(6, 60928, self.SA), name.to_bytes(8, "little"), True)
        t0 = time.monotonic()
        next_dm1 = t0
        while not self._stop.wait(0.02):
            now = time.monotonic()
            t = now - t0
            fault = _fault_active(t)
            for message_set, phase in ((1, 0.0), (3, 1.0)):
                axes = _js1h_motion(t, phase)
                bjm = 0xFDD6 if message_set == 1 else 0xFDDA
                ejm = bjm + 1
                pressed = int(t * 2) % 8 + 1  # one of buttons 1..8 at a time
                buttons = [(1 if b == pressed else 0) for b in range(1, 11)] + [3, 3]  # 11, 12 not installed
                self._send(j1939_id(3, bjm, self.SA), self._bjm(axes["X"], axes["Y"], buttons), True)
                self._send(j1939_id(3, ejm, self.SA), self._ejm(axes["G1"], None if fault else axes["G2"]), True)
            if now >= next_dm1:
                next_dm1 = now + 1.0
                # SPN 2663 (grip Y) FMI 3 voltage high, or "no active DTCs".
                dtc = (2663 | (3 << 16) | (1 << 24)) if fault else 0
                lamps = 0b0000_0100 if fault else 0
                self._send(j1939_id(6, 65226, self.SA), bytes([lamps, 0xFF]) + struct.pack("<I", dtc) + b"\xff\xff", True)

    @staticmethod
    def _axis_fields(value: float | None, base: int) -> list[tuple[int, int, int]]:
        if value is None:  # sensor error: 1022 counts, status bits "error"
            return [(base, 2, 2), (base + 2, 2, 2), (base + 4, 2, 2), (base + 6, 10, 1022)]
        counts = min(1000, round(abs(value) * 10))
        neutral = counts < 20
        return [
            (base, 2, int(neutral)),
            (base + 2, 2, int(value < 0 and not neutral)),
            (base + 4, 2, int(value > 0 and not neutral)),
            (base + 6, 10, counts),
        ]

    def _bjm(self, x: float, y: float, buttons: list[int]) -> bytes:
        fields = self._axis_fields(x, 0) + self._axis_fields(y, 16) + [(36, 2, 0), (38, 2, 0)]
        layout = {1: 46, 2: 44, 3: 42, 4: 40, 5: 54, 6: 52, 7: 50, 8: 48, 9: 62, 10: 60, 11: 58, 12: 56}
        fields += [(layout[b], 2, state) for b, state in enumerate(buttons, start=1)]
        return pack_bits(fields)

    def _ejm(self, grip_x: float, grip_y: float | None) -> bytes:
        return pack_bits(self._axis_fields(grip_x, 0) + self._axis_fields(grip_y, 16) + [(52, 2, 3), (54, 2, 3)])


class JS1HCANopenSimulator(CANopenSimulator):
    """Danfoss JS1-H (CAN variant) at node 0x10: heartbeat, TPDO1-3, EMCY."""

    NODE = 0x10
    ENABLED_BUTTONS = 10

    def _run(self) -> None:
        self._boot()
        t0 = time.monotonic()
        next_hb = next_pdo = t0
        fault_reported = False
        while not self._stop.is_set():
            msg = self.bus.recv(0.005)
            if msg is not None:
                self._handle(msg)
            now = time.monotonic()
            if now >= next_hb:
                self._send(0x700 + self.NODE, bytes([self.state]), False)
                next_hb = now + 0.02  # 1017h default: 20 ms
            if now < next_pdo or self.state != 0x05:
                continue
            next_pdo = now + 0.02
            t = now - t0
            fault = _fault_active(t)
            axes = _js1h_motion(t, 0.0)
            pressed = int(t * 2) % 8 + 1
            pressed_bits = 1 << (pressed - 1)
            enabled_bits = (1 << self.ENABLED_BUTTONS) - 1
            tpdo1 = bytes([0]) + pressed_bits.to_bytes(3, "little") + bytes([0]) + enabled_bits.to_bytes(3, "little")
            self._send(0x180 + self.NODE, tpdo1, False)
            self._send(0x280 + self.NODE, self._axes_pdo([axes["X"], axes["Y"], 0], third_reserved=True), False)
            grip2 = None if fault else axes["G2"]
            self._send(0x380 + self.NODE, self._axes_pdo([axes["G1"], grip2, axes["G3"]]), False)
            if fault != fault_reported:
                fault_reported = fault
                # 0x3110 voltage high, ER bit 2, occurrence 1, sensor 0x83 (grip Y); then reset.
                emcy = bytes([0x10, 0x31, 0x04, 1, 0x83, 0, 0, 0]) if fault else bytes(8)
                self._send(0x080 + self.NODE, emcy, False)

    @staticmethod
    def _axes_pdo(values: list[float | None], third_reserved: bool = False) -> bytes:
        """Three S16 values (0.1 %) followed by three 2-bit neutral states.

        In TPDO2 the third value and state are reserved objects (6405h/6406h).
        """
        raw, bits = b"", 0
        for i, value in enumerate(values):
            raw += struct.pack("<h", 0 if value is None else round(value * 10))
            state = 2 if value is None else int(abs(value) >= 2)  # 0 neutral, 1 out, 2 error
            if third_reserved and i == 2:
                state = 0
            bits |= state << (2 * i)
        return raw + bytes([bits])

    def _boot(self) -> None:
        self.state = 0x7F
        self._send(0x700 + self.NODE, b"\x00", False)

    def _on_reset_node(self) -> None:
        pass


SIMULATORS = {
    ("j1939", "generic"): J1939Simulator,
    ("canopen", "generic"): CANopenSimulator,
    ("j1939", "js1h"): JS1HJ1939Simulator,
    ("canopen", "js1h"): JS1HCANopenSimulator,
}
