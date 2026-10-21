"""Live view of a Danfoss JS1-H joystick: XY plot, grip bars and buttons."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cananalyser.profiles.js1h import (
    BUTTON_ERROR,
    BUTTON_NA,
    ERROR,
    NOT_AVAILABLE,
    OK,
    PRESSED,
    Axis,
    Joystick,
    JS1HProfile,
)

GOOD = QColor(30, 160, 80)
BAD = QColor(210, 40, 40)
MUTED = QColor(150, 150, 150)
ACCENT = QColor(40, 110, 220)


def status_color(status: str) -> QColor:
    return {OK: ACCENT, ERROR: BAD}.get(status, MUTED)


def axis_text(axis: Axis) -> str:
    if axis.status == ERROR:
        return "ERROR"
    if axis.value is None:
        return "n/a"
    return f"{axis.value:+.1f} %"


class XYPlot(QWidget):
    """Square ±100 % plot with the current position and a short trail."""

    def __init__(self):
        super().__init__()
        self.joystick: Joystick | None = None
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(420, 420)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 40
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        fg = self.palette().text().color()
        p.fillRect(rect, self.palette().base())

        def to_px(x: float, y: float) -> QPointF:  # +Y (forward) is up
            return QPointF(rect.center().x() + x / 100 * side / 2, rect.center().y() - y / 100 * side / 2)

        grid = QPen(MUTED, 1, Qt.DotLine)
        for v in (-50, 50):
            p.setPen(grid)
            p.drawLine(to_px(v, -100), to_px(v, 100))
            p.drawLine(to_px(-100, v), to_px(100, v))
        p.setPen(QPen(fg, 1))
        p.drawRect(rect)
        p.drawLine(to_px(0, -100), to_px(0, 100))
        p.drawLine(to_px(-100, 0), to_px(100, 0))
        p.setFont(QFont(p.font().family(), 8))
        p.drawText(QRectF(rect.right() - 60, rect.center().y() + 2, 58, 14), Qt.AlignRight, "X +100")
        p.drawText(QRectF(rect.center().x() + 4, rect.top() + 2, 60, 14), Qt.AlignLeft, "Y +100")

        js = self.joystick
        if js is None:
            return
        x, y = js.main_axes
        trail = list(js.trail)
        if len(trail) > 1:
            path = QPainterPath(to_px(*trail[0]))
            for point in trail[1:]:
                path.lineTo(to_px(*point))
            trail_color = QColor(ACCENT)
            trail_color.setAlpha(90)
            p.setPen(QPen(trail_color, 2))
            p.drawPath(path)
        ok = x.status == OK and y.status == OK
        if x.value is not None and y.value is not None:
            color = ACCENT if ok else BAD
            p.setPen(QPen(color.darker(130), 2))
            p.setBrush(color)
            p.drawEllipse(to_px(x.value, y.value), 9, 9)
        p.setPen(QPen(BAD if not ok else fg))
        p.setFont(QFont(p.font().family(), 10, QFont.Bold))
        p.drawText(QRectF(rect.left() + 6, rect.top() + 4, side, 18), Qt.AlignLeft, f"X {axis_text(x)}   Y {axis_text(y)}")


class BarGraph(QWidget):
    """Horizontal ±100 % bar for one proportional input."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.axis = Axis()
        self.setFixedHeight(34)
        self.setMinimumWidth(220)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = self.palette().text().color()
        label_w, value_w = 70, 80
        track = QRectF(label_w, 8, self.width() - label_w - value_w - 8, self.height() - 16)
        p.setPen(fg)
        p.drawText(QRectF(0, 0, label_w - 6, self.height()), Qt.AlignVCenter | Qt.AlignRight, self.name)
        p.fillRect(track, self.palette().base())
        p.setPen(QPen(MUTED, 1))
        p.drawRect(track)
        zero = track.center().x()
        if self.axis.value is not None:
            end = zero + max(-100.0, min(100.0, self.axis.value)) / 100 * track.width() / 2
            bar = QRectF(min(zero, end), track.top() + 2, abs(end - zero), track.height() - 4)
            p.fillRect(bar, status_color(self.axis.status))
        p.setPen(QPen(fg, 1))
        p.drawLine(QPointF(zero, track.top() - 3), QPointF(zero, track.bottom() + 3))
        p.setPen(BAD if self.axis.status == ERROR else fg)
        p.drawText(QRectF(track.right() + 8, 0, value_w, self.height()), Qt.AlignVCenter | Qt.AlignLeft, axis_text(self.axis))


