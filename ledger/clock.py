"""Time source. Injected so tests can move time forward to expire holds."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FakeClock:
    def __init__(self, start_ms: int = 1_767_225_600_000) -> None:  # 2026-01-01T00:00:00Z
        self._now = start_ms
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += int(seconds * 1000)


def iso(ms: int | None) -> str | None:
    """Epoch milliseconds to an ISO 8601 UTC timestamp with millisecond precision."""
    if ms is None:
        return None
    stamp = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"
