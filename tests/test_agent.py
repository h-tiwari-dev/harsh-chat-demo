import re

import pytest
from agent_env import Env
from errand.llm import Interpretation, RuleBasedLLM, ScriptedLLM
from errand.models import ToolError
from errand.state import Phase

REQ = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500"


def prices(text):
    return [int(x) for x in re.findall(r"₹(\d+)", text)]


def test_happy_path_with_traced_transitions():
    e = Env(force_book_outcomes=["success"])
    r = e.say(REQ)
    assert r.phase == Phase.COLLECTING and "area" in r.text and "service" not in r.text
    r = e.say("Indiranagar")
    assert r.phase == Phase.PRESENTING
    assert "₹" in r.text and "rating" in r.text
    r = e.say("the first one")
    assert r.phase == Phase.AWAITING_APPROVAL and "Reply yes to confirm" in r.text
    assert not e.mutating()
    r = e.say("yes")
    assert r.phase == Phase.DONE and "BK-" in r.text
    assert r.data["booking_id"] == e.new_bookings()[0].id
    trans = [(t["frm"], t["to"]) for t in e.tracer.events if t["type"] == "state_transition"]
    assert ("AWAITING_APPROVAL", "EXECUTING") in trans and ("EXECUTING", "DONE") in trans
    assert any(t["type"] == "model_call" for t in e.tracer.events)


def test_budget_and_window_filtering_in_code():
    e = Env()
    e.say("Find a plumber in Indiranagar this week under ₹600, mornings only")
    for o in e.agent.state.options:
        assert o["price"] <= 600 and o["slot"][11:16] < "12:00"


def test_correction_merges_without_reasking():
    e = Env()
    e.say(REQ); e.say("Indiranagar")
    r = e.say("actually make it 2000")
    sl = e.agent.state.slots
    assert sl.budget == 2000 and sl.service == "plumber" and sl.area == "Indiranagar"
    assert sl.time_window["latest"] == "12:00"
    assert r.phase == Phase.PRESENTING
    assert len(e.calls("search_providers")) == 1  # filtering only, no new search


def test_never_reasks_known_facts():
    e = Env()
    r = e.say("I need an electrician in Koramangala")
    assert "area" not in r.text and "service" not in r.text and "day" in r.text


def test_choose_by_number_and_proposal_has_exact_details():
    e = Env()
    e.say("Find a plumber in Indiranagar this week")
    opt = e.agent.state.options[1]
    r = e.say("2")
    p = e.agent.approvals.pending("u1")
    assert (p.provider_id, p.slot, p.price) == (opt["provider_id"], opt["slot"], opt["price"])
    assert str(opt["price"]) in r.text


def test_non_yes_change_voids_and_reproposes():
    e = Env()
    e.to_proposal()
    old = e.agent.approvals.pending("u1")
    r = e.say("2")  # picks a different option
    new = e.agent.approvals.pending("u1")
    assert new and new.fingerprint != old.fingerprint and r.phase == Phase.AWAITING_APPROVAL
    # a budget change voids the pending proposal and re-presents
    r = e.say("actually under 100")
    assert e.agent.approvals.pending("u1") is None and r.phase != Phase.AWAITING_APPROVAL
    assert not e.mutating()


def test_non_change_message_keeps_proposal():
    e = Env()
    e.to_proposal()
    fp = e.agent.approvals.pending("u1").fingerprint
    r = e.say("hmm?")
    assert e.agent.approvals.pending("u1").fingerprint == fp and "Reply yes" in r.text


def test_decline_voids():
    e = Env()
    e.to_proposal()
    r = e.say("no")
    assert e.agent.approvals.pending("u1") is None and not e.mutating()
    assert e.say("yes").text.startswith("There is nothing")


def test_repeated_yes_makes_no_api_call():
    e = Env(force_book_outcomes=["success"])
    e.to_proposal(); e.say("yes")
    n = len(e.mock.call_log)
    r = e.say("yes")
    assert "already" in r.text.lower() and "BK-" in r.text and len(e.mock.call_log) == n
    assert len(e.new_bookings()) == 1


