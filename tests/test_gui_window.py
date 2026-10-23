import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cananalyser.gui.main_window import CANOPEN, J1939, MainWindow, database_kind  # noqa: E402
from conftest import frame  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "databases" / "danfoss_js1h"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow()
    yield w
    w.close()


def test_database_kind():
    assert database_kind("a/b/engine.DBC") == J1939
    assert database_kind("drive.eds") == CANOPEN
    assert database_kind("drive.dcf") == CANOPEN
    assert database_kind("notes.txt") is None


def test_load_eds_switches_protocol_when_disconnected(window):
    assert window.protocol.currentText() == J1939
    window.load_database_file(DB / "js1h_can.eds", 16)
    assert window.protocol.currentText() == CANOPEN
    assert window.canopen.object_dictionary(16) is not None
    assert "js1h_can.eds (node 16)" in window._loaded_files


def test_load_dbc_switches_back(window):
    window.protocol.setCurrentText(CANOPEN)
    window.load_database_file(DB / "js1h_j1939.dbc")
    assert window.protocol.currentText() == J1939
    out = window.j1939.decode(frame(0x0CFDD613, bytes(8)))
    assert out.summary.startswith("BJM1")


def test_unknown_file_type_is_rejected(window, tmp_path):
    with pytest.raises(ValueError):
        window.load_database_file(tmp_path / "notes.txt")


def test_default_node_id_from_bus(window):
    window.protocol.setCurrentText(CANOPEN)
    assert window._default_node_id() == 1
    window.canopen.reset()
    window.nodes.update([window.canopen.decode(frame(0x705, [0x7F], ts=1.0))])
    assert window._default_node_id() == 5
