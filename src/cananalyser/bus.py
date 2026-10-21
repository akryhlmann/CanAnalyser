"""Frame sources and the acquisition thread.

Kept free of any GUI code so the same engine can drive a desktop UI, a web
dashboard or a headless test script.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import can

from cananalyser.decoders import Decoder, RawDecoder
from cananalyser.model import DecodedFrame

log = logging.getLogger(__name__)

BITRATES = (125_000, 250_000, 500_000, 1_000_000, 50_000, 20_000, 10_000, 800_000)


@dataclass
class BusConfig:
    interface: str = "kvaser"  # "kvaser", "virtual", or any python-can interface
    channel: int | str = 0
    bitrate: int = 250_000
    listen_only: bool = True

    def open(self) -> can.BusABC:
        kwargs: dict = {"interface": self.interface, "channel": self.channel}
        if self.interface == "kvaser":
            kwargs["bitrate"] = self.bitrate
            # python-can's Kvaser backend: driver_mode=False selects silent mode.
            kwargs["driver_mode"] = not self.listen_only
        elif self.interface == "virtual":
            kwargs["receive_own_messages"] = False
        else:
            kwargs["bitrate"] = self.bitrate
        return can.Bus(**kwargs)


class ReplaySource:
    """Plays back a log file (BLF, ASC, candump .log, TRC, CSV) in real time."""

    def __init__(self, path: str | Path, realtime: bool = True):
        reader = can.LogReader(str(path))
        self._iter = iter(can.MessageSync(reader, skip=1.0) if realtime else reader)
        self.finished = False

    def recv(self, timeout: float) -> can.Message | None:
        if self.finished:
            time.sleep(timeout)
            return None
        try:
            return next(self._iter)
        except StopIteration:
            self.finished = True
            return None

    def shutdown(self) -> None:
        self.finished = True


class Monitor:
    """Owns the frame source, decoder and logger; runs the reader thread.

    Decoded frames are put on :attr:`frames`; consumers drain the queue in
    batches (the GUI does this on a timer) so a busy bus never blocks the
    reader.
    """

    def __init__(self, decoder: Decoder | None = None, max_queue: int = 200_000):
        self.decoder: Decoder = decoder or RawDecoder()
        self.frames: queue.Queue[DecodedFrame] = queue.Queue(max_queue)
        self.dropped = 0
        self.error: str | None = None
        self._listen_only = True
        self._tx_echo: queue.SimpleQueue[can.Message] = queue.SimpleQueue()
        self._bus: can.BusABC | None = None
        self._source = None
        self._logger: can.Listener | None = None
        self._log_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def can_transmit(self) -> bool:
        return self._bus is not None

    def start_bus(self, config: BusConfig) -> None:
        self.stop()
        self._bus = config.open()
        self._listen_only = config.listen_only
        self._start(self._bus)

    def start_replay(self, path: str | Path, realtime: bool = True) -> None:
        self.stop()
        self._bus = None
        self._start(ReplaySource(path, realtime))

    def _start(self, source) -> None:
        self._source = source
        self.error = None
        self.dropped = 0
        self.decoder.reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="can-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._source is not None:
            self._source.shutdown()
            self._source = None
        self._bus = None
        self.stop_logging()

    # -- logging -------------------------------------------------------------

    def start_logging(self, path: str | Path) -> None:
        """Record every frame; format follows the extension (.blf, .asc, .log, .csv, .trc)."""
        with self._log_lock:
            if self._logger is not None:
                self._logger.stop()
            self._logger = can.Logger(str(path))

    def stop_logging(self) -> None:
        with self._log_lock:
            if self._logger is not None:
                self._logger.stop()
                self._logger = None

    @property
    def logging(self) -> bool:
        return self._logger is not None

    # -- transmit ------------------------------------------------------------

    def send(self, msg: can.Message) -> None:
        if self._bus is None:
            raise RuntimeError("Not connected to a CAN interface")
        if self._listen_only:
            raise RuntimeError("Interface is in listen-only mode")
        msg.timestamp = time.time()
        self._bus.send(msg)
        # Kvaser opens separate read/write handles, so our own frames are not
        # echoed back. Hand them to the reader thread, which owns the decoder.
        msg.is_rx = False
        self._tx_echo.put(msg)

    # -- reader thread -------------------------------------------------------

    def _run(self) -> None:
        source = self._source
        try:
            while not self._stop.is_set():
                msg = source.recv(0.05)
                while not self._tx_echo.empty():
                    self._process(self._tx_echo.get(), tx=True)
                if msg is not None:
                    self._process(msg)
        except Exception as exc:  # noqa: BLE001 - surface driver errors to the UI
            log.exception("CAN reader stopped")
            self.error = str(exc)

    def _process(self, msg: can.Message, tx: bool = False) -> None:
        with self._log_lock:
            if self._logger is not None:
                self._logger(msg)
        try:
            frame = self.decoder.decode(msg)
        except Exception as exc:  # noqa: BLE001 - never let one bad frame kill the reader
            log.exception("Decoder error")
            frame = DecodedFrame(msg, summary=f"<decoder error: {exc}>")
        frame.tx = tx
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            self.dropped += 1

    def drain(self, limit: int = 20_000) -> list[DecodedFrame]:
        out = []
        try:
            while len(out) < limit:
                out.append(self.frames.get_nowait())
        except queue.Empty:
            pass
        return out
