"""Qt table models fed with batches of :class:`DecodedFrame`."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor

from cananalyser.model import DecodedFrame, FaultSet, NodeInfo, SignalValue

TX_BRUSH = QBrush(QColor(0, 110, 200))
ALERT_BRUSH = QBrush(QColor(200, 30, 30))


def fmt_id(msg) -> str:
    return f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"


def fmt_data(msg) -> str:
    if msg.is_remote_frame:
        return "RTR"
    if msg.is_error_frame:
        return "ERROR FRAME"
    return bytes(msg.data).hex(" ").upper()


def fmt_value(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


class _Table(QAbstractTableModel):
    headers: tuple[str, ...] = ()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._rows())

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        return None

    def _rows(self):
        raise NotImplementedError

    def _refresh_all(self) -> None:
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))


class TraceModel(_Table):
    """Chronological frame list with a bounded ring buffer."""

    headers = ("Time", "Dir", "ID", "DLC", "Data", "Decoded")

    def __init__(self, max_rows: int = 100_000):
        super().__init__()
        self.max_rows = max_rows
        self.t0: float | None = None
        self._frames: deque[DecodedFrame] = deque()

    def _rows(self):
        return self._frames

    def append(self, frames: list[DecodedFrame]) -> None:
        if not frames:
            return
        frames = frames[-self.max_rows :]
        if self.t0 is None:
            self.t0 = frames[0].msg.timestamp
        overflow = len(self._frames) + len(frames) - self.max_rows
        if overflow > 0:
            self.beginRemoveRows(QModelIndex(), 0, overflow - 1)
            for _ in range(overflow):
                self._frames.popleft()
            self.endRemoveRows()
        first = len(self._frames)
        self.beginInsertRows(QModelIndex(), first, first + len(frames) - 1)
        self._frames.extend(frames)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._frames.clear()
        self.t0 = None
        self.endResetModel()

    def data(self, index, role=Qt.DisplayRole):
        frame = self._frames[index.row()]
        msg = frame.msg
        if role == Qt.DisplayRole:
            col = index.column()
            if col == 0:
                return f"{msg.timestamp - (self.t0 or 0):.4f}"
            if col == 1:
                return "Tx" if frame.tx else "Rx"
            if col == 2:
                return fmt_id(msg)
            if col == 3:
                return str(msg.dlc)
            if col == 4:
                return fmt_data(msg)
            return frame.summary
        if role == Qt.ForegroundRole and frame.tx:
            return TX_BRUSH
        return None


class TraceFilter(QSortFilterProxyModel):
    """Case-insensitive substring filter over the ID and Decoded columns."""

    def __init__(self):
        super().__init__()
        self._needle = ""

    def set_text(self, text: str) -> None:
        self._needle = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        if not self._needle:
            return True
        model = self.sourceModel()
        for col in (2, 5):
            if self._needle in model.data(model.index(row, col, parent)).lower():
                return True
        return False


@dataclass
class _MessageStats:
    count: int
    first: float
    last: float
    frame: DecodedFrame
    period: float | None = None
    min_period: float | None = None
    max_period: float | None = None

    def add(self, frame: DecodedFrame) -> None:
        ts = frame.msg.timestamp
        self.period = ts - self.last
        self.min_period = self.period if self.min_period is None else min(self.min_period, self.period)
        self.max_period = self.period if self.max_period is None else max(self.max_period, self.period)
        self.count += 1
        self.last = ts
        self.frame = frame

    @property
    def avg_period(self) -> float | None:
        return (self.last - self.first) / (self.count - 1) if self.count > 1 else None


def fmt_ms(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds * 1000:.1f}"


class OverviewModel(_Table):
    """One row per CAN ID with count, period statistics and latest content."""

    headers = ("ID", "Count", "Last ms", "Avg ms", "Min ms", "Max ms", "DLC", "Data", "Decoded")

    def __init__(self):
        super().__init__()
        self._keys: list[tuple[int, bool]] = []
        self._stats: dict[tuple[int, bool], _MessageStats] = {}

    def _rows(self):
        return self._keys

    def update(self, frames: list[DecodedFrame]) -> None:
        new = []
        for frame in frames:
            msg = frame.msg
            key = (msg.arbitration_id, msg.is_extended_id)
            stat = self._stats.get(key)
            if stat is None:
                self._stats[key] = _MessageStats(1, msg.timestamp, msg.timestamp, frame)
                new.append(key)
            else:
                stat.add(frame)
        if new:
            first = len(self._keys)
            self.beginInsertRows(QModelIndex(), first, first + len(new) - 1)
            self._keys.extend(new)
            self.endInsertRows()
        self._refresh_all()

    def clear(self) -> None:
        self.beginResetModel()
        self._keys.clear()
        self._stats.clear()
        self.endResetModel()

    def data(self, index, role=Qt.DisplayRole):
        stat = self._stats[self._keys[index.row()]]
        if role == Qt.DisplayRole:
            msg = stat.frame.msg
            return (
                fmt_id(msg),
                str(stat.count),
                fmt_ms(stat.period),
                fmt_ms(stat.avg_period),
                fmt_ms(stat.min_period),
                fmt_ms(stat.max_period),
                str(msg.dlc),
                fmt_data(msg),
                stat.frame.summary,
            )[index.column()]
        if role == Qt.TextAlignmentRole and 1 <= index.column() <= 5:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


def fmt_raw(raw) -> str:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return f"{raw} (0x{raw:X})" if raw >= 0 else str(raw)
    return "" if raw is None else str(raw)


class SignalModel(_Table):
    """Latest value per signal, with its raw value and update interval."""

    headers = ("Source", "Signal", "Value", "Raw", "Unit", "Node", "Interval ms")

    def __init__(self):
        super().__init__()
        self._keys: list[tuple] = []
        # key -> (latest value, timestamp, interval since the previous update)
        self._values: dict[tuple, tuple[SignalValue, float, float | None]] = {}

    def _rows(self):
        return self._keys

    def update(self, frames: list[DecodedFrame]) -> None:
        new = []
        for frame in frames:
            ts = frame.msg.timestamp
            for sig in frame.signals:
                key = (sig.source, sig.name, sig.node)
                previous = self._values.get(key)
                if previous is None:
                    new.append(key)
                    interval = None
                else:
                    interval = ts - previous[1]
                self._values[key] = (sig, ts, interval)
        if new:
            first = len(self._keys)
            self.beginInsertRows(QModelIndex(), first, first + len(new) - 1)
            self._keys.extend(new)
            self.endInsertRows()
        self._refresh_all()

    def clear(self) -> None:
        self.beginResetModel()
        self._keys.clear()
        self._values.clear()
        self.endResetModel()

    def data(self, index, role=Qt.DisplayRole):
        sig, _, interval = self._values[self._keys[index.row()]]
        if role == Qt.DisplayRole:
            node = "" if sig.node is None else f"0x{sig.node:02X} ({sig.node})"
            return (sig.source, sig.name, fmt_value(sig.value), fmt_raw(sig.raw), sig.unit, node, fmt_ms(interval))[
                index.column()
            ]
        if role == Qt.TextAlignmentRole and index.column() == 6:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class NodeModel(_Table):
    headers = ("Address", "State", "Name", "Details", "Heartbeat ms", "Last seen s ago")

    # A node is flagged when no heartbeat arrived for this many intervals.
    TIMEOUT_FACTOR = 2.5

    def __init__(self):
        super().__init__()
        self.now = 0.0  # latest bus timestamp, so ages also work in replay
        self._keys: list[int] = []
        self._nodes: dict[int, NodeInfo] = {}

    def _rows(self):
        return self._keys

    def update(self, frames: list[DecodedFrame]) -> None:
        new = []
        for frame in frames:
            self.now = max(self.now, frame.msg.timestamp)
            for node in frame.nodes:
                if node.address not in self._nodes:
                    new.append(node.address)
                self._nodes[node.address] = node
        if new:
            first = len(self._keys)
            self.beginInsertRows(QModelIndex(), first, first + len(new) - 1)
            self._keys.extend(new)
            self.endInsertRows()
        self._refresh_all()

    def clear(self) -> None:
        self.beginResetModel()
        self._keys.clear()
        self._nodes.clear()
        self.now = 0.0
        self.endResetModel()

    def timed_out(self, node: NodeInfo) -> bool:
        return bool(node.heartbeat_interval) and self.now - node.last_seen > self.TIMEOUT_FACTOR * node.heartbeat_interval

    def data(self, index, role=Qt.DisplayRole):
        node = self._nodes[self._keys[index.row()]]
        if role == Qt.DisplayRole:
            state = f"TIMEOUT (last: {node.state})" if self.timed_out(node) else node.state
            hb = f"{node.heartbeat_interval * 1000:.0f}" if node.heartbeat_interval else ""
            return (
                f"0x{node.address:02X} ({node.address})",
                state,
                node.name,
                node.details,
                hb,
                f"{max(0.0, self.now - node.last_seen):.1f}",
            )[index.column()]
        if role == Qt.ForegroundRole and index.column() == 1 and self.timed_out(node):
            return ALERT_BRUSH
        return None


class FaultModel(_Table):
    headers = ("Source", "Code", "Description", "Detail")

    def __init__(self):
        super().__init__()
        self._sets: dict[str, FaultSet] = {}
        self._flat: list[tuple[str, object]] = []

    def _rows(self):
        return self._flat

    def update(self, frames: list[DecodedFrame]) -> None:
        changed = False
        for frame in frames:
            for fs in frame.faults:
                if self._sets.get(fs.source) != fs:
                    self._sets[fs.source] = fs
                    changed = True
        if changed:
            self.beginResetModel()
            self._flat = [(s, f) for s, fs in sorted(self._sets.items()) for f in fs.faults]
            self.endResetModel()

    @property
    def active_count(self) -> int:
        return len(self._flat)

    def clear(self) -> None:
        self.beginResetModel()
        self._sets.clear()
        self._flat = []
        self.endResetModel()

    def data(self, index, role=Qt.DisplayRole):
        source, fault = self._flat[index.row()]
        if role == Qt.DisplayRole:
            return (source, fault.code, fault.description, fault.detail)[index.column()]
        if role == Qt.ForegroundRole:
            return ALERT_BRUSH
        return None
