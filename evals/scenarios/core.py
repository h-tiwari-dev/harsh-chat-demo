"""Core booking-flow scenarios: happy path, filtering, corrections, no re-asking."""
from errand.llm import Interpretation, ScriptedLLM

from ..harness import scenario

FIND = "Find a plumber in Indiranagar this week"


def oracle(w, service="plumber", area="Indiranagar", dates=("2026-11-02", "2026-11-08")):
    """Independent ground truth straight from the marketplace (read-only call)."""
    return w.mock.search_providers(service, area, dates)


@scenario("happy_path_book", "search -> choose -> yes -> exactly one verified booking")
def happy(ctx):
    w = ctx.world(force_book_outcomes=["success"])
    r1 = ctx.say(w, FIND)
    ctx.check("phase_after_search", "PRESENTING", r1.phase.name)
    ctx.check("mutations_before_choice", 0, len(w.calls("hold_slot", "book", "cancel", "reschedule")))
    r2 = ctx.say(w, "1")
    ctx.check("phase_after_choice", "AWAITING_APPROVAL", r2.phase.name)
    ctx.check("mutations_before_yes", 0, len(w.calls("hold_slot", "book", "cancel", "reschedule")))
    prop = w.agent.approvals.pending("u1")
    r3 = ctx.say(w, "yes")
    ctx.check("final_phase", "DONE", r3.phase.name)
    nb = w.new_bookings()
    ctx.check("bookings", 1, len(nb))
    if nb:
        ctx.check("booking_matches_proposal", (prop.provider_id, prop.slot, prop.price),
                  (nb[0].provider_id, nb[0].slot, nb[0].price))
        ctx.check("reply_cites_booking", True, nb[0].id in r3.text)


@scenario("budget_time_window_filtering", "options respect budget and morning window; checked against an independent oracle")
def filtering(ctx):
    w = ctx.world()
    probe = oracle(w)
    prices = sorted(p.price for p in probe)
    budget = prices[len(prices) // 2]
    ctx.say(w, f"Find a plumber in Indiranagar this week, mornings only, under ₹{budget}")
    opts = w.agent.state.options
    # ground truth: providers with price<=budget and at least one future slot before 12:00 in the dates
    elig = {p.id for p in probe if p.price <= budget and any(s[11:16] < "12:00" and s > w.clock.now().isoformat(timespec="minutes")
                                                           for s in p.free_slots)}
    ctx.check("budget_slot_recorded", (budget, "12:00"), (w.agent.state.slots.budget, w.agent.state.slots.time_window["latest"]))
    ctx.check("all_options_within_budget", True, all(o["price"] <= budget for o in opts))
    ctx.check("all_slots_in_morning", True, all(o["slot"][11:16] < "12:00" for o in opts))
    ctx.check("options_subset_of_eligible", True, {o["provider_id"] for o in opts} <= elig)
    ctx.check("option_count", min(3, len(elig)), len(opts))
    ctx.note(f"{len(probe)} providers, budget {budget}, {len(elig)} eligible")
    # impossible budget -> no options, explains why, nothing booked
    ctx.say(w, "make it under ₹50")
    ctx.check("impossible_budget_options", 0, len(w.agent.state.options))
    ctx.check("impossible_budget_phase", "COLLECTING", w.phase)


@scenario("correction_keeps_other_slots", "'actually <new day>' changes only the date range; service/area/budget/window kept")
def correction(ctx):
    w = ctx.world()
    ctx.say(w, "Find a plumber in Indiranagar this week, mornings only, under ₹1400")
    before = w.agent.state.slots.public()
    ctx.say(w, "actually make it Thursday")
    after = w.agent.state.slots.public()
    ctx.check("date_changed", ["2026-11-05", "2026-11-05"], after["date_range"])
    same = {k: before[k] for k in ("service", "area", "budget", "time_window", "preferences")}
    ctx.check("other_slots_kept", same, {k: after[k] for k in same})
    ctx.check("options_respect_new_date", True, all(o["slot"].startswith("2026-11-05") for o in w.agent.state.options))
    # a second correction (budget) keeps the date too
    ctx.say(w, "under ₹1300")
    ctx.check("date_kept_after_budget_fix", ["2026-11-05", "2026-11-05"], w.agent.state.slots.public()["date_range"])


@scenario("never_reasks_known_fact", "LLM sees only still-missing fields; agent never asks again for service/area it was given")
def no_reask(ctx):
    w = ctx.world()
    r1 = ctx.say(w, "Find a plumber in Indiranagar")
    ctx.check("known_before_turn2", ("plumber", "Indiranagar"), (lambda s: (s["service"], s["area"]))(
        w.agent.state.slots.public()))
    ctx.check("only_dates_missing", ["date_range"], w.agent._snapshot()["missing"])
    ctx.check("first_question_mentions_day_not_area", (True, False),
              ("day" in r1.text.lower(), "your area" in r1.text.lower()))
    r2 = ctx.say(w, "this week")
    ctx.check("llm_saw_only_dates_missing", ["date_range"], w.llm.snapshots[1][1]["missing"])
    ctx.check("nothing_missing_now", [], w.agent._snapshot()["missing"])
    ctx.check("moves_on_to_options", "PRESENTING", r2.phase.name)
    ctx.check("no_area_question_after", False, "your area" in r2.text.lower())
    # scripted-LLM variant: state known across 3 partial updates, ask count for each field <= 1
    llm = ScriptedLLM([Interpretation("new_request", {"service": "plumber"}),
                       Interpretation("update_slots", {"area": "Indiranagar"}),
                       Interpretation("update_slots", {"date_range": ["2026-11-03", "2026-11-04"]})])
    w2 = ctx.world(ctx.new_run(), llm=llm)
    qs = [ctx.say(w2, t).text.lower() for t in ("a", "b", "c")]
    ctx.check("service_asked_in", 0, sum("what service" in q for q in qs))
    ctx.check("area_asked_times", 1, sum("your area" in q for q in qs))
    ctx.check("scripted_flow_reaches", "PRESENTING", w2.phase)
