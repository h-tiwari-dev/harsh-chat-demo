"""JSONL tracer with OpenTelemetry-ish field names (trace_id == run_id)."""
from __future__ import annotations

import json
import uuid

from .sanitize import sanitize_for_trace


class Tracer:
    def __init__(self, path, clock, run_id=None):
        self.path = path
        self.clock = clock
        self.run_id = run_id or uuid.uuid4().hex[:16]
        self.events: list[dict] = []
        self._seq = 0
        self._fh = None
        self._closed = False
        self._summary = None
        self._counts: dict[str, int] = {}
        self._latency = 0.0
        self._tin = 0
        self._tout = 0
        self._cost = 0.0
        if path:
            import os
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def _emit(self, type_: str, **fields) -> dict:
        self._seq += 1
        rec = {"ts": self.clock.now().isoformat(), "seq": self._seq,
               "run_id": self.run_id, "trace_id": self.run_id,
               "span_id": f"{self._seq:08x}", "type": type_}
        rec.update(sanitize_for_trace(fields))
        self._counts[type_] = self._counts.get(type_, 0) + 1
        self.events.append(rec)
        if self._fh and not self._closed:
            self._fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
            self._fh.flush()
        return rec

    def state_transition(self, frm, to, reason=""):
        self._emit("state_transition", frm=str(getattr(frm, "name", frm)),
                   to=str(getattr(to, "name", to)), reason=reason)

    def model_call(self, name, prompt_summary="", response_summary="", latency_ms=0,
                   tokens_in=0, tokens_out=0, cost_usd=0.0):
        self._latency += latency_ms
        self._tin += tokens_in
        self._tout += tokens_out
        self._cost += cost_usd
        self._emit("model_call", name=name, prompt_summary=prompt_summary,
                   response_summary=response_summary, latency_ms=latency_ms,
                   tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)

    def tool_call(self, tool, args, result=None, error=None, latency_ms=0):
        self._latency += latency_ms
        self._emit("tool_call", tool=tool, args=args, result=result,
                   error=None if error is None else str(error), latency_ms=latency_ms)

    def event(self, kind, **fields):
        fields.pop("type", None)
        self._emit("event", kind=kind, **fields)

    def close(self) -> dict:
        if self._summary is not None:
            return self._summary
        self._summary = {
            "run_id": self.run_id,
            "counts": dict(self._counts),
            "total_latency_ms": self._latency,
            "tokens": {"in": self._tin, "out": self._tout},
            "cost_usd": self._cost,
        }
        self._closed = True
        if self._fh:
            self._fh.close()
            self._fh = None
        return self._summary
