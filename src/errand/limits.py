"""Run budgets: turns, model calls, tool calls, wall clock."""
from __future__ import annotations


class LimitExceeded(Exception):
    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(message or f"limit exceeded: {kind}")


class Budget:
    def __init__(self, clock, max_turns=12, max_model_calls=30, max_tool_calls=40, wall_clock_s=60):
        self.clock = clock
        self.max_turns = max_turns
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.wall_clock_s = wall_clock_s
        self.turns = 0
        self.model_calls = 0
        self.tool_calls = 0
        self._start = clock.now()

    def elapsed_s(self) -> float:
        return (self.clock.now() - self._start).total_seconds()

    def check_time(self) -> None:
        if self.elapsed_s() > self.wall_clock_s:
            raise LimitExceeded("wall_clock")

    def start_turn(self) -> None:
        self.check_time()
        if self.turns + 1 > self.max_turns:
            raise LimitExceeded("turns")
        self.turns += 1

    def count_model_call(self) -> None:
        self.check_time()
        if self.model_calls + 1 > self.max_model_calls:
            raise LimitExceeded("model_calls")
        self.model_calls += 1

    def count_tool_call(self) -> None:
        self.check_time()
        if self.tool_calls + 1 > self.max_tool_calls:
            raise LimitExceeded("tool_calls")
        self.tool_calls += 1

    def snapshot(self) -> dict:
        return {
            "turns": self.turns, "max_turns": self.max_turns,
            "model_calls": self.model_calls, "max_model_calls": self.max_model_calls,
            "tool_calls": self.tool_calls, "max_tool_calls": self.max_tool_calls,
            "elapsed_s": self.elapsed_s(), "wall_clock_s": self.wall_clock_s,
        }
