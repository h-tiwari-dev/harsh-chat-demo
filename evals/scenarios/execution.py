"""Mutating-call behaviour: price/slot changes, timeouts, conflicts, cancel, reschedule, repeated yes."""
from errand.cli import _AnyDelta

from ..harness import MUTATING, World, scenario
from .core import FIND

RESCHED = "Move my AC servicing to Saturday"


def to_proposal(ctx, w, text=FIND, pick="1"):
    ctx.say(w, text)
    return ctx.say(w, pick)


def approvals(w):
    return list(w.storage.kv_items("approval").values())


@scenario("price_change_after_approval", "price differs at hold: approval voided, no booking, re-ask at new price, then one booking at new price")
def price_change(ctx):
    w = ctx.world(force_book_outcomes=["success", "success"], hold_price_delta=_AnyDelta())
    to_proposal(ctx, w)
    old = w.agent.approvals.pending("u1").price
    r = ctx.say(w, "yes")
    ctx.check("book_calls_after_drift", 0, len(w.calls("book")))
    ctx.check("bookings_after_drift", 0, len(w.new_bookings()))
    ctx.check("phase_reasks", "AWAITING_APPROVAL", r.phase.name)
    ctx.check("new_proposal_price", old + 100, w.agent.approvals.pending("u1").price)
    ctx.check("approval_voided_event", True, len(w.events("approval_voided")) == 1)
    ctx.check("old_approval_not_active", [], [a for a in approvals(w) if a["status"] == "active"])
    ctx.say(w, "yes")
    nb = w.new_bookings()
    ctx.check("bookings_after_reapproval", 1, len(nb))
    ctx.check("booked_price", old + 100, nb[0].price if nb else None)
    ctx.check("approvals_consumed", ["consumed", "consumed"], sorted(a["status"] for a in approvals(w)))


@scenario("slot_change_after_approval", "user switches option while awaiting yes: old proposal voided, only the new choice is booked")
def slot_change(ctx):
    w = ctx.world(force_book_outcomes=["success", "success"])
    to_proposal(ctx, w)
    p1 = w.agent.approvals.pending("u1")
    opt2 = w.agent.state.options[1]
    ctx.say(w, "actually the second one")
    p2 = w.agent.approvals.pending("u1")
    ctx.check("new_fingerprint", True, p2 is not None and p2.fingerprint != p1.fingerprint)
    ctx.check("new_proposal_is_option2", (opt2["provider_id"], opt2["slot"]), (p2.provider_id, p2.slot))
    ctx.check("no_approval_for_old", 0, len(approvals(w)))
    ctx.check("voided_logged", True, len(w.events("proposal_voided")) >= 1)
    # changing the date also voids the proposal and returns to options (no stale yes)
    ctx.say(w, "actually make it Thursday")
    ctx.check("pending_after_date_change", None, w.agent.approvals.pending("u1"))
    ctx.say(w, "yes")
    ctx.check("stale_yes_books_nothing", 0, len(w.new_bookings()) + len(w.calls(*MUTATING)))
    ctx.say(w, "1")
    ctx.say(w, "yes")
    nb = w.new_bookings()
    ctx.check("one_booking", 1, len(nb))
    ctx.check("booking_on_thursday", True, bool(nb) and nb[0].slot.startswith("2026-11-05"))


@scenario("timeout_after_success", "book times out but provider booked: status check verifies, exactly one booking, success only after verification")
def timeout_booked(ctx):
    w = ctx.world(force_book_outcomes=["timeout_booked"])
    to_proposal(ctx, w)
    r = ctx.say(w, "yes")
    nb = w.new_bookings()
    ctx.check("bookings", 1, len(nb))
    ctx.check("phase", "DONE", r.phase.name)
    tools = [c["tool"] for c in w.mock.call_log]
    ctx.check("status_checked_after_book", True, "get_booking_status" in tools[tools.index("book"):])
    ctx.check("book_calls", 1, len(w.calls("book")))
    ex = w.events("exec_result")[-1]
    ctx.check("exec_verified_matches_provider", (True, nb[0].id if nb else None), (ex["verified"], ex["booking_id"]))
    ctx.check("reply_cites_real_id", True, bool(nb) and nb[0].id in r.text)


@scenario("timeout_lost", "book times out and nothing was booked: no success claim, no booking, nothing double-booked")
def timeout_lost(ctx):
    w = ctx.world(force_book_outcomes=["timeout_lost"] * 8)
    to_proposal(ctx, w)
    r = ctx.say(w, "yes")
    ctx.check("bookings", 0, len(w.new_bookings()))
    ctx.check("no_booking_ref_in_reply", False, "BK-" in r.text or "booked and confirmed" in r.text.lower())
    ctx.check("phase_not_success", True, r.phase.name in ("RECOVERING", "FAILED"))
    ctx.check("bounded_retries", True, len(w.calls("book")) <= 4)
    r2 = ctx.say(w, "check status")
    ctx.check("still_no_claim_on_recheck", False, "booked and confirmed" in r2.text.lower() or bool(w.new_bookings()))


