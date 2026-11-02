"""Shared demo session: builds a scenario world + Agent, supports restart (resume) and trace reading."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from errand.clock import FakeClock  # noqa: E402
from errand.mocks.provider import MockProviderAPI, Scenario, default_seed_bookings  # noqa: E402
from errand.storage import Storage  # noqa: E402
from errand.trace import Tracer  # noqa: E402

OUT_DIR = os.path.join(ROOT, "demo", "out")
USER_ID = "demo-user"
START = "2026-11-02T08:00:00"

SCENARIOS = {
    "happy": ("Happy path (memory prefill)", dict(injection_in_reviews=False),
              "Book an electrician in Indiranagar this week, after 5pm"),
    "injection": ("Injection in reviews (ignored)", dict(injection_in_reviews=True),
                  "Show me reviews for the first electrician in Indiranagar this week"),
    "timeout": ("Timeout after success (recovered)", dict(force_book_outcomes=["timeout_booked"], injection_in_reviews=False),
                "Book an electrician in Indiranagar this week"),
    "drift": ("Price drift at hold (re-approval)", dict(injection_in_reviews=False, _drift=250),
              "Book an electrician in Indiranagar this week"),
    "conflict": ("Slot conflict on reschedule", dict(injection_in_reviews=False, _conflict=True),
                 "Reschedule my AC servicing booking to this week"),
}


def _probe_ids(service="electrician", area="Indiranagar"):
    api = MockProviderAPI(Scenario(), FakeClock(START))
    return api.search_providers(service, area, ["2026-11-02", "2026-11-09"])


def build_scenario(name: str) -> Scenario:
    kw = dict(SCENARIOS[name][1])
    drift, conflict = kw.pop("_drift", 0), kw.pop("_conflict", False)
    sc = Scenario(**kw)
    if drift:
        sc.hold_price_delta = {p.id: drift for p in _probe_ids()}
    if conflict:
        sc.reschedule_conflict_slots = {p.free_slots[0] for p in _probe_ids("ac_servicing") if p.free_slots}
    return sc


class Session:
    def __init__(self, scenario="happy", out_dir=OUT_DIR):
        from errand.llm import RuleBasedLLM
        self.RuleBasedLLM = RuleBasedLLM
        self.name = scenario if scenario in SCENARIOS else "happy"
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        self.trace_path = os.path.join(out_dir, f"trace-{self.name}-{stamp}.jsonl")
        self.db_path = os.path.join(tempfile.mkdtemp(prefix="errand-demo-"), "agent.db")
        self.clock = FakeClock(START)
        self.api = MockProviderAPI(build_scenario(self.name), self.clock, default_seed_bookings(USER_ID))
        self.run_id = None
        self.transcript: list[dict] = []
        self.restarts = 0
        self.phase = "COLLECTING"
        self._boot(resume=False)

    def _boot(self, resume: bool):
        from errand.agent import Agent  # written by the orchestrator
        self.storage = Storage(self.db_path)
        self.tracer = Tracer(self.trace_path, self.clock, run_id=self.run_id)
        self.run_id = self.tracer.run_id
        args = dict(user_id=USER_ID, api=self.api, llm=self.RuleBasedLLM(), storage=self.storage,
                    clock=self.clock, tracer=self.tracer)
        ctor = Agent.resume if resume and hasattr(Agent, "resume") else Agent
        self.agent = ctor(**args)

    def send(self, text: str) -> dict:
        t0 = time.time()
        try:
            r = self.agent.handle(text)
            reply, phase = r.text, getattr(r.phase, "name", str(r.phase))
        except Exception as e:  # keep the demo alive
            reply, phase = f"[demo] agent raised {type(e).__name__}: {e}", "FAILED"
        self.phase = phase
        self.transcript.append({"user": text, "agent": reply, "phase": phase, "ms": int((time.time() - t0) * 1000)})
        return self.transcript[-1]

    def restart(self) -> dict:
        """Simulate a process crash: drop agent, tracer and DB handle; boot a new Agent from the same DB file."""
        try:
            self.tracer.event("process_restart", note="simulated crash; resuming from storage")
            self.tracer.close()
            self.storage.close()
        except Exception:
            pass
        self.restarts += 1
        self._boot(resume=True)
        self.tracer.event("process_resumed", restarts=self.restarts)
        return {"restarts": self.restarts}

    def trace(self, since=0) -> list[dict]:
        out = []
        try:
            with open(self.trace_path, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= since and line.strip():
                        try:
                            out.append(dict(json.loads(line), _i=i))
                        except ValueError:
                            break  # partially written line; pick up next poll
        except FileNotFoundError:
            pass
        return out

    def close(self):
        try:
            self.tracer.close()
        except Exception:
            pass
