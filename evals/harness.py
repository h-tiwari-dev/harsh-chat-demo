"""Eval harness: a recorded world (agent + mock marketplace + recording LLM), scenario registry and checks."""
from __future__ import annotations

import copy
import traceback
from dataclasses import dataclass, field
from typing import Callable

from errand.agent import Agent
from errand.clock import FakeClock
from errand.llm import RuleBasedLLM
from errand.mocks.provider import MockProviderAPI, Scenario, default_seed_bookings
from errand.storage import Storage
from errand.trace import Tracer

MUTATING = ("hold_slot", "book", "cancel", "reschedule")


class CrashNow(BaseException):
    """Simulates the process dying (not caught by `except Exception`)."""


class FlakyAPI:
    """Delegates to the real mock; can crash / fail / hook selected tools."""

    def __init__(self, inner):
        self._inner = inner
        self.crash_on: set[str] = set()
        self.fail_on: dict[str, Exception] = {}
        self.hooks: dict[str, Callable] = {}
        self.post: dict[str, Callable] = {}  # tool -> fn(result) -> result

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_") or name in ("all_bookings",):
            return attr

        def wrapped(*a, **k):
            if name in self.crash_on:
                raise CrashNow(name)
            if name in self.fail_on:
                raise self.fail_on[name]
            if name in self.hooks:
                self.hooks[name]()
            r = attr(*a, **k)
            if name in self.post:
                r = self.post[name](r)
            return r
        return wrapped


class RecLLM:
    """Wraps any LLM, recording every (user_text, state_snapshot) it is shown."""

    def __init__(self, inner):
        self.inner = inner
        self.snapshots: list[tuple[str, dict]] = []

    def interpret(self, user_text, state):
        self.snapshots.append((user_text, copy.deepcopy(state)))
        return self.inner.interpret(user_text, state)

    def __getattr__(self, n):
        return getattr(self.inner, n)


class World:
    """One agent process over a mock marketplace. `resume=True` + shared mock/storage/clock = restart."""

    def __init__(self, path=":memory:", llm=None, user="u1", mock=None, storage=None, clock=None,
                 budget_cfg=None, flaky=False, resume=False, **scenario):
        self.user = user
        self.clock = clock or FakeClock()
        self.mock = mock or MockProviderAPI(Scenario(**scenario), self.clock, default_seed_bookings(user))
        self.api = FlakyAPI(self.mock) if flaky else self.mock
        self.storage = storage or Storage(path)
        self.tracer = Tracer(None, self.clock)
        self.llm = RecLLM(llm or RuleBasedLLM())
        ctor = Agent.resume if resume else Agent
        self.agent = ctor(user, self.api, self.llm, self.storage, self.clock, tracer=self.tracer,
                          budget_cfg=budget_cfg)

    # observations
    def calls(self, *tools):
        return [c for c in self.mock.call_log if c["tool"] in tools]

    def events(self, kind=None):
        ev = [e for e in self.tracer.events if e["type"] == "event"]
        return [e for e in ev if kind is None or e.get("kind") == kind]

    def new_bookings(self):
        return [b for b in self.mock.all_bookings() if not b.id.startswith("BK-SEED")]

    def booking(self, bid):
        return next((b for b in self.mock.all_bookings() if b.id == bid), None)

    @property
    def phase(self):
        return self.agent.state.phase.name


@dataclass
class RunRecord:
    """Everything the invariant checker needs about one independent run (possibly several processes)."""
    worlds: list = field(default_factory=list)
    replies: list = field(default_factory=list)       # (user_text, reply_text, phase_name)
    startup: list = field(default_factory=list)       # startup messages after restarts
    forgotten: list = field(default_factory=list)     # (value, reply_idx, trace_marks{id(world):n}, snapshot_marks)
    mock: object = None


@dataclass
class Check:
    name: str
    expected: str
    actual: str
    ok: bool


class Ctx:
    """Passed to every scenario function."""

    def __init__(self, name):
        self.name = name
        self.runs: list[RunRecord] = []
        self.checks: list[Check] = []
        self.notes: list[str] = []

    # --- worlds
    def new_run(self) -> RunRecord:
        r = RunRecord()
        self.runs.append(r)
        return r

    def world(self, run: RunRecord | None = None, **kw) -> World:
        run = run or (self.runs[0] if self.runs else self.new_run())
        w = World(**kw)
        run.worlds.append(w)
        run.mock = w.mock
        return w

    def say(self, w: World, text, run: RunRecord | None = None):
        run = run or self._run_of(w)
        r = w.agent.handle(text)
        run.replies.append((text, r.text, r.phase.name))
        return r

    def startup(self, w: World):
        run = self._run_of(w)
        if w.agent.startup_message:
            run.startup.append(w.agent.startup_message)

    def _run_of(self, w):
        for r in self.runs:
            if w in r.worlds:
                return r
        raise KeyError("world not registered")

    def mark_forgotten(self, w: World, value: str):
        """From now on `value` must never appear in LLM snapshots, replies, traces or stored memory."""
        run = self._run_of(w)
        run.forgotten.append({"value": value, "reply_idx": len(run.replies),
                              "trace": {id(x): len(x.tracer.events) for x in run.worlds},
                              "snap": {id(x): len(x.llm.snapshots) for x in run.worlds}})

    # --- assertions recorded as expected/actual pairs
    def check(self, name, expected, actual):
        self.checks.append(Check(name, _s(expected), _s(actual), expected == actual))

    def check_true(self, name, cond, detail=""):
        self.checks.append(Check(name, "True", _s(bool(cond)) + (f" ({detail})" if detail and not cond else ""), bool(cond)))

    def note(self, s):
        self.notes.append(s)


def _s(x):
    s = repr(x) if not isinstance(x, str) else x
    return s if len(s) <= 140 else s[:137] + "..."


@dataclass
class ScenarioDef:
    name: str
    desc: str
    fn: Callable[[Ctx], None]


REGISTRY: list[ScenarioDef] = []


def scenario(name, desc):
    def deco(fn):
        REGISTRY.append(ScenarioDef(name, desc, fn))
        return fn
    return deco


def run_scenario(sd: ScenarioDef):
    from .invariants import check_run
    ctx = Ctx(sd.name)
    err = None
    try:
        sd.fn(ctx)
    except BaseException as e:  # a crash in a scenario is a failure, not a harness abort
        err = "".join(traceback.format_exception_only(type(e), e)).strip()
        ctx.note("scenario raised " + err)
    violations = []
    for i, run in enumerate(ctx.runs):
        for v in check_run(run):
            violations.append(v if len(ctx.runs) == 1 else f"run{i}: {v}")
    exp = "; ".join(f"{c.name}={c.expected}" for c in ctx.checks) + "; invariants=OK"
    act = "; ".join(f"{c.name}={c.actual}" for c in ctx.checks) + "; invariants=" + (
        "OK" if not violations else f"{len(violations)} violation(s)")
    if err:
        act += f"; ERROR={_s(err)}"
    ok = err is None and not violations and all(c.ok for c in ctx.checks)
    notes = [sd.desc] + ctx.notes + [f"VIOLATION: {v}" for v in violations] + \
        [f"FAILED CHECK {c.name}: expected {c.expected} got {c.actual}" for c in ctx.checks if not c.ok]
    return {"scenario": sd.name, "expected": exp, "actual": act, "passed": ok, "notes": " | ".join(notes),
            "checks": [c.__dict__ for c in ctx.checks], "violations": violations}
