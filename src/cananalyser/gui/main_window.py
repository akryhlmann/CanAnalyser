from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import can
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from cananalyser.bus import BITRATES, BusConfig, Monitor
from cananalyser.decoders import CANopenDecoder, J1939Decoder
from cananalyser.decoders.canopen import (
    NMT_PRE_OPERATIONAL,
    NMT_RESET_COMMUNICATION,
    NMT_RESET_NODE,
    NMT_START,
    NMT_STOP,
    nmt_frame,
)
from cananalyser.gui.models import FaultModel, NodeModel, OverviewModel, SignalModel, TraceFilter, TraceModel
from cananalyser.model import DecodedFrame
from cananalyser.sim import SIMULATORS

J1939 = "J1939"
CANOPEN = "CANopen"
SIM_CHANNEL = "cananalyser-sim"
LOG_FILTER = "Vector BLF (*.blf);;Vector ASC (*.asc);;candump log (*.log);;PEAK TRC (*.trc);;CSV (*.csv)"


def kvaser_channels() -> list[tuple[str, int]]:
    """List Kvaser channels via CANlib; empty when the driver isn't installed."""
    try:
        configs = can.detect_available_configs(interfaces=["kvaser"])
    except Exception:  # noqa: BLE001 - missing canlib32.dll etc.
        return []
    channels = []
    for c in configs:
        name = c.get("device_name") or "Kvaser"
        label = f"{c['channel']}: {name}"
        if c.get("serial"):
            label += f" S/N {c['serial']}"
        if c.get("dongle_channel"):
            label += f" ch {c['dongle_channel']}"
        channels.append((label, int(c["channel"])))
    # Physical devices first; CANlib's virtual channels look connected but carry no traffic.
    return sorted(channels, key=lambda ch: "virtual" in ch[0].lower())


def frame_bits(msg: can.Message) -> int:
    """Approximate on-wire bits incl. typical bit stuffing, for bus load."""
    base = 67 if msg.is_extended_id else 47
    return int((base + 8 * len(msg.data)) * 1.1)


def make_table(model, stretch_last: bool = True) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(20)
    view.horizontalHeader().setStretchLastSection(stretch_last)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    return view


