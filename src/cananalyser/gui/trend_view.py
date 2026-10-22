"""Trend plot: selected signals over time."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from cananalyser.trend import SignalKey, TrendStore

WINDOWS = (("10 s", 10.0), ("30 s", 30.0), ("1 min", 60.0), ("5 min", 300.0), ("All", None))


def key_label(key: SignalKey, unit: str) -> str:
    source, name, node = key
    text = f"{source} · {name}"
    if unit:
        text += f" [{unit}]"
    if node is not None:
        text += f"  (0x{node:02X})"
    return text


class TrendView(QWidget):
    def __init__(self, store: TrendStore):
        super().__init__()
        self.store = store
        self.curves: dict[SignalKey, pg.PlotDataItem] = {}
        self._items: dict[SignalKey, QListWidgetItem] = {}
        self._color_index = 0

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter signals, e.g. Position, Grip, BJM1")
        self.filter.textChanged.connect(self._apply_filter)
        self.list = QListWidget()
        self.list.itemChanged.connect(self._item_toggled)
        clear = QPushButton("Uncheck all")
        clear.clicked.connect(self._uncheck_all)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Check signals to plot (numeric values; text states plot their raw value):"))
        left_layout.addWidget(self.filter)
        left_layout.addWidget(self.list)
        left_layout.addWidget(clear)

        self.window = QComboBox()
        for label, seconds in WINDOWS:
            self.window.addItem(label, seconds)
        self.window.setCurrentIndex(1)
        self.pause = QCheckBox("Pause")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Time window"))
        controls.addWidget(self.window)
        controls.addWidget(self.pause)
        controls.addStretch()

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.addLegend(offset=(10, 10))
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(controls)
        right_layout.addWidget(self.plot)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([330, 900])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

    # -- signal list ---------------------------------------------------------

    def add_keys(self, keys: list[SignalKey]) -> None:
        for key in keys:
            item = QListWidgetItem(key_label(key, self.store.units.get(key, "")))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, key)
            self._items[key] = item
            self.list.addItem(item)
        self._apply_filter(self.filter.text())

    def select(self, key: SignalKey) -> None:
        item = self._items.get(key)
        if item is not None:
            item.setCheckState(Qt.Checked)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for item in self._items.values():
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _item_toggled(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked and key not in self.curves:
            pen = pg.mkPen(pg.intColor(self._color_index, hues=9), width=2)
            self._color_index += 1
            self.curves[key] = self.plot.plot([], [], pen=pen, name=item.text(), connect="finite")
            self.refresh(force=True)
        elif item.checkState() != Qt.Checked and key in self.curves:
            self.plot.removeItem(self.curves.pop(key))

    def _uncheck_all(self) -> None:
        for item in self._items.values():
            item.setCheckState(Qt.Unchecked)

    def clear(self) -> None:
        for curve in self.curves.values():
            self.plot.removeItem(curve)
        self.curves.clear()
        self._items.clear()
        self.list.clear()

    # -- plotting ------------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        if (self.pause.isChecked() and not force) or not self.curves:
            return
        end = self.store.latest_time()
        seconds = self.window.currentData()
        for key, curve in self.curves.items():
            t, v = self.store.window(key, seconds, end)
            curve.setData(t, v)
        if seconds is not None:
            self.plot.setXRange(max(0.0, end - seconds), max(end, seconds), padding=0)
        else:
            self.plot.enableAutoRange(axis="x")
