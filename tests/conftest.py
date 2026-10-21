from pathlib import Path

import can
import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def examples() -> Path:
    return EXAMPLES


def frame(can_id: int, data, extended: bool | None = None, ts: float = 0.0, remote: bool = False) -> can.Message:
    if extended is None:
        extended = can_id > 0x7FF
    return can.Message(
        arbitration_id=can_id,
        data=bytes(data),
        is_extended_id=extended,
        timestamp=ts,
        is_remote_frame=remote,
    )
