import struct

import pytest

from cananalyser.decoders.canopen import CANopenDecoder, emcy_description, nmt_frame
from conftest import frame

NODE = 5


def sdo_write(dec, index, sub, value, size=4):
    cmd = 0x23 | ((4 - size) << 2)
    dec.decode(frame(0x600 + NODE, bytes([cmd]) + struct.pack("<HB", index, sub) + value.to_bytes(4, "little")))
    return dec.decode(frame(0x580 + NODE, bytes([0x60]) + struct.pack("<HB", index, sub) + bytes(4)))


def test_heartbeat_state_and_interval():
    dec = CANopenDecoder()
    assert dec.decode(frame(0x705, [0], ts=0.0)).nodes[0].state == "boot-up"
    dec.decode(frame(0x705, [0x7F], ts=1.0))
    out = dec.decode(frame(0x705, [0x05], ts=2.0))
    node = out.nodes[0]
    assert node.state == "operational"
    assert node.heartbeat_interval == pytest.approx(1.0)


def test_nmt_frame_and_decode():
    msg = nmt_frame(0x01, NODE)
    assert msg.arbitration_id == 0 and list(msg.data) == [1, NODE]
    assert "Start remote node -> node 5" in CANopenDecoder().decode(msg).summary
    with pytest.raises(ValueError):
        nmt_frame(0x55, 1)


def test_emcy_active_set_and_reset():
    dec = CANopenDecoder()
    out = dec.decode(frame(0x085, [0x10, 0x32, 0x04, 0, 0, 0, 0, 0]))
    (fs,) = out.faults
    assert fs.faults[0].code == "0x3210" and "Voltage" in fs.faults[0].description
    out = dec.decode(frame(0x085, bytes(8)))
    assert out.faults[0].faults == ()
    assert emcy_description(0x8130) == "Life guard or heartbeat error"


def test_pdo_from_eds(examples):
    dec = CANopenDecoder()
    dec.load_eds(examples / "sim_node.eds", NODE)
    out = dec.decode(frame(0x185, struct.pack("<Hh", 1500, -100)))
    values = {s.name: s.value for s in out.signals}
    assert values == {"Process data.Motor speed": 1500, "Process data.Motor current": -100}


def test_pdo_mapping_learned_from_sdo_overrides_eds(examples):
    dec = CANopenDecoder()
    dec.load_eds(examples / "sim_node.eds", NODE)
    sdo_write(dec, 0x1A00, 0, 0, size=1)
    sdo_write(dec, 0x1A00, 3, 0x60000308)
    sdo_write(dec, 0x1A00, 0, 3, size=1)
    out = dec.decode(frame(0x185, struct.pack("<HhB", 1500, -100, 2)))
    assert [s.name for s in out.signals][-1] == "Process data.Status"
    assert out.signals[-1].value == 2


def test_pdo_learned_without_eds():
    dec = CANopenDecoder()
    sdo_write(dec, 0x1A01, 0, 0, size=1)
    sdo_write(dec, 0x1A01, 1, 0x20000120)  # 32-bit object
    sdo_write(dec, 0x1A01, 0, 1, size=1)
    out = dec.decode(frame(0x285, struct.pack("<I", 123456)))
    assert out.summary.startswith("TPDO2 node 5")
    assert out.signals[0].name == "0x2000:01" and out.signals[0].value == 123456


def test_unmapped_pdo():
    out = CANopenDecoder().decode(frame(0x186, bytes(4)))
    assert out.summary == "TPDO1 node 6 (mapping unknown)"


def test_sdo_expedited_read_with_eds(examples):
    dec = CANopenDecoder()
    dec.load_eds(examples / "sim_node.eds", NODE)
    dec.decode(frame(0x605, bytes([0x40, 0x17, 0x10, 0, 0, 0, 0, 0])))
    out = dec.decode(frame(0x585, bytes([0x4B, 0x17, 0x10, 0]) + struct.pack("<I", 1000)))
    assert out.signals[0].name == "Producer heartbeat time"
    assert out.signals[0].value == 1000


def test_sdo_segmented_upload(examples):
    dec = CANopenDecoder()
    dec.load_eds(examples / "sim_node.eds", NODE)
    text = b"Simulated node"
    dec.decode(frame(0x605, bytes([0x40, 0x08, 0x10, 0, 0, 0, 0, 0])))
    dec.decode(frame(0x585, bytes([0x41, 0x08, 0x10, 0]) + struct.pack("<I", len(text))))
    dec.decode(frame(0x605, bytes([0x60]) + bytes(7)))
    dec.decode(frame(0x585, bytes([0x00]) + text[:7]))
    dec.decode(frame(0x605, bytes([0x70]) + bytes(7)))
    last = text[7:]
    n = 7 - len(last)
    out = dec.decode(frame(0x585, bytes([0x10 | (n << 1) | 1]) + last.ljust(7, b"\0")))
    assert out.signals[0].value == "Simulated node"


def test_sdo_abort():
    dec = CANopenDecoder()
    out = dec.decode(frame(0x585, bytes([0x80, 0x00, 0x20, 1]) + struct.pack("<I", 0x06020000)))
    assert "does not exist" in out.summary


def test_extended_frame_is_not_canopen():
    assert "not CANopen" in CANopenDecoder().decode(frame(0x18FEF100, bytes(8))).summary
