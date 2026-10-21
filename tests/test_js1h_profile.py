import struct
import time
from pathlib import Path

import pytest

from cananalyser.bus import BusConfig, Monitor
from cananalyser.decoders import CANopenDecoder, J1939Decoder
from cananalyser.decoders.canopen import NMT_START, nmt_frame
from cananalyser.model import DecodedFrame, SignalValue
from cananalyser.profiles.js1h import BUTTON_NA, ERROR, OK, PRESSED, JS1HProfile
from cananalyser.sim import JS1HCANopenSimulator, JS1HJ1939Simulator
from cananalyser.trend import TrendStore
from conftest import frame

DB = Path(__file__).resolve().parent.parent / "databases" / "danfoss_js1h"
SA, NODE = 0x13, 0x10


@pytest.fixture
def j1939():
    return J1939Decoder(DB / "js1h_j1939.dbc")


@pytest.fixture
def canopen():
    dec = CANopenDecoder()
    dec.load_eds(DB / "js1h_can.eds", NODE)
    return dec


def test_j1939_signed_axes_buttons_and_errors(j1939):
    sim = JS1HJ1939Simulator("unused")
    buttons = [PRESSED, 0, 0, 0, 0, 0, 0, 0, 0, 0, BUTTON_NA, BUTTON_NA]
    profile = JS1HProfile()
    profile.update([
        j1939.decode(frame(0x0CFDD600 | SA, sim._bjm(-42.0, 0.5, buttons), ts=1.0)),
        j1939.decode(frame(0x0CFDD700 | SA, sim._ejm(80.0, None), ts=1.0)),
    ])
    (js,) = profile.joysticks.values()
    assert js.key == ("J1939", SA, "1")
    x, y = js.main_axes
    assert x.value == pytest.approx(-42.0) and x.status == OK and x.neutral is False
    assert y.value == pytest.approx(0.5) and y.neutral is True
    grips = dict(js.grips)
    assert grips["Grip X"].value == pytest.approx(80.0)
    assert grips["Grip Y"].status == ERROR and grips["Grip Y"].value is None
    assert js.buttons[1] == PRESSED and js.buttons[12] == BUTTON_NA
    derived = {sig.name: sig.value for _, sig in profile.take_derived()}
    assert derived == pytest.approx({"X (signed)": -42.0, "Y (signed)": 0.5, "Grip X (signed)": 80.0})


def test_message_sets_are_separate_joysticks(j1939):
    sim = JS1HJ1939Simulator("unused")
    profile = JS1HProfile()
    profile.update([
        j1939.decode(frame(0x0CFDD600 | SA, sim._bjm(10, 10, [0] * 12))),
        j1939.decode(frame(0x0CFDDA00 | SA, sim._bjm(20, 20, [0] * 12))),
    ])
    assert set(profile.joysticks) == {("J1939", SA, "1"), ("J1939", SA, "3")}


def test_canopen_axes_grips_buttons(canopen):
    sim = JS1HCANopenSimulator
    profile = JS1HProfile()
    pressed, enabled = 1 << 4, (1 << 10) - 1  # button 5 pressed, buttons 1-10 enabled
    tpdo1 = bytes([0]) + pressed.to_bytes(3, "little") + bytes([0]) + enabled.to_bytes(3, "little")
    profile.update([
        canopen.decode(frame(0x180 + NODE, tpdo1)),
        canopen.decode(frame(0x280 + NODE, sim._axes_pdo([-55.5, 1.0, 0], third_reserved=True))),
        canopen.decode(frame(0x380 + NODE, sim._axes_pdo([10.0, None, -30.0]))),
    ])
    (js,) = profile.joysticks.values()
    x, y = js.main_axes
    assert x.value == pytest.approx(-55.5) and x.neutral is False
    assert y.neutral is True
    grips = dict(js.grips)
    assert list(grips) == ["Grip 1", "Grip 2", "Grip 3"]
    assert grips["Grip 2"].status == ERROR
    assert grips["Grip 3"].value == pytest.approx(-30.0)
    assert js.buttons[5] == PRESSED and js.buttons[4] == 0
    assert js.enabled[10] is True and js.enabled[11] is False


def test_canopen_profile_matches_objects_not_names():
    sig = SignalValue("TPDO2", "Some other name", 12.3, "%", NODE, raw=123, obj=(0x6401, 1))
    profile = JS1HProfile()
    profile.update([DecodedFrame(frame(0x290, b""), signals=[sig])])
    assert profile.joysticks[("CANopen", NODE, "1")].axes["X"].value == pytest.approx(12.3)


def collect(monitor, seconds):
    end, frames = time.monotonic() + seconds, []
    while time.monotonic() < end:
        frames += monitor.drain()
        time.sleep(0.05)
    return frames


def test_canopen_simulator_sends_pdos_after_nmt_start(canopen, request):
    channel = f"js-{request.node.name}"
    sim, monitor = JS1HCANopenSimulator(channel), Monitor(canopen)
    monitor.start_bus(BusConfig("virtual", channel, listen_only=False))
    sim.start()
    try:
        before = collect(monitor, 0.3)
        monitor.send(nmt_frame(NMT_START, NODE))
        after = collect(monitor, 0.5)
    finally:
        sim.stop()
        monitor.stop()
    assert not any(f.summary.startswith("TPDO") for f in before)
    profile = JS1HProfile()
    profile.update(after)
    js = profile.joysticks[("CANopen", NODE, "1")]
    assert len(js.grips) == 3 and len(js.buttons) == 24


def test_trend_store_window():
    store = TrendStore()
    samples = [(t, SignalValue("S", "v", float(t), "%")) for t in range(100, 111)]
    assert store.add_samples(samples) == [("S", "v", None)]
    t, v = store.window(("S", "v", None), 3.0, store.latest_time())
    assert list(t) == [7.0, 8.0, 9.0, 10.0] and list(v) == [107.0, 108.0, 109.0, 110.0]
    # Text states fall back to the raw value; pure text is skipped.
    store.add_samples([(111, SignalValue("S", "state", "In neutral", raw=1)), (111, SignalValue("S", "txt", "x"))])
    assert ("S", "state", None) in store.keys() and ("S", "txt", None) not in store.keys()
    # J1939 error / not-available values are gaps, not their raw counts.
    store.add_samples([(112, SignalValue("S", "pos", "error", raw=1022))])
    _, v = store.window(("S", "pos", None), None, store.latest_time())
    assert len(v) == 1 and v[0] != v[0]  # NaN


def test_simulated_bjm_round_trips_through_dbc(j1939):
    sim = JS1HJ1939Simulator("unused")
    out = j1939.decode(frame(0x0CFDD600 | SA, sim._bjm(75.0, -100.0, [0] * 12)))
    values = {s.name: s.raw for s in out.signals}
    assert values["X_Position"] == 750 and values["X_LeverRightPosPosStatus"] == 1
    assert values["Y_Position"] == 1000 and values["Y_LeverBackNegPosStatus"] == 1


def test_axes_pdo_layout():
    data = JS1HCANopenSimulator._axes_pdo([1.0, -1.0, None])
    assert struct.unpack("<hhh", data[:6]) == (10, -10, 0)
    assert data[6] == 0b10_00_00  # third value missing -> error state
