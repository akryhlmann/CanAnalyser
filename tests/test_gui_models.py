import struct

import pytest
from PySide6.QtCore import Qt

from cananalyser.decoders import J1939Decoder
from cananalyser.gui.models import OverviewModel, SignalModel
from cananalyser.model import DecodedFrame
from conftest import frame


def cell(model, row, header):
    return model.data(model.index(row, model.headers.index(header)), Qt.DisplayRole)


def test_overview_period_statistics():
    model = OverviewModel()
    model.update([DecodedFrame(frame(0x123, b"\x01", ts=t)) for t in (0.0, 0.010, 0.030, 0.040)])
    assert cell(model, 0, "Count") == "4"
    assert cell(model, 0, "Last ms") == "10.0"
    assert cell(model, 0, "Min ms") == "10.0"
    assert cell(model, 0, "Max ms") == "20.0"
    assert cell(model, 0, "Avg ms") == "13.3"


def test_overview_single_frame_has_no_period():
    model = OverviewModel()
    model.update([DecodedFrame(frame(0x123, b"\x01", ts=1.0))])
    assert cell(model, 0, "Avg ms") == "" and cell(model, 0, "Max ms") == ""


def test_signal_raw_value_and_interval(examples):
    dec = J1939Decoder(examples / "sim_j1939.dbc")
    data = bytes([0xF3, 0x7D, 165]) + struct.pack("<H", 1500 * 8) + b"\xff\xff\xff"
    model = SignalModel()
    model.update([dec.decode(frame(0x0CF00400, data, ts=t)) for t in (0.0, 0.1)])
    row = next(r for r in range(model.rowCount()) if cell(model, r, "Signal") == "EngineSpeed")
    assert cell(model, row, "Value") == "1500"
    assert cell(model, row, "Raw") == "12000 (0x2EE0)"
    assert cell(model, row, "Interval ms") == "100.0"