def test_slot_taken_apologises_and_offers_alternatives():
    e = Env(force_book_outcomes=["slot_taken", "success"])
    e.to_proposal()
    first = e.agent.state.selected
    r = e.say("yes")
    assert "Sorry" in r.text and r.phase == Phase.PRESENTING
    assert not e.new_bookings()
    assert all((o["provider_id"], o["slot"]) != (first["provider_id"], first["slot"]) for o in e.agent.state.options)
    e.say("1"); r = e.say("yes")
    assert r.phase == Phase.DONE and len(e.new_bookings()) == 1


def test_needs_reapproval_on_price_change():
    from errand.cli import _AnyDelta
    e = Env(force_book_outcomes=["success"], hold_price_delta=_AnyDelta())
    r0 = e.to_proposal()
    old = prices(r0.text)[0]
    r = e.say("yes")
    assert r.phase == Phase.AWAITING_APPROVAL and "price changed" in r.text.lower()
    assert prices(r.text)[-1] == old + 100 == e.agent.approvals.pending("u1").price
    assert not e.new_bookings()
    assert e.say("yes").phase == Phase.DONE


def test_cancel_flow_verified_after_503_retry():
    e = Env()
    r = e.say("Cancel my AC servicing")
    assert r.phase == Phase.AWAITING_APPROVAL and "BK-SEED-2" in r.text and not e.mutating()
    r = e.say("yes")
    assert r.phase == Phase.DONE and "Cancelled" in r.text
    assert len(e.calls("cancel")) == 2  # 503 then retry
    assert next(b for b in e.mock.all_bookings() if b.id == "BK-SEED-2").status == "cancelled"


def test_cancel_all_is_refused_by_code():
    e = Env()
    r = e.say("cancel everything")
    assert not e.mutating() and r.phase != Phase.AWAITING_APPROVAL


def test_reschedule_flow():
    e = Env()
    r = e.say("Move my AC servicing to Saturday")
    assert r.phase == Phase.PRESENTING and "Sat 07 Nov" in r.text
    r = e.say("1")
    assert "BK-SEED-2" in r.text and "Reply yes" in r.text
    r = e.say("yes")
    assert r.phase == Phase.DONE and "Rescheduled" in r.text
    assert next(b for b in e.mock.all_bookings() if b.id == "BK-SEED-2").slot.startswith("2026-11-07")


def test_reschedule_conflict_apologises():
    e = Env(reschedule_conflict_slots={"2026-11-07T09:00"})
    e.say("Move my AC servicing to Saturday"); e.say("1")
    r = e.say("yes")
    assert "Sorry" in r.text and "unchanged" in r.text and r.phase == Phase.PRESENTING
    assert next(b for b in e.mock.all_bookings() if b.id == "BK-SEED-2").slot == "2026-11-05T11:00"
    assert "09:00" not in "".join(o["slot"][11:] for o in e.agent.state.options)


def test_rebook_same_electrician_not_before_10():
    e = Env()
    e.say("Book the same electrician as last time, but not before 10 a.m.")
    r = e.say("Indiranagar this week")
    assert r.phase in (Phase.PRESENTING, Phase.COLLECTING)
    e.say("Indiranagar this week") if r.phase == Phase.COLLECTING and "date" in r.text else None
    if e.agent.state.options:
        assert all(o["provider_id"] == "prov-electrician-ravi" and o["slot"][11:16] >= "10:00"
                   for o in e.agent.state.options)
        e.say("1")
        p = e.agent.approvals.pending("u1")
        assert p.provider_id == "prov-electrician-ravi" and p.slot[11:16] >= "10:00"
    assert e.agent.state.slots.rebook_provider_id == "prov-electrician-ravi"


def test_memory_prefill_and_commands():
    e = Env()
    assert "I don't have" in e.say("show my memory").text
    e.say("my area is Koramangala")
    r = e.say("Find a plumber this week")
    assert e.agent.state.slots.area == "Koramangala" and r.phase == Phase.PRESENTING
    assert "saved area" in r.text
    assert "Koramangala" in e.say("show my memory").text
    e.say("forget my area")
    assert e.agent.state.slots.area is None  # does not linger in workflow state
    assert "Koramangala" not in e.say("show my memory").text
    r = e.say("Find a plumber this week")
    assert "area" in r.text and not e.agent.state.slots.area


