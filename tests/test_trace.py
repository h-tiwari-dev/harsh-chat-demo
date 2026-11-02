import json
from errand.clock import FakeClock
from errand.trace import Tracer


def test_jsonl_and_summary(tmp_path):
    p = tmp_path / "t" / "run.jsonl"
    t = Tracer(str(p), FakeClock(), run_id="r1")
    t.state_transition("COLLECTING", "SEARCHING", "have slots")
    t.model_call("interpret", "p", "r", 10, tokens_in=5, tokens_out=7, cost_usd=0.01)
    t.tool_call("book", {"api_key": "sk-SECRET123456", "x": 1}, result={"h": "Bearer abc123"}, latency_ms=20)
    t.tool_call("book", {}, error="boom password=hunter2", latency_ms=5)
    t.event("approval", ok=True)
    s = t.close()
    lines = [json.loads(l) for l in p.read_text().splitlines()]
    assert [l["seq"] for l in lines] == [1, 2, 3, 4, 5]
    assert {l["run_id"] for l in lines} == {"r1"}
    assert [l["type"] for l in lines] == ["state_transition", "model_call", "tool_call", "tool_call", "event"]
    raw = p.read_text()
    for sec in ("SECRET123456", "abc123", "hunter2"):
        assert sec not in raw
    assert lines[2]["tool"] == "book" and lines[2]["latency_ms"] == 20
    assert s == {"run_id": "r1", "counts": {"state_transition": 1, "model_call": 1, "tool_call": 2, "event": 1},
                 "total_latency_ms": 35, "tokens": {"in": 5, "out": 7}, "cost_usd": 0.01}


def test_in_memory():
    t = Tracer(None, FakeClock())
    t.event("x", a=1)
    assert t.events[0]["kind"] == "x" and t.events[0]["type"] == "event"
    assert t.close()["counts"] == {"event": 1}
