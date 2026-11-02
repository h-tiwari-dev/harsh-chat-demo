"""Shared helpers to build a full Agent over the mock marketplace (importable from tests as `agent_env`)."""
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
    """Delegates to the real mock; can crash / fail selected tools."""
    def __init__(self, inner):
        self._inner = inner
        self.crash_on: set[str] = set()
        self.fail_on: dict[str, Exception] = {}
        self.hooks: dict[str, callable] = {}

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
            return attr(*a, **k)
        return wrapped


class Env:
    def __init__(self, path=":memory:", llm=None, user="u1", mock=None, storage=None, clock=None,
                 budget_cfg=None, flaky=False, resume=False, **scenario):
        self.user = user
        self.clock = clock or FakeClock()
        self.mock = mock or MockProviderAPI(Scenario(**scenario), self.clock, default_seed_bookings(user))
        self.api = FlakyAPI(self.mock) if flaky else self.mock
        self.storage = storage or Storage(path)
        self.tracer = Tracer(None, self.clock)
        self.llm = llm or RuleBasedLLM()
        ctor = Agent.resume if resume else Agent
        self.agent = ctor(user, self.api, self.llm, self.storage, self.clock, tracer=self.tracer,
                          budget_cfg=budget_cfg)

    def say(self, text):
        return self.agent.handle(text)

    def calls(self, *tools):
        return [c for c in self.mock.call_log if c["tool"] in tools]

    def mutating(self):
        return self.calls(*MUTATING)

    def events(self, kind=None):
        ev = [e for e in self.tracer.events if e["type"] == "event"]
        return [e for e in ev if kind is None or e.get("kind") == kind]

    def to_proposal(self, text="Find a plumber in Indiranagar this week", pick="1"):
        self.say(text)
        return self.say(pick)

    def new_bookings(self):
        return [b for b in self.mock.all_bookings() if not b.id.startswith("BK-SEED")]
