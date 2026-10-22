from pathlib import Path

import pytest

from cananalyser.decoders import J1939Decoder
from cananalyser.decoders.j1939 import invert
from cananalyser.redundancy import MISMATCH, OK, TIMEOUT, WAITING, RedundancyChecker, RedundancyConfig
from cananalyser.sim import JS1HSBWSimulator, j1939_id
from conftest import frame

DB = Path(__file__).resolve().parent.parent / "databases" / "danfoss_js1h" / "js1h_j1939.dbc"
P, S = 51, 52
BUTTONS = [0] * 10 + [3, 3]
sim = JS1HSBWSimulator("unused")
SAME = object()  # secondary value equal to the primary's


@pytest.fixture
def setup():
    checker = RedundancyChecker(RedundancyConfig(primary_sa=P, secondary_sa=S, debounce=3))
    decoder = J1939Decoder(DB)
    decoder.inverted = checker.inverted_sources
    return checker, decoder


def cycle(decoder, ts, x, y, x2=SAME, y2=SAME, buttons=BUTTONS, buttons2=None, send_secondary=True, invert_secondary=True):
    primary = decoder.decode(frame(j1939_id(3, 0xFDD6, P), sim._bjm(x, y, buttons), ts=ts))
    frames = [primary]
    if send_secondary:
        data = sim._bjm(x if x2 is SAME else x2, y if y2 is SAME else y2, buttons2 or buttons)
        if invert_secondary:
            data = invert(data)
        frames.append(decoder.decode(frame(j1939_id(3, 0xFDD6, S), data, ts=ts + 0.001)))
    return frames


def run(checker, decoder, n, **kw):
    for i in range(n):
        ts = 1.0 + i * 0.02
        checker.update(cycle(decoder, ts, **kw), ts + 0.001)


def test_inverted_secondary_decodes_like_primary(setup):
    _, decoder = setup
    p, s = cycle(decoder, 1.0, 40.0, -25.0)
    assert {x.name: x.raw for x in p.signals} == {x.name: x.raw for x in s.signals}
    assert "(inverted)" in s.summary


def test_small_differences_are_ok(setup):
    checker, decoder = setup
    run(checker, decoder, 10, x=40.0, y=-25.0, x2=40.8, y2=-24.5)
    assert checker.status == OK and checker.pairs == 10
    assert checker.checks["X position"].worst == "|Δ| 0.8 %"
    assert checker.fault_set().faults == ()
    assert any(sig.name == "BJM1 X Δ secondary−primary" for _, sig in checker.take_derived())


def test_position_mismatch_needs_debounce(setup):
    checker, decoder = setup
    run(checker, decoder, 2, x=40.0, y=0.0, x2=46.0)
    assert checker.status == OK and checker.checks["X position"].disagreements == 2
    run(checker, decoder, 1, x=40.0, y=0.0, x2=46.0)
    assert checker.status == MISMATCH
    (fault,) = checker.fault_set().faults
    assert fault.code == "REDUNDANCY X position"


def test_direction_matters(setup):
    checker, decoder = setup
    run(checker, decoder, 3, x=10.0, y=0.0, x2=-10.0)  # same magnitude, opposite side
    assert checker.checks["X position"].active


def test_button_disagreement(setup):
    checker, decoder = setup
    stuck = [1] + BUTTONS[1:]
    run(checker, decoder, 3, x=0.0, y=0.0, buttons2=stuck)
    assert checker.checks["Button1_PressedStatus"].active
    run(checker, decoder, 1, x=0.0, y=0.0)
    assert not checker.checks["Button1_PressedStatus"].active
    assert checker.checks["Button1_PressedStatus"].fault_events == 1


def test_secondary_not_inverted(setup):
    checker, decoder = setup
    run(checker, decoder, 3, x=30.0, y=30.0, invert_secondary=False)
    assert checker.checks["Inversion"].active
    assert checker.status == MISMATCH


def test_secondary_timeout_and_never_seen(setup):
    checker, decoder = setup
    run(checker, decoder, 5, x=0.0, y=0.0, send_secondary=False)
    checker.check_timeouts(1.15)  # 70 ms after the last primary, 150 ms after start
    assert checker.status == TIMEOUT
    assert "no frames" in checker.checks["Secondary channel"].current
    assert checker.checks["Primary channel"].active is False


def test_waiting_before_any_pair(setup):
    checker, _ = setup
    assert checker.status == WAITING


def test_both_sensor_errors_agree(setup):
    checker, decoder = setup
    run(checker, decoder, 3, x=None, y=0.0)  # sensor error (1022) on both channels
    assert not checker.checks["X position"].active
    run(checker, decoder, 3, x=10.0, y=0.0, x2=None)  # only the secondary reports an error
    assert checker.checks["X position"].active


def test_unpaired_frames_are_counted(setup):
    checker, decoder = setup
    p = decoder.decode(frame(j1939_id(3, 0xFDD6, P), sim._bjm(0, 0, BUTTONS), ts=1.0))
    s = decoder.decode(frame(j1939_id(3, 0xFDD6, S), invert(sim._bjm(0, 0, BUTTONS)), ts=1.05))  # 50 ms later
    checker.update([p, s], 1.05)
    assert checker.pairs == 0
    assert checker.channels["primary"].unpaired == 1
