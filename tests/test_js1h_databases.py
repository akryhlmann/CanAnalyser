"""Danfoss JS1-H databases decode frames laid out as in the technical information."""

import importlib.util
import struct
from pathlib import Path

import pytest

from cananalyser.decoders import CANopenDecoder, J1939Decoder
from conftest import frame

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "databases" / "danfoss_js1h"
NODE = 0x10


def load_generator():
    spec = importlib.util.spec_from_file_location("make_js1h", ROOT / "tools" / "make_js1h_databases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bits(*fields):
    """Pack (start_bit, length, value) fields LSB-first into 8 bytes."""
    value = 0
    for start, length, field in fields:
        value |= (field & ((1 << length) - 1)) << start
    return value.to_bytes(8, "little")


def test_generated_files_are_up_to_date():
    gen = load_generator()
    assert (DB / "js1h_j1939.dbc").read_text() == gen.make_dbc()
    assert (DB / "js1h_can.eds").read_text() == gen.make_eds("can")
    assert (DB / "js1h_canplus.eds").read_text() == gen.make_eds("canplus")


def test_bjm1_decodes():
    dec = J1939Decoder(DB / "js1h_j1939.dbc")
    data = bits(
        (0, 2, 0), (2, 2, 0), (4, 2, 1), (6, 10, 500),  # X right of neutral, 50 %
        (16, 2, 1), (18, 2, 0), (20, 2, 0), (22, 10, 0),  # Y in neutral
        (36, 2, 3), (38, 2, 3),  # detents not available
        (46, 2, 1),  # button 1 pressed (byte 5 bit 7)
        (40, 2, 0), (42, 2, 0), (44, 2, 0),
        (48, 16, 0xFFFF),  # buttons 5..12 not available
    )
    out = dec.decode(frame(0x0CFDD613, data))  # PGN 64982 from SA 0x13
    values = {s.name: s.value for s in out.signals}
    assert out.summary.startswith("BJM1")
    assert values["X_Position"] == pytest.approx(50.0)
    assert values["X_LeverRightPosPosStatus"] == "On positive side of neutral"
    assert values["Y_NeutralPosStatus"] == "In neutral position"
    assert values["Button1_PressedStatus"] == "Button pressed"
    assert values["Button12_PressedStatus"] == "n/a"
    assert values["X_DetentPosStatus"] == "n/a"


@pytest.mark.parametrize("raw, expected", [(1022, "error"), (1023, "n/a"), (1000, 100.0)])
def test_position_special_values(raw, expected):
    dec = J1939Decoder(DB / "js1h_j1939.dbc")
    out = dec.decode(frame(0x0CFDD713, bits((6, 10, raw))))  # EJM1
    assert {s.name: s.value for s in out.signals}["GripX_Position"] == expected


def test_dm1_uses_manual_spns():
    dec = J1939Decoder(DB / "js1h_j1939.dbc")
    dtc = struct.pack("<I", 2660 | (3 << 16) | (1 << 24))  # SPN 2660 FMI 3 (voltage high)
    out = dec.decode(frame(0x18FECA13, bytes([0x04, 0xFF]) + dtc + b"\xff\xff"))
    fault = out.faults[0].faults[0]
    assert fault.code == "SPN 2660 FMI 3"
    assert fault.description.startswith("X_Position")


@pytest.fixture(params=["can", "canplus"])
def canopen_dec(request):
    dec = CANopenDecoder()
    dec.load_eds(DB / f"js1h_{request.param}.eds", NODE)
    return request.param, dec


def test_tpdo2_axes(canopen_dec):
    _, dec = canopen_dec
    # X +50.0 %, Y -100.0 %, reserved, X out of neutral, Y neutral, reserved 2-bit.
    data = struct.pack("<hhH", 500, -1000, 0) + bytes([0b00_00_01])
    out = dec.decode(frame(0x280 + NODE, data))
    values = {s.name.split(".")[-1]: s.value for s in out.signals}
    assert values["X-Axis proportional"] == pytest.approx(50.0)
    assert values["Y-Axis proportional"] == pytest.approx(-100.0)
    assert values["X-Axis neutral state"] == 1
    assert values["Y-Axis neutral state"] == 0
    assert out.signals[0].unit == "%"


def test_tpdo1_buttons(canopen_dec):
    variant, dec = canopen_dec
    if variant == "can":
        # byte0 freeze, bytes1-3 buttons 1-24 pressed, byte4 reserved, bytes5-7 enabled 1-24
        data = bits((8, 1, 1), (31, 1, 1), (63, 1, 1))
        last_enabled = "Button 24 enabled"
    else:
        # byte0 freeze, buttons 1-12, 4 bit reserved, buttons 13-24, 4 bit reserved, enabled 1-12
        data = bits((8, 1, 1), (35, 1, 1), (51, 1, 1))[:7]
        last_enabled = "Button 12 enabled"
    out = dec.decode(frame(0x180 + NODE, data))
    values = {s.name.split(".")[-1]: s.value for s in out.signals}
    assert values["Button 1 pressed"] == 1
    assert values["Button 2 pressed"] == 0
    assert values["Button 24 pressed"] == 1
    assert values[last_enabled] == 1
    assert "short" not in out.summary


def test_canplus_tpdo4():
    dec = CANopenDecoder()
    dec.load_eds(DB / "js1h_canplus.eds", NODE)
    out = dec.decode(frame(0x480 + NODE, struct.pack("<hhhh", 100, 200, 300, -400)))
    assert [s.value for s in out.signals] == pytest.approx([10.0, 20.0, 30.0, -40.0])
    assert out.signals[0].name.endswith("Axis 5 (X100p11)")


def test_heartbeat_default_and_emcy():
    dec = CANopenDecoder()
    dec.load_eds(DB / "js1h_can.eds", NODE)
    out = dec.decode(frame(0x080 + NODE, bytes([0x10, 0x31, 0x04, 0x00, 0x80, 0, 0, 0])))
    assert out.faults[0].faults[0].code == "0x3110"
