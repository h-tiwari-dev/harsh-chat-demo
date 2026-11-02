import pytest
from errand.approval import ApprovalStore, Proposal
from errand.clock import FakeClock
from errand.executor import BookingExecutor
from errand.limits import Budget, LimitExceeded
from errand.mocks.provider import MockProviderAPI, Scenario
from errand.models import ToolError
from errand.storage import Storage
from errand.trace import Tracer

U = "u1"


class Env:
    def __init__(self, path=":memory:", api=None, storage=None, clock=None, **sc):
        self.clock = clock or FakeClock()
        self.storage = storage or Storage(path)
        self.api = api or MockProviderAPI(Scenario(**sc), self.clock)
        self.approvals = ApprovalStore(self.storage)
        self.tracer = Tracer(None, self.clock)
        self.budget = Budget(self.clock, wall_clock_s=10_000)
        self.ex = BookingExecutor(self.api, self.approvals, self.storage, self.tracer, self.budget, self.clock)
        prov = self.api.search_providers("electrician", "Indiranagar", ("2026-11-03", "2026-11-10"))
        p = next(p for p in prov if p.free_slots)
        self.pid, self.slot, self.price = p.id, p.free_slots[0], p.price

    def approve(self, action="book", price=None, **kw):
        price = self.price if price is None else price
        self.approvals.propose(Proposal.build("pr", action, U, kw.pop("provider_id", self.pid),
                                              kw.pop("slot", self.slot), price, **kw))
        assert self.approvals.grant_from_user_message(U, "yes")

    def book(self, price=None):
        return self.ex.execute(U, "book", self.pid, self.slot, self.price if price is None else price)

    def calls(self, tool):
        return [c for c in self.api.call_log if c["tool"] == tool]


def test_no_approval_no_tool_calls():
    e = Env()
    n = len(e.api.call_log)
    r = e.book()
    assert r.status == "failed" and not r.verified
    assert len(e.api.call_log) == n and not e.api.all_bookings()


def test_wrong_price_approval_rejected():
    e = Env(); e.approve(price=e.price + 1)
    assert e.book().status == "failed"
    assert not e.calls("book")


def test_happy_path_journal_and_single_use():
    e = Env(force_book_outcomes=["success"]); e.approve()
    r = e.book()
    assert r.status == "booked" and r.verified and r.booking_id
    j = e.storage.kv_get("journal", r.idempotency_key)
    assert j["state"] == "BOOKED"
    assert r.idempotency_key.startswith(f"{U}:book:")
    # repeated execute: already_done, no second book call, approval gone
    r2 = e.book()
    assert r2.status == "already_done" and r2.booking_id == r.booking_id
    assert len(e.calls("book")) == 1 and len(e.api.all_bookings()) == 1


def test_journal_pending_before_first_call():
    e = Env(force_book_outcomes=["success"]); e.approve()
    seen = {}
    orig = e.api.hold_slot
    def spy(pid, slot):
        seen.update(e.storage.kv_items("journal"))
        return orig(pid, slot)
    e.api.hold_slot = spy
    e.book()
    assert [v["state"] for v in seen.values()] == ["PENDING"]


def test_approval_consumed_second_execute_after_failure_denied():
    e = Env(force_book_outcomes=["slot_taken"]); e.approve()
    assert e.book().status == "slot_taken"
    assert e.book().status == "failed"  # needs fresh approval
    assert len(e.calls("book")) == 1


def test_price_drift_voids_without_booking():
    e = Env(hold_price_delta={}); e.approve()
    e.api.scenario.hold_price_delta[e.pid] = 150
    r = e.book()
    assert r.status == "needs_reapproval" and r.new_price == e.price + 150 and not r.verified
    assert not e.calls("book") and not e.api.all_bookings()
    assert e.approvals.pending(U) is None


def test_slot_taken():
    e = Env(force_book_outcomes=["slot_taken"]); e.approve()
    r = e.book()
    assert r.status == "slot_taken" and not r.verified
    assert e.storage.kv_get("journal", r.idempotency_key)["state"] == "FAILED"


def test_timeout_but_booked_is_verified_without_rebook():
    e = Env(force_book_outcomes=["timeout_booked"]); e.approve()
    r = e.book()
    assert r.status == "booked" and r.verified
    assert len(e.calls("book")) == 1 and len(e.api.all_bookings()) == 1
    assert e.calls("get_booking_status")


def test_timeout_lost_retries_same_key_then_succeeds():
    e = Env(force_book_outcomes=["timeout_lost", "success"]); e.approve()
    t0 = e.clock.now()
    r = e.book()
    assert r.status == "booked" and r.verified
    keys = {c["args"]["idempotency_key"] for c in e.calls("book")}
    assert keys == {r.idempotency_key}
    assert e.clock.now() > t0  # backed off via clock.sleep
    assert len(e.api.all_bookings()) == 1


def test_persistent_timeouts_unverified_bounded():
    e = Env(force_book_outcomes=["timeout_lost"] * 10); e.approve()
    r = e.book()
    assert r.status == "unverified" and not r.verified
    assert len(e.calls("book")) == e.ex.max_retries + 1
    assert e.storage.kv_get("journal", r.idempotency_key)["state"] == "UNKNOWN"
    assert not e.api.all_bookings()


