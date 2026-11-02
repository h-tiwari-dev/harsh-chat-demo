"""Regression tests from the adversarial review (see docs/bugs_log.md)."""
from agent_env import Env
from errand.approval import ApprovalStore, Proposal
from errand.executor import BookingExecutor
from errand.sanitize import sanitize_for_trace
from errand.storage import Storage
from errand.trace import Tracer
from errand.limits import Budget
from errand.clock import FakeClock
from errand.mocks.provider import MockProviderAPI, Scenario


def _approve(ap, uid, action, **kw):
    p = Proposal.build("P", action, uid, **kw)
    ap.propose(p)
    assert ap.grant_from_user_message(uid, "yes")


def test_regression_rebook_same_slot_after_cancel_is_really_booked():
    clock = FakeClock()
    api = MockProviderAPI(Scenario(force_book_outcomes=["success", "success"], cancel_first_call_503=False), clock)
    st = Storage()
    ap = ApprovalStore(st)
    ex = BookingExecutor(api, ap, st, Tracer(None, clock), Budget(clock), clock)
    pid = api.search_providers("plumber", "Indiranagar", ("2026-11-03", "2026-11-09"))[0]
    slot = pid.free_slots[0]
    kw = dict(provider_id=pid.id, slot=slot, price=pid.price)
    _approve(ap, "u1", "book", **kw)
    r1 = ex.execute("u1", "book", **kw)
    assert r1.status == "booked"
    _approve(ap, "u1", "cancel", provider_id=pid.id, slot=slot, price=pid.price, booking_id=r1.booking_id)
    assert ex.execute("u1", "cancel", provider_id=pid.id, slot=slot, price=pid.price,
                      booking_id=r1.booking_id).status == "cancelled"
    _approve(ap, "u1", "book", **kw)
    r3 = ex.execute("u1", "book", **kw)
    live = [b for b in api.all_bookings() if b.status == "booked" and b.provider_id == pid.id and b.slot == slot]
    assert live, f"user told {r3.status} but no live booking exists"


def test_regression_trace_redacts_secrets_inside_lists_under_sensitive_keys():
    out = sanitize_for_trace({"api_key": ["abc123supersecret"], "credentials": {"user": "bob", "pass": "hunter2"}})
    assert "abc123supersecret" not in str(out) and "hunter2" not in str(out)



def test_regression_forgotten_area_not_kept_in_booking_history_or_reused_by_reschedule():
    e = Env(force_book_outcomes=["success"])
    e.to_proposal("Find a plumber in Indiranagar this week")
    assert "BK-" in e.say("yes").text
    e.say("forget my area")
    assert all(m.get("area") is None for m in e.storage.kv_items("agent_bookings").values())
    e.say("Reschedule my plumber booking to next week")
    # the search area comes from the provider's own record, never from the forgotten memory/history value
    assert e.calls("get_provider")
