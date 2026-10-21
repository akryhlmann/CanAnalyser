"""Time series of decoded signal values for the trend plot."""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from cananalyser.model import DecodedFrame, SignalValue

SignalKey = tuple  # (source, name, node), same as the Signals table


def signal_key(sig: SignalValue) -> SignalKey:
    return (sig.source, sig.name, sig.node)


def plot_value(sig: SignalValue) -> float | None:
    """Physical value; for value-table states the raw value.

    J1939 "n/a" / "error" values become NaN, a gap in the plot: their raw
    value (e.g. 1022 counts) is not on the signal's physical scale.
    """
    if sig.value in ("n/a", "error"):
        return math.nan
    for candidate in (sig.value, sig.raw):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return float(candidate)
    return None


class TrendStore:
    """Keeps the most recent ``max_points`` samples of every numeric signal.

    At 20 ms per message the default keeps about 20 minutes per signal.
    """

    def __init__(self, max_points: int = 60_000):
        self.max_points = max_points
        self.t0: float | None = None
        self._series: dict[SignalKey, tuple[deque, deque]] = {}
        self.units: dict[SignalKey, str] = {}

    def clear(self) -> None:
        self.t0 = None
        self._series.clear()
        self.units.clear()

    def keys(self) -> list[SignalKey]:
        return list(self._series)

    def add(self, frames: list[DecodedFrame]) -> list[SignalKey]:
        """Record a batch of frames; returns keys seen for the first time."""
        return self.add_samples((frame.msg.timestamp, sig) for frame in frames for sig in frame.signals)

    def add_samples(self, samples) -> list[SignalKey]:
        """Record (timestamp, SignalValue) pairs; returns keys seen for the first time."""
        new = []
        for ts, sig in samples:
            value = plot_value(sig)
            if value is None:
                continue
            if self.t0 is None:
                self.t0 = ts
            key = signal_key(sig)
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = (deque(maxlen=self.max_points), deque(maxlen=self.max_points))
                self.units[key] = sig.unit
                new.append(key)
            series[0].append(ts - self.t0)
            series[1].append(value)
        return new

    def latest_time(self) -> float:
        return max((s[0][-1] for s in self._series.values() if s[0]), default=0.0)

    def window(self, key: SignalKey, seconds: float | None, end: float) -> tuple[np.ndarray, np.ndarray]:
        """Samples of ``key`` in the last ``seconds`` before ``end`` (None = everything)."""
        times, values = self._series[key]
        t = np.fromiter(times, dtype=float, count=len(times))
        v = np.fromiter(values, dtype=float, count=len(values))
        if seconds is not None and len(t):
            start = np.searchsorted(t, end - seconds)
            t, v = t[start:], v[start:]
        return t, v
