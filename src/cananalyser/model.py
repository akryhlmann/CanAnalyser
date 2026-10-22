"""Protocol-independent result types produced by the decoders.

Decoders run on the acquisition thread and hand these immutable records to
the UI, so the UI never touches decoder state directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import can


@dataclass(frozen=True)
class SignalValue:
    """One decoded value, e.g. a DBC signal or a CANopen object."""

    source: str  # message / object the value came from, e.g. "EEC1" or "TPDO1"
    name: str
    value: Any
    unit: str = ""
    node: int | None = None
    raw: Any = None  # value as transmitted, before scaling / value tables
    obj: tuple[int, int] | None = None  # CANopen (index, subindex), if known


@dataclass(frozen=True)
class Fault:
    code: str  # e.g. "SPN 110 FMI 0" or "0x8130"
    description: str
    detail: str = ""


@dataclass(frozen=True)
class FaultSet:
    """The complete set of active faults for one source.

    Replaces any faults previously reported for ``source``; an empty
    ``faults`` tuple means the source reports no active faults.
    """

    source: str
    faults: tuple[Fault, ...]


@dataclass(frozen=True)
class NodeInfo:
    """Snapshot of what is known about one node on the bus."""

    address: int
    state: str = ""  # NMT state (CANopen) or "claimed" (J1939)
    name: str = ""  # device name / J1939 NAME summary
    details: str = ""
    last_seen: float = 0.0
    heartbeat_interval: float | None = None


@dataclass
class DecodedFrame:
    """A received (or transmitted) frame together with its interpretation."""

    msg: can.Message
    summary: str = ""
    signals: list[SignalValue] = field(default_factory=list)
    nodes: list[NodeInfo] = field(default_factory=list)
    faults: list[FaultSet] = field(default_factory=list)
    tx: bool = False
