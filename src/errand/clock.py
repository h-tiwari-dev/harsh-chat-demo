from __future__ import annotations
import time
from datetime import datetime, timedelta


class Clock:
    def now(self) -> datetime: raise NotImplementedError
    def sleep(self, s: float) -> None: raise NotImplementedError
    def advance(self, s: float) -> None: raise NotImplementedError


class SystemClock(Clock):
    def now(self): return datetime.now()
    def sleep(self, s): time.sleep(s)
    def advance(self, s): pass


class FakeClock(Clock):
    """Deterministic clock; sleep() advances time instantly."""
    def __init__(self, start: str = "2026-11-02T08:00:00"):
        self._t = datetime.fromisoformat(start)
    def now(self): return self._t
    def sleep(self, s): self.advance(s)
    def advance(self, s): self._t += timedelta(seconds=s)