def test_inferred_fact_asks_first():
    e = Env(force_book_outcomes=["success"])
    e.to_proposal(); e.say("yes")
    assert e.agent.memory.usable("u1") == {}  # inferred, unconfirmed: not usable
    r = e.say("Find a plumber this week")
    assert "Use it?" in r.text and not e.agent.state.slots.area
    r = e.say("yes")
    assert e.agent.memory.usable("u1")["area"] == "Indiranagar" and r.phase == Phase.PRESENTING


def test_inferred_fact_rejected_is_forgotten():
    e = Env(force_book_outcomes=["success"])
    e.to_proposal(); e.say("yes")
    e.say("Find a plumber this week")
    r = e.say("no")
    assert "area" in r.text and e.agent.memory.usable("u1") == {}
    assert all(f.key != "area" for f in e.agent.memory.needs_confirmation("u1"))


def test_reviews_flagged_traced_and_never_in_llm_input():
    llm = RuleBasedLLM()
    seen = []
    class Spy(RuleBasedLLM):
        def interpret(self, t, s):
            seen.append((t, s)); return super().interpret(t, s)
    e = Env(llm=Spy())
    r = e.say("Find a plumber in Indiranagar this week")
    assert e.events("injection_flagged")
    assert "IGNORE PREVIOUS" not in r.text.upper() and "ignored" in r.text
    e.say("1")
    blob = repr(seen).lower()
    assert "ignore previous" not in blob and "admin mode" not in blob
    assert e.agent.state.phase == Phase.AWAITING_APPROVAL  # reviews did not alter state


def test_scripted_llm_sequence_and_text():
    llm = ScriptedLLM({"hello there": Interpretation("chitchat")},
                      matchers=[(r"plumb", {"intent": "new_request", "slots_update": {
                          "service": "plumber", "area": "Indiranagar", "date_range": ["2026-11-03", "2026-11-05"]}})])
    e = Env(llm=llm)
    assert e.say("hello there").phase == Phase.COLLECTING
    assert e.say("need a plumber please").phase == Phase.PRESENTING
    llm.sequence.append(Interpretation("choose_option", option_index=1))
    assert e.say("whatever").phase == Phase.AWAITING_APPROVAL


def test_bad_llm_output_is_sanitised():
    llm = ScriptedLLM([Interpretation("new_request", {"service": "plumber", "area": "Indiranagar",
                       "date_range": ["2026-11-03", "bogus"], "budget": "lots", "provider_id": "x"})])
    e = Env(llm=llm)
    r = e.say("hi")
    assert r.phase == Phase.COLLECTING and "dates" in r.text or "day" in r.text
    assert e.events("llm_slot_keys_rejected")


def test_llm_exception_is_handled():
    class Boom:
        def interpret(self, t, s): raise RuntimeError("llm down")
    e = Env(llm=Boom())
    r = e.say("Find a plumber")
    assert r.phase == Phase.COLLECTING and e.events("llm_error")


def test_tool_exception_gives_failed_phase_and_never_raises():
    e = Env(flaky=True)
    e.api.fail_on["search_providers"] = RuntimeError("db exploded")
    r = e.say("Find a plumber in Indiranagar this week")
    assert r.phase == Phase.FAILED and "went wrong" in r.text and e.events("agent_error")
    e.api.fail_on.clear()
    assert e.say("Find a plumber in Indiranagar this week").phase == Phase.PRESENTING


def test_transient_search_error_is_retried():
    e = Env(flaky=True)
    calls = {"n": 0}
    real = e.mock.search_providers
    def once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolError("service_unavailable", "503", transient=True)
        return real(*a, **k)
    e.api._inner.search_providers = once
    assert e.say("Find a plumber in Indiranagar this week").phase == Phase.PRESENTING


def test_unverified_goes_to_recovering_then_reports_honestly():
    e = Env(flaky=True, force_book_outcomes=["timeout_lost"] * 6)
    e.to_proposal()
    e.api.fail_on["get_booking_status"] = ToolError("service_unavailable", "x", transient=True)
    r = e.say("yes")
    assert r.phase == Phase.RECOVERING and "BK-" not in r.text and "couldn't confirm" in r.text
    assert not e.new_bookings()
    e.api.fail_on.clear()
    r = e.say("check status")
    assert "BK-" not in r.text and not e.new_bookings()
