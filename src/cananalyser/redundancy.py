"""Cross-check of a redundant J1939 joystick (e.g. Danfoss JS1-H SBW).

Two microcontrollers measure the same lever and each sends BJM1 from its own
source address; the secondary sends every data byte bit-inverted. The
decoder inverts the secondary back (``J1939Decoder.inverted``), and this
module pairs primary and secondary frames of the same cycle and compares
them:

* positions within a tolerance (the two measurements differ slightly),
* status bits and buttons equal,
* the secondary really inverted (not a copy of the primary),
* both channels present, paired within ``max_pair_gap`` and not timed out.

A disagreement becomes an active fault only after ``debounce`` consecutive
pairs, so a button edge or a neutral threshold seen one cycle apart by the
two controllers is counted but not raised.

This is diagnostic monitoring, not a safety function.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cananalyser.model import DecodedFrame, Fault, FaultSet, SignalValue

BJM1_PGN = 64982
POSITION_ERROR, POSITION_NA = 1022, 1023

STATUS_FIELDS = (
    "X_NeutralPosStatus", "X_LeverLeftNegPosStatus", "X_LeverRightPosPosStatus", "X_DetentPosStatus",
    "Y_NeutralPosStatus", "Y_LeverBackNegPosStatus", "Y_LeverForwardPosPosStatus", "Y_DetentPosStatus",
    *(f"Button{b}_PressedStatus" for b in range(1, 13)),
)
_NEGATIVE = {"X": "X_LeverLeftNegPosStatus", "Y": "Y_LeverBackNegPosStatus"}

OK, WAITING, MISMATCH, TIMEOUT = "OK", "WAITING", "MISMATCH", "TIMEOUT"


@dataclass
class RedundancyConfig:
    message: str = "BJM1"
    pgn: int = BJM1_PGN
    primary_sa: int = 51
    secondary_sa: int = 52
    position_tolerance: float = 2.0  # % of travel (1 % = 10 counts)
    debounce: int = 3  # consecutive disagreeing pairs before a fault is active
    max_pair_gap: float = 0.010  # s between the two frames of one cycle
    timeout: float = 0.100  # s without a frame on a channel


@dataclass
class Check:
    """One compared quantity (an axis, a status field, a channel, ...)."""

    name: str
    current: str = ""
    worst: str = ""
    disagreements: int = 0  # pairs (or events) that disagreed
    fault_events: int = 0  # times the check became an active fault
    run: int = 0  # consecutive disagreeing pairs
    active: bool = False
    _worst_value: float = 0.0

    def record(self, disagree: bool, debounce: int) -> None:
        if disagree:
            self.disagreements += 1
            self.run += 1
            if self.run >= debounce and not self.active:
                self.active = True
                self.fault_events += 1
        else:
            self.run = 0
            self.active = False

    def track_worst(self, value: float, text: str) -> None:
        if value >= self._worst_value:
            self._worst_value = value
            self.worst = text


@dataclass
class _Sample:
    ts: float
    raw: dict
    data: bytes  # bytes as transmitted


@dataclass
class _Channel:
    last: float | None = None
    count: int = 0
    unpaired: int = 0
    pending: _Sample | None = None
    timed_out: bool = False
    periods: list[float] = field(default_factory=list)


class RedundancyChecker:
    def __init__(self, config: RedundancyConfig | None = None):
        self.config = config or RedundancyConfig()
        self.reset()

    def reset(self) -> None:
        self.pairs = 0
        self.channels = {"primary": _Channel(), "secondary": _Channel()}
        self.checks: dict[str, Check] = {}
        for name in ("Primary channel", "Secondary channel", "Pairing gap", "Inversion",
                     "X position", "Y position", *STATUS_FIELDS):
            self.checks[name] = Check(name)
        self.gap_stats: list[float] = []  # [min, total, max] of the pairing gap
        self._started: float | None = None  # first frame on either channel
        self._derived: list[tuple[float, SignalValue]] = []

    @property
    def inverted_sources(self) -> frozenset[tuple[int, int]]:
        return frozenset({(self.config.pgn, self.config.secondary_sa)})

    # -- input -------------------------------------------------------------

    def update(self, frames: list[DecodedFrame], now: float) -> None:
        cfg = self.config
        for frame in frames:
            msg = frame.msg
            if not frame.signals or frame.signals[0].source != cfg.message:
                continue
            sa = frame.signals[0].node
            if sa == cfg.primary_sa:
                self._receive("primary", "secondary", frame)
            elif sa == cfg.secondary_sa:
                self._receive("secondary", "primary", frame)
            now = max(now, msg.timestamp)
        self.check_timeouts(now)

    def _receive(self, name: str, other_name: str, frame: DecodedFrame) -> None:
        cfg = self.config
        channel, other = self.channels[name], self.channels[other_name]
        if self._started is None:
            self._started = frame.msg.timestamp
        sample = _Sample(frame.msg.timestamp, {s.name: s.raw for s in frame.signals}, bytes(frame.msg.data))
        if channel.last is not None:
            channel.periods.append(sample.ts - channel.last)
            del channel.periods[:-500]
        channel.last = sample.ts
        channel.count += 1

        if other.pending is not None and sample.ts - other.pending.ts > cfg.max_pair_gap:
            other.unpaired += 1  # partner never came
            self.checks[f"{other_name.capitalize()} channel"].disagreements += 1
            other.pending = None
        if other.pending is not None:
            partner, other.pending = other.pending, None
            if name == "primary":
                self._compare(sample, partner)
            else:
                self._compare(partner, sample)
            return
        if channel.pending is not None:
            channel.unpaired += 1
            self.checks[f"{name.capitalize()} channel"].disagreements += 1
        channel.pending = sample

    def check_timeouts(self, now: float) -> None:
        for name, channel in self.channels.items():
            check = self.checks[f"{name.capitalize()} channel"]
            # A channel that never sent anything times out once the other one
            # has been running for longer than the timeout.
            reference = channel.last if channel.last is not None else self._started
            timed_out = reference is not None and now - reference > self.config.timeout
            if timed_out and not channel.timed_out:
                check.fault_events += 1
            channel.timed_out = check.active = timed_out
            period = _avg(channel.periods)
            if channel.last is None:
                check.current = "no frames" + (" (TIMEOUT)" if timed_out else "")
            elif timed_out:
                check.current = f"TIMEOUT ({(now - channel.last) * 1000:.0f} ms silent)"
            else:
                check.current = f"{channel.count} frames, {period * 1000:.1f} ms period" if period else f"{channel.count} frames"
            check.worst = f"{channel.unpaired} unpaired"

    # -- comparison ----------------------------------------------------------

    def _compare(self, p: _Sample, s: _Sample) -> None:
        cfg = self.config
        self.pairs += 1
        gap = s.ts - p.ts
        if not self.gap_stats:
            self.gap_stats = [gap, 0.0, gap]
        self.gap_stats = [min(self.gap_stats[0], gap), self.gap_stats[1] + gap, max(self.gap_stats[2], gap)]
        g = self.checks["Pairing gap"]
        g.current = f"{gap * 1000:+.2f} ms"
        g.worst = (f"min {self.gap_stats[0] * 1000:+.2f} / avg {self.gap_stats[1] / self.pairs * 1000:+.2f} / "
                   f"max {self.gap_stats[2] * 1000:+.2f} ms")
        self._derived.append((s.ts, SignalValue("Redundancy", f"{cfg.message} pairing gap", gap * 1000, "ms")))

        # The secondary must be the inverse of the primary, give or take the
        # small measurement differences. If it is closer to an exact copy, it
        # was not inverted (a stuck or wrongly configured channel).
        inv = self.checks["Inversion"]
        copied = _bit_distance(p.data, s.data) < _bit_distance(p.data, bytes(b ^ 0xFF for b in s.data))
        inv.record(copied, cfg.debounce)
        inv.current = "secondary NOT inverted" if copied else "ok"

        for axis in ("X", "Y"):
            self._compare_axis(axis, p, s)
        for name in STATUS_FIELDS:
            a, b = p.raw.get(name), s.raw.get(name)
            check = self.checks[name]
            check.record(a != b, cfg.debounce)
            check.current = f"{a} / {b}"

    def _compare_axis(self, axis: str, p: _Sample, s: _Sample) -> None:
        cfg = self.config
        check = self.checks[f"{axis} position"]
        pv, sv = _signed_counts(p.raw, axis), _signed_counts(s.raw, axis)
        if isinstance(pv, str) or isinstance(sv, str):
            check.record(pv != sv, cfg.debounce)  # both "error" (or both "n/a") agree
            check.current = f"{_fmt_counts(pv)} / {_fmt_counts(sv)}"
            return
        diff = (sv - pv) / 10  # percent
        disagree = abs(diff) > cfg.position_tolerance
        check.record(disagree, cfg.debounce)
        check.current = f"{pv / 10:+.1f} / {sv / 10:+.1f} %  (Δ {diff:+.1f} %)"
        check.track_worst(abs(diff), f"|Δ| {abs(diff):.1f} %")
        self._derived.append((s.ts, SignalValue("Redundancy", f"{cfg.message} {axis} Δ secondary−primary", diff, "%")))

    # -- output ----------------------------------------------------------------

    @property
    def status(self) -> str:
        if any(c.timed_out for c in self.channels.values()):
            return TIMEOUT
        if self.pairs == 0:
            return WAITING
        if any(c.active for c in self.checks.values()):
            return MISMATCH
        return OK

    def fault_set(self) -> FaultSet:
        cfg = self.config
        # Kept free of live values so the set only changes when a fault comes or goes.
        faults = tuple(
            Fault(
                code=f"REDUNDANCY {check.name}",
                description="channel timeout" if check.name.endswith("channel") else "primary and secondary disagree",
                detail="see Redundancy tab for current values",
            )
            for check in self.checks.values()
            if check.active
        )
        return FaultSet(f"{cfg.message} 0x{cfg.primary_sa:02X}/0x{cfg.secondary_sa:02X}", faults)

    def take_derived(self) -> list[tuple[float, SignalValue]]:
        derived, self._derived = self._derived, []
        return derived


def _signed_counts(raw: dict, axis: str):
    position = raw.get(f"{axis}_Position")
    if position is None:
        return "missing"
    if position == POSITION_ERROR:
        return "error"
    if position == POSITION_NA:
        return "n/a"
    return -position if raw.get(_NEGATIVE[axis]) == 1 else position


def _fmt_counts(value) -> str:
    return value if isinstance(value, str) else f"{value / 10:+.1f} %"


def _bit_distance(a: bytes, b: bytes) -> int:
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
