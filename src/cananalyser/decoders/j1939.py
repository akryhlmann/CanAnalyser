"""SAE J1939 decoder.

Handles identifier decomposition, transport protocol (BAM and RTS/CTS)
reassembly, address claims, DM1/DM2 diagnostics and DBC signal decoding.
Everything degrades gracefully when the DBC is incomplete: unknown PGNs
are named from a built-in table and shown as raw data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import can
import cantools

from cananalyser.decoders.j1939_pgns import (
    PGN_ADDRESS_CLAIMED,
    PGN_DM1,
    PGN_DM2,
    PGN_NAMES,
    PGN_REQUEST,
    PGN_TP_CM,
    PGN_TP_DT,
)
from cananalyser.model import DecodedFrame, Fault, FaultSet, NodeInfo, SignalValue

GLOBAL_ADDRESS = 0xFF
NULL_ADDRESS = 0xFE

# Generous compared to the J1939-21 T1..T4 timeouts so that host-side
# timestamp jitter never drops a valid transfer.
TP_SESSION_TIMEOUT = 2.0

# How often (in bus time) a node's "last seen" is re-published to the UI.
NODE_REFRESH_INTERVAL = 0.5

FMI_DESCRIPTIONS = {
    0: "Above normal (most severe)",
    1: "Below normal (most severe)",
    2: "Erratic, intermittent or incorrect",
    3: "Voltage above normal / shorted high",
    4: "Voltage below normal / shorted low",
    5: "Current below normal / open circuit",
    6: "Current above normal / grounded circuit",
    7: "Mechanical system not responding",
    8: "Abnormal frequency, pulse width or period",
    9: "Abnormal update rate",
    10: "Abnormal rate of change",
    11: "Root cause not known",
    12: "Bad intelligent device or component",
    13: "Out of calibration",
    14: "Special instructions",
    15: "Above normal (least severe)",
    16: "Above normal (moderately severe)",
    17: "Below normal (least severe)",
    18: "Below normal (moderately severe)",
    19: "Received network data in error",
    20: "Data drifted high",
    21: "Data drifted low",
    31: "Condition exists",
}

LAMP_NAMES = ("PL", "AWL", "RSL", "MIL")  # bit pairs 1-0, 3-2, 5-4, 7-6


@dataclass(frozen=True)
class J1939Id:
    priority: int
    pgn: int
    sa: int
    da: int

    @classmethod
    def parse(cls, can_id: int) -> J1939Id:
        priority = (can_id >> 26) & 0x7
        edp_dp = (can_id >> 24) & 0x3
        pf = (can_id >> 16) & 0xFF
        ps = (can_id >> 8) & 0xFF
        sa = can_id & 0xFF
        if pf < 240:  # PDU1: PS is the destination address
            return cls(priority, (edp_dp << 16) | (pf << 8), sa, ps)
        return cls(priority, (edp_dp << 16) | (pf << 8) | ps, sa, GLOBAL_ADDRESS)


@dataclass(frozen=True)
class J1939Name:
    """The 64-bit NAME sent in an Address Claimed message."""

    identity: int
    manufacturer: int
    ecu_instance: int
    function_instance: int
    function: int
    vehicle_system: int
    vehicle_system_instance: int
    industry_group: int
    arbitrary_address_capable: bool

    @classmethod
    def parse(cls, data: bytes) -> J1939Name:
        v = int.from_bytes(data[:8], "little")
        return cls(
            identity=v & 0x1FFFFF,
            manufacturer=(v >> 21) & 0x7FF,
            ecu_instance=(v >> 32) & 0x7,
            function_instance=(v >> 35) & 0x1F,
            function=(v >> 40) & 0xFF,
            vehicle_system=(v >> 49) & 0x7F,
            vehicle_system_instance=(v >> 56) & 0xF,
            industry_group=(v >> 60) & 0x7,
            arbitrary_address_capable=bool(v >> 63),
        )

    def summary(self) -> str:
        return (
            f"func {self.function} mfr {self.manufacturer} "
            f"id {self.identity} ecu-inst {self.ecu_instance}"
        )


@dataclass
class _TpSession:
    pgn: int
    size: int
    packets: int
    bam: bool
    last: float
    data: bytearray = field(default_factory=bytearray)
    received: set[int] = field(default_factory=set)


@dataclass
class _Node:
    name: J1939Name | None = None
    state: str = "active"
    pgns: set[int] = field(default_factory=set)
    published: float = float("-inf")


def pgn_name(pgn: int) -> str:
    return PGN_NAMES.get(pgn, f"PGN {pgn} (0x{pgn:05X})")


def parse_dtcs(data: bytes) -> tuple[dict[str, int], list[tuple[int, int, int]]]:
    """Parse a DM1/DM2 payload into lamp states and (SPN, FMI, OC) tuples."""
    lamps = {}
    if data:
        for i, name in enumerate(LAMP_NAMES):
            lamps[name] = (data[0] >> (2 * i)) & 0x3
    dtcs = []
    for off in range(2, len(data) - 3, 4):
        b0, b1, b2, b3 = data[off : off + 4]
        spn = b0 | (b1 << 8) | ((b2 >> 5) << 16)
        fmi = b2 & 0x1F
        oc = b3 & 0x7F
        if spn == 0 and fmi == 0:  # "no active DTCs" placeholder
            continue
        if (b0, b1, b2, b3) == (0xFF, 0xFF, 0xFF, 0xFF):  # padding
            continue
        dtcs.append((spn, fmi, oc))
    return lamps, dtcs


class J1939Decoder:
    def __init__(self, dbc_path: str | Path | None = None):
        self._db: cantools.database.Database | None = None
        self._by_pgn: dict[int, cantools.database.Message] = {}
        self._by_pgn_sa: dict[tuple[int, int], cantools.database.Message] = {}
        self._spn_names: dict[int, str] = {}
        self._sessions: dict[tuple[int, int], _TpSession] = {}
        self._nodes: dict[int, _Node] = {}
        if dbc_path:
            self.load_dbc(dbc_path)

    # -- configuration -------------------------------------------------

    def load_dbc(self, path: str | Path) -> None:
        """Load a J1939 DBC. Can be called again to add more files."""
        if self._db is None:
            self._db = cantools.database.Database()
        self._db.add_dbc_file(str(path))
        self._index_db()

    def _index_db(self) -> None:
        by_pgn, by_pgn_sa, spn_names = {}, {}, {}
        for message in self._db.messages:
            jid = J1939Id.parse(message.frame_id)
            by_pgn.setdefault(jid.pgn, message)
            by_pgn_sa[(jid.pgn, jid.sa)] = message
            for signal in message.signals:
                if signal.spn is not None:
                    spn_names.setdefault(signal.spn, signal.name)
        self._by_pgn, self._by_pgn_sa, self._spn_names = by_pgn, by_pgn_sa, spn_names

    def reset(self) -> None:
        self._sessions.clear()
        self._nodes.clear()

    # -- decoding ------------------------------------------------------

    def decode(self, msg: can.Message) -> DecodedFrame:
        frame = DecodedFrame(msg)
        if not msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame:
            frame.summary = "Not a J1939 frame (11-bit)" if not msg.is_extended_id else ""
            return frame

        jid = J1939Id.parse(msg.arbitration_id)
        data = bytes(msg.data)
        ts = msg.timestamp
        self._expire_sessions(ts)

        if jid.pgn == PGN_TP_CM:
            frame.summary = self._handle_tp_cm(jid, data, ts)
        elif jid.pgn == PGN_TP_DT:
            frame.summary = self._handle_tp_dt(jid, data, ts, frame)
        else:
            self._decode_pg(jid.pgn, jid.sa, jid.da, data, frame)
            frame.summary = f"{frame.summary}  {_addr(jid)}"

        self._touch_node(jid.sa, jid.pgn, ts, frame)
        return frame

    def _decode_pg(self, pgn: int, sa: int, da: int, data: bytes, frame: DecodedFrame) -> None:
        """Decode one complete parameter group (single frame or reassembled)."""
        message = self._by_pgn_sa.get((pgn, sa)) or self._by_pgn.get(pgn)
        name = message.name if message is not None else pgn_name(pgn)
        frame.summary = name

        if pgn == PGN_REQUEST and len(data) >= 3:
            requested = int.from_bytes(data[:3], "little")
            frame.summary = f"Request {pgn_name(requested)}"
        elif pgn == PGN_ADDRESS_CLAIMED and len(data) >= 8:
            node = self._nodes.setdefault(sa, _Node())
            node.name = J1939Name.parse(data)
            node.state = "cannot claim" if sa == NULL_ADDRESS else "claimed"
            node.published = float("-inf")  # force a node update
            frame.summary = f"Address Claimed: {node.name.summary()}"
        elif pgn in (PGN_DM1, PGN_DM2):
            frame.faults.append(self._decode_dm(pgn, sa, data))
            lamps, _ = parse_dtcs(data)
            on = [n for n, s in lamps.items() if s == 1]
            frame.summary = f"{name}  lamps: {','.join(on) or 'off'}"

        if message is not None:
            frame.signals.extend(self._decode_signals(message, data, sa))

    def _decode_signals(self, message, data: bytes, sa: int) -> list[SignalValue]:
        try:
            raw = message.decode(data, decode_choices=False, scaling=False, allow_truncated=True)
            phys = message.decode(data, decode_choices=True, scaling=True, allow_truncated=True)
        except Exception as exc:  # noqa: BLE001 - malformed data or DBC mismatch
            return [SignalValue(message.name, "<decode error>", str(exc), node=sa)]
        out = []
        for signal in message.signals:
            if signal.name not in phys:
                continue
            value = phys[signal.name]
            status = _j1939_status(raw[signal.name], signal.length, signal.is_signed)
            if status:
                value = status
            elif not isinstance(value, (int, float)):
                value = str(value)
            out.append(SignalValue(message.name, signal.name, value, signal.unit or "", sa, raw[signal.name]))
        return out

    def _decode_dm(self, pgn: int, sa: int, data: bytes) -> FaultSet:
        lamps, dtcs = parse_dtcs(data)
        lamp_text = " ".join(f"{n}={'on' if s == 1 else 'off'}" for n, s in lamps.items())
        faults = tuple(
            Fault(
                code=f"SPN {spn} FMI {fmi}",
                description=self._spn_names.get(spn, f"SPN {spn}")
                + f": {FMI_DESCRIPTIONS.get(fmi, 'FMI ' + str(fmi))}",
                detail=f"OC {oc}; {lamp_text}",
            )
            for spn, fmi, oc in dtcs
        )
        tag = "DM1" if pgn == PGN_DM1 else "DM2"
        return FaultSet(f"SA 0x{sa:02X} {tag}", faults)

    # -- transport protocol ---------------------------------------------

    def _handle_tp_cm(self, jid: J1939Id, data: bytes, ts: float) -> str:
        if len(data) < 8:
            return "TP.CM (short frame)"
        control = data[0]
        pgn = int.from_bytes(data[5:8], "little")
        key = (jid.sa, jid.da)
        if control in (16, 32):  # RTS / BAM
            size = int.from_bytes(data[1:3], "little")
            packets = data[3]
            self._sessions[key] = _TpSession(pgn, size, packets, control == 32, ts)
            kind = "BAM" if control == 32 else "RTS"
            return f"TP.CM {kind} {pgn_name(pgn)}, {size} bytes / {packets} packets  {_addr(jid)}"
        if control == 17:
            return f"TP.CM CTS {pgn_name(pgn)}, {data[1]} packets from #{data[2]}  {_addr(jid)}"
        if control == 19:
            return f"TP.CM EndOfMsgAck {pgn_name(pgn)}  {_addr(jid)}"
        if control == 255:
            self._sessions.pop(key, None)
            self._sessions.pop((jid.da, jid.sa), None)
            return f"TP.CM Abort {pgn_name(pgn)}, reason {data[1]}  {_addr(jid)}"
        return f"TP.CM control {control}  {_addr(jid)}"

    def _handle_tp_dt(self, jid: J1939Id, data: bytes, ts: float, frame: DecodedFrame) -> str:
        session = self._sessions.get((jid.sa, jid.da))
        if session is None or not data:
            return f"TP.DT (no session)  {_addr(jid)}"
        seq = data[0]
        if not 1 <= seq <= session.packets:
            return f"TP.DT #{seq} out of range  {_addr(jid)}"
        start = (seq - 1) * 7
        if len(session.data) < start + 7:
            session.data.extend(bytes(start + 7 - len(session.data)))
        session.data[start : start + 7] = data[1:8].ljust(7, b"\xff")
        session.received.add(seq)
        session.last = ts
        if len(session.received) < session.packets:
            return f"TP.DT #{seq}/{session.packets} {pgn_name(session.pgn)}  {_addr(jid)}"

        del self._sessions[(jid.sa, jid.da)]
        payload = bytes(session.data[: session.size])
        self._decode_pg(session.pgn, jid.sa, jid.da, payload, frame)
        return f"TP.DT #{seq}/{session.packets} complete -> {frame.summary} [{payload.hex(' ')}]  {_addr(jid)}"

    def _expire_sessions(self, now: float) -> None:
        stale = [k for k, s in self._sessions.items() if now - s.last > TP_SESSION_TIMEOUT]
        for key in stale:
            del self._sessions[key]

    # -- nodes ------------------------------------------------------------

    def _touch_node(self, sa: int, pgn: int, ts: float, frame: DecodedFrame) -> None:
        node = self._nodes.setdefault(sa, _Node())
        new_pgn = pgn not in node.pgns
        node.pgns.add(pgn)
        if not new_pgn and ts - node.published < NODE_REFRESH_INTERVAL:
            return
        node.published = ts
        frame.nodes.append(
            NodeInfo(
                address=sa,
                state=node.state,
                name=node.name.summary() if node.name else "",
                details=f"{len(node.pgns)} PGNs",
                last_seen=ts,
            )
        )


def _addr(jid: J1939Id) -> str:
    if jid.da == GLOBAL_ADDRESS:
        return f"SA=0x{jid.sa:02X}"
    return f"SA=0x{jid.sa:02X} DA=0x{jid.da:02X}"


def _j1939_status(raw, length: int, signed: bool) -> str | None:
    """Return "n/a" / "error" for the J1939 reserved raw value ranges."""
    if signed or not isinstance(raw, int) or length < 2:
        return None
    if length % 8 == 0:  # byte-sized parameters reserve whole ranges (0xFExx, 0xFFxx)
        top = raw >> (length - 8)
        if top == 0xFF:
            return "n/a"
        if top == 0xFE:
            return "error"
        return None
    full = (1 << length) - 1
    if raw == full:
        return "n/a"
    if raw == full - 1:
        return "error"
    return None
