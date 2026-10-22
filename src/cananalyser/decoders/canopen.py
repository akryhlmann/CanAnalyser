"""CANopen (CiA 301) passive decoder.

Classifies frames using the predefined connection set, tracks NMT state from
heartbeats, reassembles expedited and segmented SDO transfers, decodes EMCY
messages and decodes PDOs.

PDO mappings come from two sources, the second overriding the first:

1. An EDS/DCF file loaded for a node ID (default/parameter values).
2. Mapping and communication parameters *observed* on the bus, i.e. SDO
   writes or reads of objects 0x1400-0x1BFF, typically made by the master
   while configuring the node at start-up.

That way PDOs can be decoded even when the EDS is out of date, as long as
the configuration phase is captured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import can
from canopen.objectdictionary import ObjectDictionary, ODVariable, import_od
from canopen.objectdictionary.datatypes import SIGNED_TYPES

from cananalyser.model import DecodedFrame, Fault, FaultSet, NodeInfo, SignalValue

NMT_STATES = {0: "boot-up", 4: "stopped", 5: "operational", 127: "pre-operational"}

NMT_START = 0x01
NMT_STOP = 0x02
NMT_PRE_OPERATIONAL = 0x80
NMT_RESET_NODE = 0x81
NMT_RESET_COMMUNICATION = 0x82

NMT_COMMANDS = {
    NMT_START: "Start remote node",
    NMT_STOP: "Stop remote node",
    NMT_PRE_OPERATIONAL: "Enter pre-operational",
    NMT_RESET_NODE: "Reset node",
    NMT_RESET_COMMUNICATION: "Reset communication",
}

SDO_ABORT_CODES = {
    0x05030000: "Toggle bit not alternated",
    0x05040000: "SDO protocol timed out",
    0x05040001: "Command specifier not valid or unknown",
    0x05040005: "Out of memory",
    0x06010000: "Unsupported access to an object",
    0x06010001: "Attempt to read a write only object",
    0x06010002: "Attempt to write a read only object",
    0x06020000: "Object does not exist in the object dictionary",
    0x06040041: "Object cannot be mapped to the PDO",
    0x06040042: "Mapped objects would exceed PDO length",
    0x06040043: "General parameter incompatibility",
    0x06040047: "General internal incompatibility in the device",
    0x06060000: "Access failed due to a hardware error",
    0x06070010: "Data type does not match, length does not match",
    0x06070012: "Data type does not match, length too high",
    0x06070013: "Data type does not match, length too low",
    0x06090011: "Sub-index does not exist",
    0x06090030: "Invalid value for parameter",
    0x06090031: "Value of parameter written too high",
    0x06090032: "Value of parameter written too low",
    0x06090036: "Maximum value is less than minimum value",
    0x08000000: "General error",
    0x08000020: "Data cannot be transferred or stored to the application",
    0x08000021: "Data cannot be transferred (local control)",
    0x08000022: "Data cannot be transferred (present device state)",
    0x08000023: "Object dictionary not present",
    0x08000024: "No data available",
}

# EMCY error code classes, most specific prefix first.
EMCY_CODES = {
    0x0000: "Error reset or no error",
    0x3110: "Mains over-voltage",
    0x3120: "Mains under-voltage",
    0x8110: "CAN overrun (objects lost)",
    0x8120: "CAN in error passive mode",
    0x8130: "Life guard or heartbeat error",
    0x8140: "Recovered from bus off",
    0x8150: "CAN-ID collision",
    0x8210: "PDO not processed due to length error",
    0x8220: "PDO length exceeded",
    0x8230: "DAM MPDO not processed, destination object not available",
    0x8240: "Unexpected SYNC data length",
    0x8250: "RPDO timeout",
}
EMCY_CLASSES = {
    0x10: "Generic error",
    0x20: "Current",
    0x21: "Current, device input side",
    0x22: "Current inside the device",
    0x23: "Current, device output side",
    0x30: "Voltage",
    0x31: "Mains voltage",
    0x32: "Voltage inside the device",
    0x33: "Output voltage",
    0x40: "Temperature",
    0x41: "Ambient temperature",
    0x42: "Device temperature",
    0x50: "Device hardware",
    0x60: "Device software",
    0x61: "Internal software",
    0x62: "User software",
    0x63: "Data set",
    0x70: "Additional modules",
    0x80: "Monitoring",
    0x81: "Communication",
    0x82: "Protocol error",
    0x90: "External error",
    0xF0: "Additional functions",
    0xFF: "Device specific",
}
ERROR_REGISTER_BITS = (
    "generic", "current", "voltage", "temperature",
    "communication", "profile", "reserved", "manufacturer",
)

# (function code, name) for the predefined connection set.
_PDO_FUNCTIONS = {
    0x180: "TPDO1", 0x200: "RPDO1", 0x280: "TPDO2", 0x300: "RPDO2",
    0x380: "TPDO3", 0x400: "RPDO3", 0x480: "TPDO4", 0x500: "RPDO4",
}
_PDO_PARAM_RANGE = range(0x1400, 0x1C00)


def emcy_description(code: int) -> str:
    if code in EMCY_CODES:
        return EMCY_CODES[code]
    return EMCY_CLASSES.get(code >> 8) or EMCY_CLASSES.get((code >> 8) & 0xF0, "Unknown")


def nmt_frame(command: int, node_id: int = 0) -> can.Message:
    """Build an NMT command frame. ``node_id`` 0 addresses all nodes."""
    if command not in NMT_COMMANDS:
        raise ValueError(f"Unknown NMT command 0x{command:02X}")
    if not 0 <= node_id <= 127:
        raise ValueError("node_id must be 0..127")
    return can.Message(arbitration_id=0x000, data=[command, node_id], is_extended_id=False)


@dataclass
class _Pdo:
    node: int
    name: str
    entries: list[tuple[int, int, int]]  # (index, subindex, bit length)


@dataclass
class _SdoTransfer:
    index: int
    subindex: int
    upload: bool  # True = read from the node
    size: int | None = None
    data: bytearray = field(default_factory=bytearray)
    expedited_pending: bool = False


@dataclass
class _Node:
    state: str = ""
    last_heartbeat: float | None = None
    interval: float | None = None


class CANopenDecoder:
    def __init__(self) -> None:
        self._ods: dict[int, ObjectDictionary] = {}
        self._learned: dict[int, dict[tuple[int, int], int]] = {}
        self._pdos: dict[int, _Pdo] = {}
        self._sdo: dict[int, _SdoTransfer] = {}
        self._nodes: dict[int, _Node] = {}
        self._emcy: dict[int, dict[int, Fault]] = {}

    # -- configuration -------------------------------------------------

    def load_eds(self, path: str | Path, node_id: int) -> ObjectDictionary:
        """Load an EDS/DCF file and associate it with ``node_id``."""
        od = import_od(str(path), node_id)
        self._ods[node_id] = od
        self._rebuild_pdos()
        return od

    def object_dictionary(self, node_id: int) -> ObjectDictionary | None:
        return self._ods.get(node_id)

    def reset(self) -> None:
        """Forget runtime state; loaded EDS files are kept."""
        self._learned.clear()
        self._sdo.clear()
        self._nodes.clear()
        self._emcy.clear()
        self._rebuild_pdos()

    # -- decoding ------------------------------------------------------

    def decode(self, msg: can.Message) -> DecodedFrame:
        frame = DecodedFrame(msg)
        if msg.is_error_frame:
            return frame
        if msg.is_extended_id:
            frame.summary = "29-bit frame (not CANopen)"
            return frame

        cob = msg.arbitration_id
        data = bytes(msg.data)
        function, node = cob & 0x780, cob & 0x7F

        pdo = self._pdos.get(cob)
        if pdo is not None:
            self._decode_pdo(pdo, data, frame)
        elif cob == 0x000:
            self._decode_nmt(data, frame)
        elif cob == 0x080:
            frame.summary = f"SYNC counter={data[0]}" if data else "SYNC"
        elif cob == 0x100:
            frame.summary = "TIME"
        elif cob in (0x7E4, 0x7E5):
            frame.summary = "LSS response" if cob == 0x7E4 else "LSS request"
        elif node == 0:
            frame.summary = "Unknown"
        elif function == 0x080:
            self._decode_emcy(node, data, frame)
        elif function in _PDO_FUNCTIONS:
            frame.summary = f"{_PDO_FUNCTIONS[function]} node {node} (mapping unknown)"
        elif function == 0x580:
            self._decode_sdo(node, data, frame, from_server=True)
        elif function == 0x600:
            self._decode_sdo(node, data, frame, from_server=False)
        elif function == 0x700:
            self._decode_heartbeat(node, msg, frame)
        else:
            frame.summary = "Unknown"
        return frame

    def _decode_nmt(self, data: bytes, frame: DecodedFrame) -> None:
        if len(data) < 2:
            frame.summary = "NMT (short frame)"
            return
        target = "all nodes" if data[1] == 0 else f"node {data[1]}"
        frame.summary = f"NMT {NMT_COMMANDS.get(data[0], f'0x{data[0]:02X}')} -> {target}"

    def _decode_heartbeat(self, node_id: int, msg: can.Message, frame: DecodedFrame) -> None:
        if msg.is_remote_frame:
            frame.summary = f"Node guarding request node {node_id}"
            return
        if not msg.data:
            frame.summary = f"Heartbeat node {node_id} (empty)"
            return
        raw = msg.data[0]
        state = NMT_STATES.get(raw & 0x7F, f"0x{raw & 0x7F:02X}")
        node = self._nodes.setdefault(node_id, _Node())
        if raw == 0:
            node.last_heartbeat = None
            node.interval = None
            frame.summary = f"Boot-up node {node_id}"
        else:
            if node.last_heartbeat is not None:
                node.interval = msg.timestamp - node.last_heartbeat
            node.last_heartbeat = msg.timestamp
            frame.summary = f"Heartbeat node {node_id}: {state}"
        node.state = state
        frame.nodes.append(
            NodeInfo(
                address=node_id,
                state=state,
                name=self._device_name(node_id),
                details="EDS loaded" if node_id in self._ods else "",
                last_seen=msg.timestamp,
                heartbeat_interval=node.interval,
            )
        )

    def _decode_emcy(self, node_id: int, data: bytes, frame: DecodedFrame) -> None:
        if len(data) < 3:
            frame.summary = f"EMCY node {node_id} (short frame)"
            return
        code = int.from_bytes(data[:2], "little")
        register = data[2]
        bits = [name for i, name in enumerate(ERROR_REGISTER_BITS) if register & (1 << i)]
        desc = emcy_description(code)
        frame.summary = f"EMCY node {node_id}: 0x{code:04X} {desc}"
        active = self._emcy.setdefault(node_id, {})
        if code == 0:
            active.clear()
        else:
            active[code] = Fault(
                code=f"0x{code:04X}",
                description=desc,
                detail=f"ER=0x{register:02X} ({', '.join(bits) or 'none'}) mfr={data[3:].hex(' ')}",
            )
        frame.faults.append(FaultSet(f"Node {node_id} EMCY", tuple(active.values())))

    # -- SDO -------------------------------------------------------------

    def _decode_sdo(self, node: int, data: bytes, frame: DecodedFrame, from_server: bool) -> None:
        if len(data) < 8:
            frame.summary = f"SDO node {node} (short frame)"
            return
        cs = data[0] >> 5
        mux = (int.from_bytes(data[1:3], "little"), data[3])
        where = f"0x{mux[0]:04X}:{mux[1]:02X}"
        direction = "rsp" if from_server else "req"

        if cs == 4:  # abort, either direction
            code = int.from_bytes(data[4:8], "little")
            self._sdo.pop(node, None)
            text = SDO_ABORT_CODES.get(code, "Unknown abort code")
            frame.summary = f"SDO abort node {node} {where}: 0x{code:08X} {text}"
            frame.signals.append(SignalValue(f"SDO node {node}", self._var_label(node, *mux), f"ABORT: {text}", node=node))
            return
        if cs in (5, 6):
            frame.summary = f"SDO {direction} node {node} block transfer (not reassembled)"
            return

        if not from_server:
            if cs == 1:  # initiate download (write)
                expedited, size_set = bool(data[0] & 2), bool(data[0] & 1)
                transfer = _SdoTransfer(*mux, upload=False)
                if expedited:
                    n = (data[0] >> 2) & 3 if size_set else 0
                    transfer.data[:] = data[4 : 8 - n]
                    transfer.expedited_pending = True
                    value = self._format_value(node, *mux, bytes(transfer.data))
                    frame.summary = f"SDO write node {node} {where} = {value}"
                else:
                    transfer.size = int.from_bytes(data[4:8], "little") if size_set else None
                    frame.summary = f"SDO write node {node} {where} segmented ({transfer.size} bytes)"
                self._sdo[node] = transfer
            elif cs == 2:  # initiate upload (read)
                self._sdo[node] = _SdoTransfer(*mux, upload=True)
                frame.summary = f"SDO read node {node} {where}"
            elif cs == 0:  # download segment
                self._sdo_segment(node, data, frame, upload=False)
            elif cs == 3:
                frame.summary = f"SDO upload segment request node {node}"
            return

        if cs == 2:  # initiate upload response
            transfer = self._sdo.get(node) or _SdoTransfer(*mux, upload=True)
            expedited, size_set = bool(data[0] & 2), bool(data[0] & 1)
            if expedited:
                n = (data[0] >> 2) & 3 if size_set else 0
                transfer.data[:] = data[4 : 8 - n]
                self._sdo.pop(node, None)
                self._complete_sdo(node, transfer, frame)
            else:
                transfer.size = int.from_bytes(data[4:8], "little") if size_set else None
                self._sdo[node] = transfer
                frame.summary = f"SDO read node {node} {where} segmented ({transfer.size} bytes)"
        elif cs == 3:  # initiate download response
            transfer = self._sdo.get(node)
            if transfer is not None and transfer.expedited_pending:
                self._sdo.pop(node)
                self._complete_sdo(node, transfer, frame)
            else:
                frame.summary = f"SDO write ack node {node} {where}"
        elif cs == 0:  # upload segment
            self._sdo_segment(node, data, frame, upload=True)
        elif cs == 1:
            frame.summary = f"SDO download segment ack node {node}"

    def _sdo_segment(self, node: int, data: bytes, frame: DecodedFrame, upload: bool) -> None:
        transfer = self._sdo.get(node)
        if transfer is None or transfer.upload != upload:
            frame.summary = f"SDO segment node {node} (no transfer in progress)"
            return
        n = (data[0] >> 1) & 7
        transfer.data.extend(data[1 : 8 - n])
        if data[0] & 1:  # last segment
            self._sdo.pop(node)
            self._complete_sdo(node, transfer, frame)
        else:
            frame.summary = f"SDO segment node {node} ({len(transfer.data)} bytes so far)"

    def _complete_sdo(self, node: int, transfer: _SdoTransfer, frame: DecodedFrame) -> None:
        raw = bytes(transfer.data)
        if transfer.size is not None:
            raw = raw[: transfer.size]
        mux = (transfer.index, transfer.subindex)
        value = self._decode_value(node, *mux, raw)
        verb = "read" if transfer.upload else "write"
        frame.summary = f"SDO {verb} node {node} 0x{mux[0]:04X}:{mux[1]:02X} = {_fmt(value)}"
        var = self._variable(node, *mux)
        frame.signals.append(
            SignalValue(
                f"SDO node {node}", self._var_label(node, *mux), value, var.unit if var else "", node,
                int.from_bytes(raw, "little") if 0 < len(raw) <= 8 else None,
                mux,
            )
        )
        if transfer.index in _PDO_PARAM_RANGE and raw:
            self._learned.setdefault(node, {})[mux] = int.from_bytes(raw[:4], "little")
            self._rebuild_pdos()

    # -- PDO -------------------------------------------------------------

    def _rebuild_pdos(self) -> None:
        pdos: dict[int, _Pdo] = {}
        for node in set(self._ods) | set(self._learned):
            for comm_base, map_base, prefix, defaults in (
                (0x1800, 0x1A00, "TPDO", (0x180, 0x280, 0x380, 0x480)),
                (0x1400, 0x1600, "RPDO", (0x200, 0x300, 0x400, 0x500)),
            ):
                for i in range(512):
                    cob = self._param(node, comm_base + i, 1)
                    count = self._param(node, map_base + i, 0)
                    if count is None:
                        continue
                    if cob is None:
                        if i >= 4:
                            continue
                        cob = defaults[i] + node
                    if cob & 0x80000000 or cob & 0x20000000:  # invalid or 29-bit
                        continue
                    entries = []
                    for sub in range(1, min(count, 64) + 1):
                        value = self._param(node, map_base + i, sub)
                        if value:
                            entries.append((value >> 16, (value >> 8) & 0xFF, value & 0xFF))
                    if entries:
                        pdos[cob & 0x7FF] = _Pdo(node, f"{prefix}{i + 1}", entries)
        self._pdos = pdos

    def _param(self, node: int, index: int, subindex: int) -> int | None:
        learned = self._learned.get(node, {}).get((index, subindex))
        if learned is not None:
            return learned
        var = self._variable(node, index, subindex)
        if var is None:
            return None
        value = var.value if var.value is not None else var.default
        return value if isinstance(value, int) else None

    def _decode_pdo(self, pdo: _Pdo, data: bytes, frame: DecodedFrame) -> None:
        bits_total = sum(bits for _, _, bits in pdo.entries)
        available = len(data) * 8
        payload = int.from_bytes(data, "little")
        offset = 0
        parts = []
        for index, sub, bits in pdo.entries:
            if offset + bits > available:
                break
            raw = (payload >> offset) & ((1 << bits) - 1)
            offset += bits
            if index < 0x20:  # dummy mapping entry (data type index)
                continue
            var = self._variable(pdo.node, index, sub)
            value = _decode_bits(var, raw, bits)
            label = self._var_label(pdo.node, index, sub)
            frame.signals.append(
                SignalValue(pdo.name, label, value, var.unit if var else "", pdo.node, raw, (index, sub))
            )
            parts.append(f"{label}={_fmt(value)}")
        frame.summary = f"{pdo.name} node {pdo.node}: " + ", ".join(parts)
        if available < bits_total:
            frame.summary += f"  [short: {len(data)} of {(bits_total + 7) // 8} bytes]"

    # -- helpers -----------------------------------------------------------

    def _variable(self, node: int, index: int, subindex: int) -> ODVariable | None:
        od = self._ods.get(node)
        if od is None:
            return None
        try:
            return od.get_variable(index, subindex)
        except (KeyError, TypeError):
            return None

    def _var_label(self, node: int, index: int, subindex: int) -> str:
        var = self._variable(node, index, subindex)
        if var is not None:
            return var.qualname
        return f"0x{index:04X}:{subindex:02X}"

    def _decode_value(self, node: int, index: int, subindex: int, raw: bytes):
        var = self._variable(node, index, subindex)
        if var is not None:
            try:
                value = var.decode_raw(raw)
                if isinstance(value, int) and index in _PDO_PARAM_RANGE and len(raw) == 4:
                    return f"0x{value:08X}"
                if isinstance(value, int):
                    return _describe(var, value)
                return value if not isinstance(value, (bytes, bytearray)) else raw.hex(" ")
            except Exception:  # noqa: BLE001 - EDS type doesn't match the data
                pass
        if 0 < len(raw) <= 4:
            value = int.from_bytes(raw, "little")
            # COB-IDs and mapping entries are far easier to read in hex.
            return f"0x{value:08X}" if index in _PDO_PARAM_RANGE and len(raw) == 4 else value
        return raw.hex(" ")

    def _format_value(self, node: int, index: int, subindex: int, raw: bytes) -> str:
        return _fmt(self._decode_value(node, index, subindex, raw))

    def _device_name(self, node: int) -> str:
        od = self._ods.get(node)
        return (od.device_information.product_name or "") if od else ""


def _decode_bits(var: ODVariable | None, raw: int, bits: int):
    if var is None:
        return raw
    if var.data_type in SIGNED_TYPES and raw & (1 << (bits - 1)):
        raw -= 1 << bits
        return _describe(var, raw)
    if bits % 8 == 0 and var.fixed_size and len(var) == bits:
        try:
            value = var.decode_raw(raw.to_bytes(bits // 8, "little", signed=False))
        except Exception:  # noqa: BLE001
            return raw
        return _describe(var, value) if isinstance(value, int) else value
    return _describe(var, raw)


def _describe(var: ODVariable, value: int):
    if value in var.value_descriptions:
        return f"{var.value_descriptions[value]} ({value})"
    if var.factor != 1:
        return var.decode_phys(value)
    return value


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)