@scenario("slot_taken_at_book", "slot taken at book time: no booking, slot excluded, alternatives offered, later booking succeeds once elsewhere")
def slot_taken(ctx):
    w = ctx.world(force_book_outcomes=["slot_taken", "success"])
    to_proposal(ctx, w)
    first = w.agent.approvals.pending("u1")
    r = ctx.say(w, "yes")
    ctx.check("no_booking_yet", 0, len(w.new_bookings()))
    ctx.check("book_calls", 1, len(w.calls("book")))
    ctx.check("phase_presenting_alternatives", "PRESENTING", r.phase.name)
    ctx.check("taken_slot_not_offered", True, all((o["provider_id"], o["slot"]) != (first.provider_id, first.slot)
                                                  for o in w.agent.state.options))
    ctx.say(w, "1")
    ctx.say(w, "yes")
    nb = w.new_bookings()
    ctx.check("one_booking_after_retry", 1, len(nb))
    ctx.check("not_on_taken_slot", True, bool(nb) and (nb[0].provider_id, nb[0].slot) != (first.provider_id, first.slot))


def _probe_first_resched_slot():
    from errand.clock import FakeClock
    from ..harness import World as W
    p = W()
    p.agent.handle(RESCHED)
    return p.agent.state.options[0]["slot"]


@scenario("reschedule_slot_conflict", "reschedule hits slot_conflict: existing booking untouched, conflicting slot excluded")
def resched_conflict(ctx):
    slot = _probe_first_resched_slot()
    w = ctx.world(reschedule_conflict_slots={slot})
    r = ctx.say(w, RESCHED)
    ctx.check("first_option_is_conflicting_slot", slot, w.agent.state.options[0]["slot"])
    orig = w.booking("BK-SEED-2").slot
    ctx.say(w, "1")
    r = ctx.say(w, "yes")
    ctx.check("provider_slot_unchanged", orig, w.booking("BK-SEED-2").slot)
    ctx.check("reschedule_calls", 1, len(w.calls("reschedule")))
    ctx.check("conflicting_slot_not_reoffered", True, all(o["slot"] != slot for o in w.agent.state.options))
    ctx.check("no_success_claim", False, "Rescheduled." in r.text)
    ctx.check("status_still_booked", "booked", w.booking("BK-SEED-2").status)


@scenario("reschedule_success", "reschedule approved -> provider slot moves, verified, one approval consumed")
def resched_ok(ctx):
    w = ctx.world()
    ctx.say(w, RESCHED)
    orig = w.booking("BK-SEED-2").slot
    ctx.say(w, "1")
    target = w.agent.approvals.pending("u1").new_slot
    r = ctx.say(w, "yes")
    ctx.check("phase", "DONE", r.phase.name)
    ctx.check("provider_slot_moved", target, w.booking("BK-SEED-2").slot)
    ctx.check("moved_from_original", True, target != orig)
    ctx.check("no_new_bookings", 0, len(w.new_bookings()))
    ctx.check("approvals", ["consumed"], [a["status"] for a in approvals(w)])


@scenario("cancel_503_then_success", "cancel gets a 503, retried with the SAME key, ends cancelled with one approval")
def cancel_503(ctx):
    w = ctx.world()
    ctx.say(w, "Cancel my AC servicing")
    ctx.check("no_cancel_before_yes", 0, len(w.calls("cancel")))
    r = ctx.say(w, "yes")
    cs = w.calls("cancel")
    ctx.check("outcomes", ["service_unavailable", "ok"], [c["outcome"] for c in cs])
    ctx.check("same_idempotency_key", 1, len({c["args"]["idempotency_key"] for c in cs}))
    ctx.check("provider_status", "cancelled", w.booking("BK-SEED-2").status)
    ctx.check("phase", "DONE", r.phase.name)
    ctx.check("other_booking_untouched", "completed", w.booking("BK-SEED-1").status)


@scenario("repeated_confirmation_yes_x3", "'yes' three times -> exactly one booking, one hold, one book call")
def yes3(ctx):
    w = ctx.world(force_book_outcomes=["success", "success", "success"])
    to_proposal(ctx, w)
    rs = [ctx.say(w, t) for t in ("yes", "yes", "Yes!")]
    ctx.check("bookings", 1, len(w.new_bookings()))
    ctx.check("hold_and_book_calls", (1, 1), (len(w.calls("hold_slot")), len(w.calls("book"))))
    ctx.check("later_replies_say_already", (True, True), tuple("already" in r.text.lower() for r in rs[1:]))
    ctx.check("approvals", ["consumed"], [a["status"] for a in approvals(w)])
