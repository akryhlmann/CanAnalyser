import time

import can
import pytest

from cananalyser.bus import BusConfig, Monitor
from cananalyser.decoders import CANopenDecoder, J1939Decoder
from cananalyser.decoders.canopen import NMT_START, nmt_frame
from cananalyser.sim import CANopenSimulator, J1939Simulator


def collect(monitor, seconds, predicate):
    deadline = time.monotonic() + seconds
    seen = []
    while time.monotonic() < deadline:
        seen.extend(monitor.drain())
        if predicate(seen):
            break
        time.sleep(0.05)
    return seen


@pytest.fixture
def channel(request):
    return f"test-{request.node.name}"


def test_canopen_sim_responds_to_nmt_start(channel):
    sim = CANopenSimulator(channel)
    monitor = Monitor(CANopenDecoder())
    monitor.start_bus(BusConfig("virtual", channel, listen_only=False))
    sim.start()
    try:
        collect(monitor, 2, lambda fs: any(n.state == "pre-operational" for f in fs for n in f.nodes))
        monitor.send(nmt_frame(NMT_START, CANopenSimulator.NODE))
        frames = collect(monitor, 2, lambda fs: any(f.summary.startswith("TPDO1") for f in fs))
    finally:
        sim.stop()
        monitor.stop()
    assert any(f.tx and "Start remote node" in f.summary for f in frames)
    pdo = next(f for f in frames if f.summary.startswith("TPDO1"))
    # Mapping was learned from the simulated master's SDO writes (no EDS loaded).
    assert len(pdo.signals) == 3


def test_listen_only_refuses_to_send(channel):
    monitor = Monitor()
    monitor.start_bus(BusConfig("virtual", channel, listen_only=True))
    try:
        with pytest.raises(RuntimeError):
            monitor.send(nmt_frame(NMT_START))
    finally:
        monitor.stop()


def test_log_and_replay(channel, tmp_path, examples):
    log_file = tmp_path / "capture.blf"
    sim = J1939Simulator(channel)
    monitor = Monitor(J1939Decoder(examples / "sim_j1939.dbc"))
    monitor.start_bus(BusConfig("virtual", channel))
    monitor.start_logging(log_file)
    sim.start()
    try:
        collect(monitor, 3, lambda fs: any(f.faults for f in fs))
    finally:
        sim.stop()
        monitor.stop()

    replay = Monitor(J1939Decoder(examples / "sim_j1939.dbc"))
    replay.start_replay(log_file, realtime=False)
    frames = collect(replay, 3, lambda fs: any(f.faults for f in fs))
    replay.stop()
    assert any(s.name == "EngineSpeed" for f in frames for s in f.signals)
    dm1 = next(f for f in frames if f.faults)
    assert len(dm1.faults[0].faults) == 2
