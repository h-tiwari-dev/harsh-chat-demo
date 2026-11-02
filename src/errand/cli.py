"""Interactive / scripted chat with the errand agent (offline: mock marketplace + RuleBasedLLM).

    python -m errand.cli --user u1 [--scenario NAME] [--trace-file path] [--db path] [--script file.txt]
"""
from __future__ import annotations

import argparse
import os
import sys

from .agent import Agent
from .clock import FakeClock, SystemClock
from .llm import RuleBasedLLM
from .mocks.provider import MockProviderAPI, Scenario, default_seed_bookings
from .storage import Storage
from .trace import Tracer


class _AnyDelta(dict):
    """hold_price_delta that applies to every provider (CLI 'price_drift' scenario)."""
    def get(self, k, d=0):
        return 100


SCENARIOS = {
    "default": lambda: Scenario(),
    "happy": lambda: Scenario(force_book_outcomes=["success"] * 20),
    "timeout": lambda: Scenario(force_book_outcomes=["timeout_booked"] + ["success"] * 20),
    "slot_taken": lambda: Scenario(force_book_outcomes=["slot_taken"] + ["success"] * 20),
    "price_drift": lambda: Scenario(force_book_outcomes=["success"] * 20, hold_price_delta=_AnyDelta()),
    "no_injection": lambda: Scenario(injection_in_reviews=False),
}


def build_agent(user, scenario="default", db=":memory:", trace_file=None, real_clock=False, llm=None, resume=False):
    clock = SystemClock() if real_clock else FakeClock()
    sc = SCENARIOS[scenario]()
    api = MockProviderAPI(sc, clock, default_seed_bookings(user))
    storage = Storage(db)
    os.makedirs("traces", exist_ok=True)
    tracer = Tracer(trace_file or os.path.join("traces", f"{user}-{clock.now().strftime('%Y%m%dT%H%M%S')}-{os.getpid()}.jsonl"), clock)
    llm = llm or RuleBasedLLM()
    cfg = {"wall_clock_s": 3600}
    ag = (Agent.resume if resume else Agent)(user, api, llm, storage, clock, tracer=tracer, budget_cfg=cfg)
    return ag, api, tracer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="errand.cli")
    ap.add_argument("--user", default="u1")
    ap.add_argument("--scenario", default="default", choices=sorted(SCENARIOS))
    ap.add_argument("--trace-file", default=None)
    ap.add_argument("--db", default=":memory:", help="SQLite path (use a file to keep memory/state across runs)")
    ap.add_argument("--script", default=None, help="text file with one user message per line (non-interactive)")
    ap.add_argument("--real-clock", action="store_true", help="use the system clock (default: fake clock, today=2026-11-02)")
    a = ap.parse_args(argv)

    agent, api, tracer = build_agent(a.user, a.scenario, a.db, a.trace_file, a.real_clock, resume=a.db != ":memory:")
    print(f"[trace: {tracer.path}]  (type 'quit' to exit)")
    if agent.startup_message:
        print(f"agent> {agent.startup_message}")
    def _followups():
        f = agent.check_followups()
        if f is not None:
            print(f"agent> {f.text}\n[phase: {f.phase.name}]")
    _followups()
    lines = None
    if a.script:
        with open(a.script, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip() and not l.lstrip().startswith("#")]
    try:
        it = iter(lines) if lines is not None else None
        while True:
            if it is not None:
                try:
                    text = next(it)
                except StopIteration:
                    break
                print(f"you> {text}")
            else:
                try:
                    text = input("you> ")
                except EOFError:
                    break
            if text.strip().lower() in ("quit", "exit"):
                break
            r = agent.handle(text)
            print(f"agent> {r.text}\n[phase: {r.phase.name}]")
            _followups()
    finally:
        summary = tracer.close()
        print(f"[trace written: {tracer.path}; events={sum(summary['counts'].values())}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
