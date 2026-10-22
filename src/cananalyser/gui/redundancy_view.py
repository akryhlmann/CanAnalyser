"""Redundancy tab: primary vs. inverted secondary channel of a joystick."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cananalyser.gui.joystick_view import BAD, GOOD, MUTED
from cananalyser.redundancy import MISMATCH, OK, TIMEOUT, RedundancyChecker, RedundancyConfig

STATUS_COLORS = {OK: GOOD, MISMATCH: BAD, TIMEOUT: BAD}


def address_box(value: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(0, 253)
    box.setValue(value)
    box.setDisplayIntegerBase(16)
    box.setPrefix("0x")
    box.setToolTip("J1939 source address (hex)")
    return box


class RedundancyView(QWidget):
    config_changed = Signal(object)  # RedundancyConfig

    HEADERS = ("Check", "Status", "Current (primary / secondary)", "Worst", "Disagreements", "Fault events")

    def __init__(self, checker: RedundancyChecker):
        super().__init__()
        self.checker = checker
        cfg = checker.config

        banner = QLabel("Diagnostic monitoring only — not a safety function. Do not use for safety decisions.")
        banner.setStyleSheet(f"color: {BAD.name()}; font-weight: bold;")

        self.primary = address_box(cfg.primary_sa)
        self.secondary = address_box(cfg.secondary_sa)
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.1, 50.0)
        self.tolerance.setSuffix(" %")
        self.tolerance.setValue(cfg.position_tolerance)
        self.debounce = QSpinBox()
        self.debounce.setRange(1, 100)
        self.debounce.setSuffix(" pairs")
        self.debounce.setValue(cfg.debounce)
        self.gap = QSpinBox()
        self.gap.setRange(1, 1000)
        self.gap.setSuffix(" ms")
        self.gap.setValue(round(cfg.max_pair_gap * 1000))
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 10000)
        self.timeout.setSuffix(" ms")
        self.timeout.setValue(round(cfg.timeout * 1000))
        apply = QPushButton("Apply and reset counters")
        apply.clicked.connect(self._apply)

        settings = QGroupBox(f"Redundant {cfg.message} channels (secondary sent bit-inverted)")
        form = QFormLayout(settings)
        form.addRow("Primary source address", self.primary)
        form.addRow("Secondary source address", self.secondary)
        form.addRow("Position tolerance", self.tolerance)
        form.addRow("Fault after", self.debounce)
        form.addRow("Max primary↔secondary gap", self.gap)
        form.addRow("Channel timeout", self.timeout)
        form.addRow(apply)

        self.status = QLabel()
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setMinimumWidth(240)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        status_layout.addWidget(self.status)
        status_layout.addWidget(self.summary)
        status_layout.addStretch()

        top = QHBoxLayout()
        top.addWidget(settings)
        top.addWidget(status_box, 1)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        # Interactive, sized once: ResizeToContents re-measures every cell on each update.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self._sized = False

        layout = QVBoxLayout(self)
        layout.addWidget(banner)
        layout.addLayout(top)
        layout.addWidget(self.table)
        self.refresh()

    def _apply(self) -> None:
        config = RedundancyConfig(
            primary_sa=self.primary.value(),
            secondary_sa=self.secondary.value(),
            position_tolerance=self.tolerance.value(),
            debounce=self.debounce.value(),
            max_pair_gap=self.gap.value() / 1000,
            timeout=self.timeout.value() / 1000,
        )
        self.config_changed.emit(config)

    def refresh(self) -> None:
        checker = self.checker
        status = checker.status
        color = STATUS_COLORS.get(status, MUTED)
        self.status.setText(status)
        self.status.setStyleSheet(
            f"font-size: 22pt; font-weight: bold; color: white; background: {color.name()}; padding: 8px;"
        )
        cfg = checker.config
        self.summary.setText(
            f"{checker.pairs} pairs compared · primary 0x{cfg.primary_sa:02X} ({cfg.primary_sa}) · "
            f"secondary 0x{cfg.secondary_sa:02X} ({cfg.secondary_sa})"
        )
        checks = list(checker.checks.values())
        if self.table.rowCount() != len(checks):
            self.table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            if check.active:
                state, brush = "FAULT", QBrush(BAD)
            elif check.run or check.disagreements:
                state, brush = "ok (had disagreements)" if not check.run else "disagreeing", QBrush(MUTED)
            else:
                state, brush = "ok", QBrush(GOOD)
            values = (check.name, state, check.current, check.worst, str(check.disagreements), str(check.fault_events))
            for col, text in enumerate(values):
                item = self.table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, col, item)
                if item.text() != text:
                    item.setText(text)
                    if col == 1:
                        item.setForeground(brush)
        if not self._sized and checker.pairs:
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(2, max(self.table.columnWidth(2), 300))
            self._sized = True