class MainWindow(QMainWindow):
    REFRESH_MS = 100

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CanAnalyser - J1939 / CANopen monitor")
        self.resize(1300, 800)

        self.j1939 = J1939Decoder()
        self.canopen = CANopenDecoder()
        self.monitor = Monitor()
        self.simulator = None
        self.total_frames = 0
        self._load_window: deque[tuple[float, int]] = deque()
        self._connected_at: float | None = None
        self._hw_stats = None
        self._hw_stats_at = 0.0
        self._loaded_files: list[str] = []

        self.trace = TraceModel()
        self.trace_filter = TraceFilter()
        self.trace_filter.setSourceModel(self.trace)
        self.overview = OverviewModel()
        self.signals = SignalModel()
        self.nodes = NodeModel()
        self.faults = FaultModel()

        self._build_toolbar()
        self._build_tabs()
        self._update_controls()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(self.REFRESH_MS)

    # -- layout ------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Connection")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.protocol = QComboBox()
        self.protocol.addItems([J1939, CANOPEN])
        self.protocol.currentTextChanged.connect(self._update_controls)
        bar.addWidget(QLabel(" Protocol "))
        bar.addWidget(self.protocol)

        self.source = QComboBox()
        for label, channel in kvaser_channels() or [("Kvaser ch 0", 0), ("Kvaser ch 1", 1)]:
            self.source.addItem(label, ("kvaser", channel))
        self.source.addItem("Simulator", ("sim", SIM_CHANNEL))
        bar.addWidget(QLabel("  Interface "))
        bar.addWidget(self.source)

        self.bitrate = QComboBox()
        for rate in BITRATES:
            self.bitrate.addItem(f"{rate // 1000} kbit/s", rate)
        self.bitrate.setCurrentIndex(BITRATES.index(250_000))
        bar.addWidget(QLabel("  Bitrate "))
        bar.addWidget(self.bitrate)

        self.listen_only = QCheckBox("Listen only")
        self.listen_only.setToolTip(
            "Silent mode: the interface never transmits, not even ACKs.\n"
            "Only use it on a network that has other nodes to ACK frames (e.g. a vehicle).\n"
            "With a single device under test nothing will be received in this mode."
        )
        self.listen_only.toggled.connect(self._update_controls)
        bar.addWidget(self.listen_only)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        bar.addWidget(self.connect_btn)
        bar.addSeparator()

        self.replay_act = bar.addAction("Replay log…", self._replay)
        self.record_act = bar.addAction("Record…", self._toggle_recording)
        self.record_act.setCheckable(True)
        bar.addSeparator()
        self.load_act = bar.addAction("Load DBC…", self._load_database)
        bar.addAction("Clear", self._clear)

    def _build_tabs(self) -> None:
        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        trace_page = QWidget()
        layout = QVBoxLayout(trace_page)
        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by ID or decoded text, e.g. EEC1, SA=0x00, node 5, 18FECA")
        self.filter_edit.textChanged.connect(self.trace_filter.set_text)
        self.pause = QCheckBox("Pause")
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        row.addWidget(self.filter_edit)
        row.addWidget(self.pause)
        row.addWidget(self.autoscroll)
        layout.addLayout(row)
        self.trace_view = make_table(self.trace_filter)
        for col, width in enumerate((90, 35, 80, 35, 190)):
            self.trace_view.setColumnWidth(col, width)
        layout.addWidget(self.trace_view)
        tabs.addTab(trace_page, "Trace")

        overview_view = make_table(self.overview)
        for col, width in enumerate((80, 70, 80, 35, 190)):
            overview_view.setColumnWidth(col, width)
        tabs.addTab(overview_view, "Messages")

        signal_view = make_table(self.signals)
        for col, width in enumerate((160, 260, 160, 60, 80)):
            signal_view.setColumnWidth(col, width)
        tabs.addTab(signal_view, "Signals")

        nodes_page = QWidget()
        layout = QVBoxLayout(nodes_page)
        node_view = make_table(self.nodes)
        for col, width in enumerate((90, 160, 260, 120, 100)):
            node_view.setColumnWidth(col, width)
        layout.addWidget(node_view)
        layout.addWidget(self._build_nmt_panel())
        tabs.addTab(nodes_page, "Nodes")

        fault_view = make_table(self.faults)
        for col, width in enumerate((140, 120, 420)):
            fault_view.setColumnWidth(col, width)
        self.tabs = tabs
        self.fault_tab = tabs.addTab(fault_view, "Faults")

    def _build_nmt_panel(self) -> QGroupBox:
        self.nmt_box = QGroupBox("CANopen NMT")
        row = QHBoxLayout(self.nmt_box)
        self.nmt_node = QSpinBox()
        self.nmt_node.setRange(0, 127)
        self.nmt_node.setSpecialValueText("All nodes")
        row.addWidget(QLabel("Target node"))
        row.addWidget(self.nmt_node)
        for label, command in (
            ("Start (operational)", NMT_START),
            ("Stop", NMT_STOP),
            ("Pre-operational", NMT_PRE_OPERATIONAL),
            ("Reset node", NMT_RESET_NODE),
            ("Reset communication", NMT_RESET_COMMUNICATION),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, c=command: self._send_nmt(c))
            row.addWidget(btn)
        row.addStretch()
        return self.nmt_box

    # -- actions -------------------------------------------------------------

    @property
    def decoder(self):
        return self.j1939 if self.protocol.currentText() == J1939 else self.canopen

    def _toggle_connection(self) -> None:
        if self.monitor.running:
            self._disconnect()
            return
        kind, channel = self.source.currentData()
        self.monitor.decoder = self.decoder
        try:
            if kind == "sim":
                self.simulator = SIMULATORS[self.protocol.currentText().lower()](channel)
                self.monitor.start_bus(BusConfig("virtual", channel, listen_only=self.listen_only.isChecked()))
                self.simulator.start()
            else:
                config = BusConfig(kind, channel, self.bitrate.currentData(), self.listen_only.isChecked())
                self.monitor.start_bus(config)
        except Exception as exc:  # noqa: BLE001 - show driver errors to the user
            self._disconnect()
            QMessageBox.critical(self, "Connect failed", f"Could not open {self.source.currentText()}:\n{exc}")
            return
        self._connected_at = time.monotonic()
        self._update_controls()

    def _disconnect(self) -> None:
        self._connected_at = None
        self._hw_stats = None
        self.monitor.stop()
        if self.simulator is not None:
            self.simulator.stop()
            self.simulator = None
        self.record_act.setChecked(False)
        self._update_controls()

    def _replay(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Replay log file", "", f"CAN logs (*.blf *.asc *.log *.trc *.csv);;{LOG_FILTER}")
        if not path:
            return
        self._disconnect()
        self.monitor.decoder = self.decoder
        try:
            self.monitor.start_replay(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Replay failed", str(exc))
        self._update_controls()

    def _toggle_recording(self, checked: bool) -> None:
        if not checked:
            self.monitor.stop_logging()
            return
        path, _ = QFileDialog.getSaveFileName(self, "Record to", time.strftime("capture_%Y%m%d_%H%M%S.blf"), LOG_FILTER)
        if not path:
            self.record_act.setChecked(False)
            return
        try:
            self.monitor.start_logging(path)
        except Exception as exc:  # noqa: BLE001
            self.record_act.setChecked(False)
            QMessageBox.critical(self, "Recording failed", str(exc))

    def _load_database(self) -> None:
        if self.protocol.currentText() == J1939:
            path, _ = QFileDialog.getOpenFileName(self, "Load J1939 DBC", "", "DBC files (*.dbc)")
            if not path:
                return
            try:
                self.j1939.load_dbc(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "DBC error", str(exc))
                return
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Load EDS/DCF", "", "Device description (*.eds *.dcf)")
            if not path:
                return
            node_id, ok = QInputDialog.getInt(self, "Node ID", f"Node ID for {Path(path).name}:", 1, 1, 127)
            if not ok:
                return
            try:
                self.canopen.load_eds(path, node_id)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "EDS error", str(exc))
                return
        self._loaded_files.append(Path(path).name)
        self.statusBar().showMessage(f"Loaded {Path(path).name}", 5000)

    def _send_nmt(self, command: int) -> None:
        try:
            self.monitor.send(nmt_frame(command, self.nmt_node.value()))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "NMT", str(exc))

    def _clear(self) -> None:
        for model in (self.trace, self.overview, self.signals, self.nodes, self.faults):
            model.clear()
        self.total_frames = 0

    def _update_controls(self) -> None:
        running = self.monitor.running
        canopen = self.protocol.currentText() == CANOPEN
        for widget in (self.protocol, self.source, self.bitrate, self.listen_only):
            widget.setEnabled(not running)
        self.connect_btn.setText("Disconnect" if running else "Connect")
        self.record_act.setEnabled(running)
        self.load_act.setText("Load EDS…" if canopen else "Load DBC…")
        self.nmt_box.setVisible(canopen)
        self.nmt_box.setEnabled(running and self.monitor.can_transmit and not self.listen_only.isChecked())

    # -- periodic refresh -------------------------------------------------------

    def _refresh(self) -> None:
        frames = self.monitor.drain()
        if frames:
            self._ingest(frames)
        elif self.nodes.rowCount():
            self.nodes._refresh_all()  # keep "last seen" ages ticking
        if self.monitor.error:
            error = self.monitor.error
            self._disconnect()
            QMessageBox.critical(self, "CAN error", error)
        self._update_status()

    def _ingest(self, frames: list[DecodedFrame]) -> None:
        self.total_frames += len(frames)
        now = time.monotonic()
        self._load_window.append((now, sum(frame_bits(f.msg) for f in frames)))
        if not self.pause.isChecked():
            self.trace.append(frames)
            if self.autoscroll.isChecked():
                self.trace_view.scrollToBottom()
        self.overview.update(frames)
        self.signals.update(frames)
        self.nodes.update(frames)
        self.faults.update(frames)
        count = self.faults.active_count
        self.tabs.setTabText(self.fault_tab, f"Faults ({count})" if count else "Faults")

    def _update_status(self) -> None:
        now = time.monotonic()
        while self._load_window and now - self._load_window[0][0] > 1.0:
            self._load_window.popleft()
        bits = sum(b for _, b in self._load_window)
        load = 100.0 * bits / self.bitrate.currentData() if self.monitor.running else 0.0
        error_frames = self.monitor.error_frames
        if self.monitor.running and now - self._hw_stats_at >= 1.0:
            self._hw_stats_at = now
            self._hw_stats = self.monitor.bus_statistics()
        if self._hw_stats is not None:
            load = self._hw_stats.bus_load / 100.0
            error_frames = max(error_frames, self._hw_stats.err_frame)
        parts = [
            "Connected" if self.monitor.running else "Disconnected",
            f"frames {self.total_frames}",
            f"bus load {load:.0f}%",
        ]
        if error_frames:
            parts.append(f"error frames {error_frames}")
        if self._hw_stats is not None and self._hw_stats.overruns:
            parts.append(f"overruns {self._hw_stats.overruns}")
        hint = self._no_traffic_hint(now, error_frames)
        if hint:
            parts.append(hint)
        if self.monitor.logging:
            parts.append("● recording")
        if self.monitor.dropped:
            parts.append(f"dropped {self.monitor.dropped}")
        if self._loaded_files:
            parts.append("files: " + ", ".join(self._loaded_files))
        self.statusBar().showMessage("   |   ".join(parts))

    def _no_traffic_hint(self, now: float, error_frames: int) -> str:
        if self._connected_at is None or now - self._connected_at < 3.0:
            return ""
        if self.total_frames > error_frames:
            return ""
        if "virtual" in self.source.currentText().lower():
            return "⚠ this is a Kvaser VIRTUAL channel (no hardware); CANlib does not see the USBcan, check drivers"
        if error_frames:
            return "⚠ only error frames: check the bitrate"
        if self.listen_only.isChecked():
            return "⚠ no frames: listen-only gets nothing if no other node ACKs; turn it off"
        # A wrong bitrate produces error frames; a completely silent line usually
        # means the device is wired to a different channel (USBcan 2xHS has two).
        return "⚠ no frames or errors on this channel: try the other Kvaser channel, then check wiring/termination"

    def closeEvent(self, event) -> None:  # noqa: N802
        self._disconnect()
        super().closeEvent(event)
