"""Danfoss JS1-H joystick profile.

Turns decoded signals into a joystick state (axes, grips, buttons) for the
Joystick view. J1939 signals are matched by the names in
``databases/danfoss_js1h/js1h_j1939.dbc``. CANopen signals are matched by
object index, so they also work with Danfoss' own EDS.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from cananalyser.model import DecodedFrame, SignalValue

OK, ERROR, NOT_AVAILABLE = "ok", "error", "n/a"

# Button states, as in the J1939 2-bit status values.
RELEASED, PRESSED, BUTTON_ERROR, BUTTON_NA = 0, 1, 2, 3

TRAIL_LENGTH = 60

_J1939_AXES = {"X": "X", "Y": "Y", "GripX": "Grip X", "GripY": "Grip Y"}
_J1939_NEGATIVE = {"X": "X_LeverLeftNegPosStatus", "Y": "Y_LeverBackNegPosStatus",
                   "GripX": "GripX_LeverLeftNegPosStatus", "GripY": "GripY_LeverBackNegPosStatus"}

_CANOPEN_AXES = {1: "X", 2: "Y", 3: "Grip 1", 4: "Grip 2", 5: "Grip 3"}
_CANOPEN_NEUTRAL = {0: True, 1: False}  # 2 = error, 3 = not available


@dataclass
class Axis:
    value: float | None = None  # signed percent of travel
    status: str = NOT_AVAILABLE
    neutral: bool | None = None
    detent: bool | None = None


@dataclass
class Joystick:
    key: tuple
    label: str
    axes: dict[str, Axis] = field(default_factory=dict)  # "X", "Y", "Grip 1", ...
    buttons: dict[int, int] = field(default_factory=dict)  # number -> RELEASED/PRESSED/...
    enabled: dict[int, bool] = field(default_factory=dict)  # CANopen only
    trail: deque = field(default_factory=lambda: deque(maxlen=TRAIL_LENGTH))
    last_update: float = 0.0

    @property
    def main_axes(self) -> tuple[Axis, Axis]:
        return self.axes.get("X", Axis()), self.axes.get("Y", Axis())

    @property
    def grips(self) -> list[tuple[str, Axis]]:
        return [(name, axis) for name, axis in self.axes.items() if name not in ("X", "Y")]


class JS1HProfile:
    def __init__(self) -> None:
        self.joysticks: dict[tuple, Joystick] = {}
        self._derived: list[tuple[float, SignalValue]] = []

    def clear(self) -> None:
        self.joysticks.clear()
        self._derived.clear()

    def take_derived(self) -> list[tuple[float, SignalValue]]:
        """Signed axis values produced since the last call, for the trend plot.

        J1939 sends positions as magnitude plus direction bits; these are the
        combined, signed values.
        """
        derived, self._derived = self._derived, []
        return derived

    def update(self, frames: list[DecodedFrame]) -> bool:
        """Apply a batch of frames; returns True when any joystick changed."""
        changed = False
        for frame in frames:
            if not frame.signals:
                continue
            source = frame.signals[0].source
            if source[:3] in ("BJM", "EJM") and source[3:] in ("1", "3"):
                self._j1939(frame, source)
                changed = True
            elif any(s.obj and s.obj[0] in (0x6401, 0x6404, 0x6020) for s in frame.signals):
                self._canopen(frame)
                changed = True
        return changed

    def _joystick(self, key: tuple, label: str) -> Joystick:
        joystick = self.joysticks.get(key)
        if joystick is None:
            joystick = self.joysticks[key] = Joystick(key, label)
        return joystick

    # -- J1939 -------------------------------------------------------------

    def _j1939(self, frame: DecodedFrame, source: str) -> None:
        sa = frame.signals[0].node
        message_set = source[3]
        js = self._joystick(("J1939", sa, message_set), f"J1939 SA 0x{sa:02X} · BJM{message_set}/EJM{message_set}")
        raw = {s.name: s.raw for s in frame.signals}
        updated = set()
        for prefix, axis_name in _J1939_AXES.items():
            position = raw.get(f"{prefix}_Position")
            if position is None:
                continue
            axis = js.axes.setdefault(axis_name, Axis())
            updated.add(axis_name)
            if position == 1022:
                axis.value, axis.status = None, ERROR
            elif position == 1023:
                axis.value, axis.status = None, NOT_AVAILABLE
            else:
                negative = raw.get(_J1939_NEGATIVE[prefix]) == 1
                axis.value = (-position if negative else position) * 0.1
                axis.status = OK
            axis.neutral = _two_bit_flag(raw.get(f"{prefix}_NeutralPosStatus"))
            axis.detent = _two_bit_flag(raw.get(f"{prefix}_DetentPosStatus"))
        for name, value in raw.items():
            if name.startswith("Button") and name.endswith("_PressedStatus") and value is not None:
                js.buttons[int(name[6:-14])] = int(value)
        self._finish(js, frame, updated)

    # -- CANopen -----------------------------------------------------------

    def _canopen(self, frame: DecodedFrame) -> None:
        node = frame.signals[0].node
        js = self._joystick(("CANopen", node, "1"), f"CANopen node {node} (0x{node:02X})")
        updated = set()
        for sig in frame.signals:
            if sig.obj is None or sig.raw is None:
                continue
            index, sub = sig.obj
            if index == 0x6401 and sub in _CANOPEN_AXES:
                axis = js.axes.setdefault(_CANOPEN_AXES[sub], Axis())
                updated.add(_CANOPEN_AXES[sub])
                value = sig.raw - 0x10000 if sig.raw & 0x8000 else sig.raw  # INTEGER16
                axis.value = value * 0.1
                if axis.status == NOT_AVAILABLE:
                    axis.status = OK
            elif index == 0x6404 and sub in _CANOPEN_AXES:
                axis = js.axes.setdefault(_CANOPEN_AXES[sub], Axis())
                axis.neutral = _CANOPEN_NEUTRAL.get(sig.raw)
                axis.status = {2: ERROR, 3: NOT_AVAILABLE}.get(sig.raw, OK)
            elif index == 0x6020 and 1 <= sub <= 24:
                js.buttons[sub] = PRESSED if sig.raw else RELEASED
            elif index == 0x6020 and 25 <= sub <= 48:
                js.enabled[sub - 24] = bool(sig.raw)
        self._finish(js, frame, updated)

    def _finish(self, js: Joystick, frame: DecodedFrame, updated_axes: set[str]) -> None:
        js.last_update = frame.msg.timestamp
        for name in updated_axes:
            axis = js.axes[name]
            if axis.value is not None:
                sig = SignalValue(f"Joystick {js.label}", f"{name} (signed)", axis.value, "%", js.key[1])
                self._derived.append((frame.msg.timestamp, sig))
        x, y = js.main_axes
        if x.value is not None and y.value is not None:
            point = (x.value, y.value)
            if not js.trail or js.trail[-1] != point:
                js.trail.append(point)


def _two_bit_flag(raw) -> bool | None:
    return {0: False, 1: True}.get(raw)

