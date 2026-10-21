import struct

from cananalyser.decoders.j1939 import J1939Decoder, J1939Id, J1939Name, parse_dtcs
from cananalyser.sim import j1939_id
from conftest import frame


def test_id_pdu2_and_pdu1():
    eec1 = J1939Id.parse(0x0CF00400)
    assert (eec1.priority, eec1.pgn, eec1.sa, eec1.da) == (3, 61444, 0x00, 0xFF)
    request = J1939Id.parse(0x18EA00F9)  # request from 0xF9 to 0x00
    assert (request.pgn, request.sa, request.da) == (59904, 0xF9, 0x00)


def test_id_roundtrip_with_sim_helper():
    for pgn, da in ((61444, 0xFF), (59904, 0x17), (0x1FEF1, 0xFF)):
        jid = J1939Id.parse(j1939_id(6, pgn, 0x21, da))
        assert (jid.pgn, jid.sa, jid.da) == (pgn, 0x21, da)


def test_dbc_signals_and_not_available(examples):
    dec = J1939Decoder(examples / "sim_j1939.dbc")
    data = bytes([0xF3, 0x7D, 165]) + struct.pack("<H", 1500 * 8) + b"\xff\xff\xff"
    out = dec.decode(frame(0x0CF00400, data))
    values = {s.name: s.value for s in out.signals}
    assert values["EngineSpeed"] == 1500
    assert values["ActualEnginePercentTorque"] == 40
    assert out.summary.startswith("EEC1")

    ccvs = bytes([0xFF, 0xFF, 0xFF]) + b"\xff" * 5  # everything "not available"
    out = dec.decode(frame(0x18FEF100, ccvs))
    values = {s.name: s.value for s in out.signals}
    assert values["WheelBasedVehicleSpeed"] == "n/a"
    assert values["ParkingBrakeSwitch"] == "n/a"


def test_unknown_pgn_uses_builtin_name():
    out = J1939Decoder().decode(frame(0x18FEE500, bytes(8)))
    assert out.summary.startswith("HOURS")
    assert out.signals == []


def test_address_claim_creates_node():
    name = (1 << 63) | (123 << 21) | 4711
    out = J1939Decoder().decode(frame(0x18EEFF00, name.to_bytes(8, "little")))
    assert out.nodes[0].state == "claimed"
    assert "mfr 123" in out.nodes[0].name
    parsed = J1939Name.parse(name.to_bytes(8, "little"))
    assert parsed.identity == 4711 and parsed.arbitrary_address_capable


def test_bam_reassembly_of_dm1():
    dec = J1939Decoder()
    payload = bytes([0x04, 0xFF]) + struct.pack("<I", 110 | (15 << 16) | (3 << 24)) + struct.pack(
        "<I", 190 | (2 << 16) | (1 << 24)
    )
    cm = bytes([32, len(payload), 0, 2, 0xFF]) + (65226).to_bytes(3, "little")
    dec.decode(frame(0x1CECFF00, cm, ts=1.0))
    out1 = dec.decode(frame(0x1CEBFF00, bytes([1]) + payload[:7], ts=1.05))
    assert out1.faults == []
    out2 = dec.decode(frame(0x1CEBFF00, bytes([2]) + payload[7:].ljust(7, b"\xff"), ts=1.10))
    assert "complete" in out2.summary
    (faults,) = out2.faults
    assert faults.source == "SA 0x00 DM1"
    assert [f.code for f in faults.faults] == ["SPN 110 FMI 15", "SPN 190 FMI 2"]


def test_rts_cts_and_session_timeout():
    dec = J1939Decoder()
    payload = bytes(range(20))
    rts = bytes([16, 20, 0, 3, 3]) + (65259).to_bytes(3, "little")
    dec.decode(frame(0x1CEC1700, rts, ts=0.0))
    for seq in (1, 2):
        dec.decode(frame(0x1CEB1700, bytes([seq]) + payload[(seq - 1) * 7 : seq * 7], ts=0.1))
    # Third packet arrives far too late: the session was dropped.
    late = dec.decode(frame(0x1CEB1700, bytes([3]) + payload[14:].ljust(7, b"\xff"), ts=10.0))
    assert "no session" in late.summary


def test_dtc_placeholder_ignored():
    lamps, dtcs = parse_dtcs(bytes([0, 0xFF, 0, 0, 0, 0, 0xFF, 0xFF]))
    assert dtcs == [] and lamps["MIL"] == 0


def test_dm1_without_faults_clears():
    out = J1939Decoder().decode(frame(0x18FECA00, bytes([0, 0xFF, 0, 0, 0, 0, 0xFF, 0xFF])))
    assert out.faults[0].faults == ()
