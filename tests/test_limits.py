import pytest
from errand.clock import FakeClock
from errand.limits import Budget, LimitExceeded


def kind(fn):
    with pytest.raises(LimitExceeded) as e:
        fn()
    return e.value.kind


def test_turns():
    b = Budget(FakeClock(), max_turns=2)
    b.start_turn(); b.start_turn()
    assert kind(b.start_turn) == "turns"


def test_model_and_tool_calls():
    b = Budget(FakeClock(), max_model_calls=1, max_tool_calls=2)
    b.count_model_call()
    assert kind(b.count_model_call) == "model_calls"
    b.count_tool_call(); b.count_tool_call()
    assert kind(b.count_tool_call) == "tool_calls"


def test_wall_clock():
    c = FakeClock()
    b = Budget(c, wall_clock_s=60)
    c.advance(60); b.check_time()
    c.advance(1)
    assert kind(b.check_time) == "wall_clock"
    assert kind(b.count_tool_call) == "wall_clock"


def test_snapshot():
    c = FakeClock()
    b = Budget(c)
    b.start_turn(); b.count_tool_call(); c.advance(5)
    s = b.snapshot()
    assert s["turns"] == 1 and s["tool_calls"] == 1 and s["model_calls"] == 0 and s["elapsed_s"] == 5
