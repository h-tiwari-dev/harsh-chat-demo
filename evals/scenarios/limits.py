"""Budget exhaustion: turns, model calls, tool calls, wall clock (+ exhaustion in the middle of an execution)."""
from ..harness import MUTATING, scenario
from .core import FIND


def _drive(ctx, w, msgs):
    r = None
    for m in msgs:
        r = ctx.say(w, m)
        if r.phase.name == "LIMIT_EXHAUSTED":
            break
    return r


def _terminal_checks(ctx, w, r, kind):
    ctx.check("phase", "LIMIT_EXHAUSTED", r.phase.name)
    ctx.check("limit_kind", [kind], [e["limit"] for e in w.events("limit_exceeded")])
    ctx.check("clear_message", True, len(r.text) > 40 and "stopped" in r.text.lower())
    n = len(w.mock.call_log)
    r2 = ctx.say(w, "yes")
    ctx.check("terminal_no_more_work", (n, "LIMIT_EXHAUSTED", None),
              (len(w.mock.call_log), r2.phase.name, w.agent.approvals.pending("u1")))


@scenario("limit_turns", "max_turns=2 -> LIMIT_EXHAUSTED with a clear message; later input does no work")
def lim_turns(ctx):
    w = ctx.world(budget_cfg={"max_turns": 2})
    r = _drive(ctx, w, [FIND, "1", "yes", "again"])
    _terminal_checks(ctx, w, r, "turns")
    ctx.check("no_mutation", 0, len(w.calls(*MUTATING)))


@scenario("limit_tool_calls", "max_tool_calls=2 -> exhausted during search/reviews; nothing mutated")
def lim_tools(ctx):
    w = ctx.world(budget_cfg={"max_tool_calls": 2})
    r = _drive(ctx, w, [FIND, "1", "yes"])
    _terminal_checks(ctx, w, r, "tool_calls")
    ctx.check("no_mutation", 0, len(w.calls(*MUTATING)))


@scenario("limit_model_calls", "max_model_calls=2 -> exhausted; nothing mutated")
def lim_model(ctx):
    w = ctx.world(budget_cfg={"max_model_calls": 2})
    r = _drive(ctx, w, [FIND, "hmm", "hmm", "yes"])  # approval turns need no model call, so use free-text turns
    _terminal_checks(ctx, w, r, "model_calls")
    ctx.check("no_mutation", 0, len(w.calls(*MUTATING)))


@scenario("limit_wall_clock", "slow tool pushes past wall_clock_s=60 -> exhausted with time-limit message")
def lim_clock(ctx):
    w = ctx.world(budget_cfg={"wall_clock_s": 60}, flaky=True)
    w.api.hooks["search_providers"] = lambda: w.clock.advance(120)
    ctx.say(w, FIND)
    r = ctx.say(w, "1")
    ctx.check("time_message", True, "time limit" in r.text)
    _terminal_checks(ctx, w, r, "wall_clock")


@scenario("limit_exhausted_mid_execution", "tool budget runs out between hold and book: outcome not claimed, no booking, no retry loop")
def lim_mid(ctx):
    w = ctx.world(budget_cfg={"max_tool_calls": 5}, force_book_outcomes=["success"])
    ctx.say(w, FIND)
    ctx.say(w, "1")
    r = ctx.say(w, "yes")
    ctx.check("phase", "LIMIT_EXHAUSTED", r.phase.name)
    ctx.check("book_not_called", 0, len(w.calls("book")))
    ctx.check("no_booking", 0, len(w.new_bookings()))
    ctx.check("no_success_claim", False, "BK-" in r.text or "confirmed" in r.text.lower().replace("not", ""))
    ctx.check("reply_flags_unfinished_action", True, "may not have finished" in r.text)
