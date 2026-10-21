from __future__ import annotations

from typing import Protocol

import can

from cananalyser.model import DecodedFrame


class Decoder(Protocol):
    """Turns raw frames into :class:`DecodedFrame` records.

    A decoder keeps protocol state (transport sessions, PDO mappings, ...)
    and is only ever called from the acquisition thread.
    """

    def decode(self, msg: can.Message) -> DecodedFrame: ...

    def reset(self) -> None: ...


class RawDecoder:
    """Fallback decoder that only passes frames through."""

    def decode(self, msg: can.Message) -> DecodedFrame:
        return DecodedFrame(msg)

    def reset(self) -> None:
        pass
