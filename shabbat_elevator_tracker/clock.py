"""Clock abstraction. Every time read in the service goes through a Clock.

Times are epoch seconds (float). The scaled clock runs `rate` times faster than real
time from the moment it is created (dev speed-up with looped recordings).
"""

from __future__ import annotations

import time


class Clock:
    rate: float = 1.0

    def now(self) -> float:
        raise NotImplementedError

    def real_to_virtual(self, seconds: float) -> float:
        return seconds * self.rate


class RealClock(Clock):
    rate = 1.0

    def now(self) -> float:
        return time.time()


class ScaledClock(Clock):
    def __init__(self, rate: float, start: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self._real0 = time.time()
        self._virt0 = self._real0 if start is None else float(start)

    def now(self) -> float:
        return self._virt0 + (time.time() - self._real0) * self.rate


class ManualClock(Clock):
    """Clock driven by the caller (replay, tests)."""

    def __init__(self, start: float = 0.0, rate: float = 1.0) -> None:
        self._t = float(start)
        self.rate = rate

    def now(self) -> float:
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)

    def advance(self, dt: float) -> None:
        self._t += dt