def test_hold_expired_rehold_once_same_price():
    e = Env(force_book_outcomes=["success"], hold_ttl_s=60); e.approve()
    orig = e.api.hold_slot
    n = {"i": 0}
    def hold(pid, slot):
        n["i"] += 1
        h = orig(pid, slot)
        if n["i"] == 1:
            e.clock.advance(120)  # first hold expires before book
        return h
    e.api.hold_slot = hold
    r = e.book()
    assert r.status == "booked" and n["i"] == 2


def test_hold_expired_with_price_change_needs_reapproval():
    e = Env(force_book_outcomes=["success"], hold_ttl_s=60); e.approve()
    orig = e.api.hold_slot
    n = {"i": 0}
    def hold(pid, slot):
        n["i"] += 1
        h = orig(pid, slot)
        if n["i"] == 1:
            e.clock.advance(120)
            e.api.scenario.hold_price_delta[pid] = 99
        return h
    e.api.hold_slot = hold
    r = e.book()
    assert r.status == "needs_reapproval" and r.new_price == e.price + 99
    assert not e.api.all_bookings()


def test_cancel_503_retried_with_same_key():
    e = Env(seed_bookings=None) if False else Env()
    e.api._bookings["BK-X"] = __import__("errand.models", fromlist=["Booking"]).Booking(
        "BK-X", U, e.pid, "electrician", e.slot, e.price, "booked")
    e.approvals.propose(Proposal.build("pr", "cancel", U, e.pid, e.slot, e.price, booking_id="BK-X"))
    e.approvals.grant_from_user_message(U, "yes")
    t0 = e.clock.now()
    r = e.ex.execute(U, "cancel", e.pid, e.slot, e.price, booking_id="BK-X")
    assert r.status == "cancelled" and r.verified
    calls = e.calls("cancel")
    assert len(calls) == 2 and len({c["args"]["idempotency_key"] for c in calls}) == 1
    assert e.clock.now() > t0
    assert e.ex.execute(U, "cancel", e.pid, e.slot, e.price, booking_id="BK-X").status == "already_done"


def test_cancel_persistent_503_bounded():
    e = Env()
    def boom(bid, key):
        raise ToolError("service_unavailable", "503", transient=True)
    e.api.cancel = boom
    e.approvals.propose(Proposal.build("pr", "cancel", U, e.pid, e.slot, e.price, booking_id="B"))
    e.approvals.grant_from_user_message(U, "yes")
    r = e.ex.execute(U, "cancel", e.pid, e.slot, e.price, booking_id="B")
    assert r.status == "unverified" and not r.verified


def test_reschedule_conflict_no_state_change():
    e = Env()
    from errand.models import Booking
    e.api._bookings["BK-X"] = Booking("BK-X", U, e.pid, "electrician", e.slot, e.price, "booked")
    new = "2026-11-09T15:00"
    e.api.scenario.reschedule_conflict_slots.add(new)
    e.approvals.propose(Proposal.build("pr", "reschedule", U, e.pid, e.slot, e.price, booking_id="BK-X", new_slot=new))
    e.approvals.grant_from_user_message(U, "yes")
    r = e.ex.execute(U, "reschedule", e.pid, e.slot, e.price, booking_id="BK-X", new_slot=new)
    assert r.status == "slot_conflict" and not r.verified
    assert e.api._bookings["BK-X"].slot == e.slot


def test_unknown_exception_never_raised():
    e = Env(); e.approve()
    def boom(*a): raise RuntimeError("kaboom")
    e.api.hold_slot = boom
    r = e.book()
    assert r.status == "failed" and "kaboom" in r.message and not r.verified


def test_every_tool_call_counted_and_traced():
    e = Env(force_book_outcomes=["timeout_booked"]); e.approve()
    traced = []
    e.tracer.tool_call = lambda tool, args, **kw: traced.append(tool)
    e.book()
    assert traced == ["hold_slot", "book", "get_booking_status"]
    assert e.budget.snapshot()["tool_calls"] == 3


def test_limit_exceeded_propagates_and_marks_unknown():
    e = Env(); e.approve()
    e.budget = e.ex.budget = Budget(e.clock, max_tool_calls=0)
    with pytest.raises(LimitExceeded):
        e.book()
    assert [v["state"] for v in e.storage.kv_items("journal").values()] == ["UNKNOWN"]


def test_recover_booked_not_rebooked(tmp_path):
    path = str(tmp_path / "s.db")
    e = Env(path, force_book_outcomes=["timeout_booked"]); e.approve()
    # simulate crash right after timeout: journal PENDING, process dies
    e.api.get_booking_status = lambda key: (_ for _ in ()).throw(RuntimeError("crash"))
    r = e.book()
    assert r.status == "unverified" or r.status == "booked"
    del e.api.get_booking_status  # restart: new storage/executor, same (healthy) api
    e2 = Env(api=e.api, storage=Storage(path), clock=e.clock)
    # force state back to PENDING to model a crash before outcome was written
    for k, v in e2.storage.kv_items("journal").items():
        v["state"] = "PENDING"; e2.storage.kv_set("journal", k, v)
    nb = len(e.calls("book"))
    res = e2.ex.recover(U)
    assert len(res) == 1 and res[0].status == "booked" and res[0].verified
    assert len(e.calls("book")) == nb and len(e.api.all_bookings()) == 1
    assert e2.ex.recover(U) == []


def test_recover_not_found_does_not_rebook():
    e = Env(force_book_outcomes=["timeout_lost"] * 10); e.approve()
    e.book()  # UNKNOWN
    n = len(e.calls("book"))
    res = e.ex.recover(U)
    assert res[0].status == "failed" and not res[0].verified
    assert len(e.calls("book")) == n