class ButtonLeds(QWidget):
    """One round indicator per button: pressed, released, error, not available."""

    COLUMNS = 8
    CELL = 46

    def __init__(self):
        super().__init__()
        self.buttons: dict[int, int] = {}
        self.enabled: dict[int, bool] = {}

    def set_state(self, buttons: dict[int, int], enabled: dict[int, bool]) -> None:
        resized = len(buttons) != len(self.buttons)
        self.buttons, self.enabled = dict(buttons), dict(enabled)
        if resized:
            self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        rows = max(1, -(-len(self.buttons) // self.COLUMNS))
        return QSize(self.COLUMNS * self.CELL, rows * self.CELL)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = self.palette().text().color()
        for i, number in enumerate(sorted(self.buttons)):
            state = self.buttons[number]
            cx = (i % self.COLUMNS) * self.CELL + self.CELL / 2
            cy = (i // self.COLUMNS) * self.CELL + self.CELL / 2
            circle = QRectF(cx - 15, cy - 15, 30, 30)
            enabled = self.enabled.get(number, True)
            if state == BUTTON_NA:
                p.setPen(QPen(MUTED, 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
            else:
                color = {PRESSED: GOOD, BUTTON_ERROR: BAD}.get(state, self.palette().base().color())
                if not enabled:
                    color = QColor(color)
                    color.setAlpha(70)
                p.setPen(QPen(MUTED if not enabled else fg, 1))
                p.setBrush(color)
            p.drawEllipse(circle)
            p.setPen(fg if enabled and state != BUTTON_NA else MUTED)
            p.drawText(circle, Qt.AlignCenter, "E" if state == BUTTON_ERROR else str(number))


class JoystickView(QWidget):
    """The Joystick tab: picks a joystick from the profile and shows it."""

    def __init__(self):
        super().__init__()
        self.selector = QComboBox()
        self.age = QLabel()
        top = QHBoxLayout()
        top.addWidget(QLabel("Joystick"))
        top.addWidget(self.selector, 1)
        top.addWidget(self.age)

        self.xy = XYPlot()
        self.grip_box = QGroupBox("Grip proportional inputs")
        self.grip_layout = QVBoxLayout(self.grip_box)
        self.bars: dict[str, BarGraph] = {}
        self.status_box = QGroupBox("Axis status")
        self.status_grid = QGridLayout(self.status_box)
        self.status_grid.setColumnStretch(1, 1)
        self.status_labels: dict[str, QLabel] = {}
        self.leds = ButtonLeds()
        button_box = QGroupBox("Buttons  (green = pressed, E = error, dashed = not available, faded = not enabled)")
        QVBoxLayout(button_box).addWidget(self.leds)

        right = QVBoxLayout()
        right.addWidget(self.grip_box)
        right.addWidget(self.status_box)
        right.addWidget(button_box)
        right.addStretch()
        body = QHBoxLayout()
        body.addWidget(self.xy, 3)
        body.addLayout(right, 2)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addLayout(top)
        layout.addLayout(body)

        hint = QLabel(
            "No JS1-H joystick data yet.\n\nLoad databases/danfoss_js1h/js1h_j1939.dbc (J1939) or an EDS "
            "for the joystick's node (CANopen), connect, and for CANopen send NMT Start.\n"
            "The view fills as soon as BJM/EJM messages or TPDOs with JS1-H objects arrive."
        )
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        self.stack = QStackedWidget()
        self.stack.addWidget(hint)
        self.stack.addWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)

    def refresh(self, profile: JS1HProfile, now: float) -> None:
        keys = list(profile.joysticks)
        current = self.selector.currentData()
        if [self.selector.itemData(i) for i in range(self.selector.count())] != keys:
            self.selector.blockSignals(True)
            self.selector.clear()
            for key in keys:
                self.selector.addItem(profile.joysticks[key].label, key)
            index = keys.index(current) if current in keys else 0
            self.selector.setCurrentIndex(index)
            self.selector.blockSignals(False)
        js = profile.joysticks.get(self.selector.currentData()) if keys else None
        self.stack.setCurrentIndex(1 if js else 0)
        if js is None:
            return
        self.age.setText(f"updated {max(0.0, now - js.last_update) * 1000:.0f} ms ago")
        self.xy.joystick = js
        self.xy.update()
        self._update_grips(js)
        self._update_status(js)
        self.leds.set_state(js.buttons, js.enabled)

    def _update_grips(self, js: Joystick) -> None:
        grips = js.grips
        if [name for name, _ in grips] != list(self.bars):
            for bar in self.bars.values():
                bar.deleteLater()
            self.bars = {}
            for name, _ in grips:
                self.bars[name] = BarGraph(name)
                self.grip_layout.addWidget(self.bars[name])
        for name, axis in grips:
            self.bars[name].axis = axis
            self.bars[name].update()
        self.grip_box.setVisible(bool(grips))

    def _update_status(self, js: Joystick) -> None:
        if list(js.axes) != list(self.status_labels):
            while self.status_grid.count():
                self.status_grid.takeAt(0).widget().deleteLater()
            self.status_labels = {}
            for row, name in enumerate(js.axes):
                self.status_labels[name] = QLabel()
                self.status_grid.addWidget(QLabel(f"<b>{name}</b>"), row, 0)
                self.status_grid.addWidget(self.status_labels[name], row, 1)
        for name, axis in js.axes.items():
            label = self.status_labels[name]
            parts = [_flag("neutral", axis.neutral)]
            if axis.detent is not None:
                parts.append(_flag("detent", axis.detent))
            if axis.status != OK:
                parts.append(f"<span style='color:{BAD.name()}'>{axis.status.upper()}</span>")
            label.setText("&nbsp;&nbsp;".join(parts))


def _flag(name: str, value: bool | None) -> str:
    if value is None:
        return f"<span style='color:{MUTED.name()}'>{name} n/a</span>"
    mark = "●" if value else "○"
    color = GOOD.name() if value else MUTED.name()
    return f"<span style='color:{color}'>{mark} {name}</span>"
