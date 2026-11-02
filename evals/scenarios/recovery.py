"""Process restarts: crash mid-booking and while awaiting approval."""
import os
import tempfile

from ..harness import CrashNow, World, scenario
from .execution import to_proposal


def _crash(ctx, w, text):
    try:
        ctx.say(w, text)
    except CrashNow:
        return True
    return False


def _restart(ctx, w, path):
    run = ctx._run_of(w)
    w.storage.close()
    w2 = World(path=path, mock=w.mock, clock=w.clock, resume=True)
    run.worlds.append(w2)
    ctx.startup(w2)
    return w2


@scenario("restart_mid_booking", "process dies after provider booked (timeout): resume reconciles via status, never re-books")
def restart_mid(ctx):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "e.db")
        w = ctx.world(path=path, flaky=True, force_book_outcomes=["timeout_booked", "success"])
        to_proposal(ctx, w)
        w.api.crash_on.add("get_booking_status")
        ctx.check("crashed", True, _crash(ctx, w, "yes"))
        ctx.check("provider_booked_before_crash", 1, len(w.new_bookings()))
        w2 = _restart(ctx, w, path)
        bid = w.new_bookings()[0].id
        ctx.check("startup_cites_provider_booking", True, bid in (w2.agent.startup_message or ""))
        ctx.check("phase_done", "DONE", w2.phase)
        ctx.check("bookings", 1, len(w.new_bookings()))
        ctx.check("book_calls", 1, len(w.calls("book")))
        r = ctx.say(w2, "yes")
        ctx.check("late_yes_is_noop", (1, 1, True), (len(w.new_bookings()), len(w.calls("book")), "already" in r.text.lower()))

        # variant: crash *at* the book call (request never reached the provider)
        path2 = os.path.join(d, "f.db")
        v = ctx.world(ctx.new_run(), path=path2, flaky=True, force_book_outcomes=["success", "success"])
        to_proposal(ctx, v)
        v.api.crash_on.add("book")
        ctx.check("crashed_at_book", True, _crash(ctx, v, "yes"))
        v.api.crash_on.clear()
        v2 = _restart(ctx, v, path2)
        ctx.check("nothing_booked_not_claimed", (0, False), (len(v.new_bookings()), "BK-" in (v2.agent.startup_message or "")))
        ctx.check("approval_not_reusable", None, v2.agent.approvals.pending("u1"))
        ctx.check("no_blind_rebook", 0, len(v.calls("book")) - 0 if v.calls("book") == [] else len(v.calls("book")))
        r = ctx.say(v2, "yes")
        ctx.check("bare_yes_after_crash_books_nothing", 0, len(v.new_bookings()))
        ctx.check("reply_nonempty_noop", True, bool(r.text.strip()))


@scenario("restart_while_awaiting_approval", "restart while a proposal waits: state resumes, first yes books once, second yes is a no-op")
def restart_await(ctx):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "e.db")
        w = ctx.world(path=path, force_book_outcomes=["success", "success"])
        to_proposal(ctx, w)
        prop = w.agent.approvals.pending("u1")
        w2 = _restart(ctx, w, path)
        ctx.check("welcome_back", True, "Still waiting" in (w2.agent.startup_message or ""))
        ctx.check("phase_resumed", "AWAITING_APPROVAL", w2.phase)
        ctx.check("same_pending_proposal", prop.fingerprint, w2.agent.approvals.pending("u1").fingerprint)
        ctx.check("no_mutation_on_resume", 0, len(w.calls("hold_slot", "book")))
        ctx.say(w2, "yes")
        r = ctx.say(w2, "yes")
        ctx.check("bookings", 1, len(w.new_bookings()))
        ctx.check("book_calls", 1, len(w.calls("book")))
        ctx.check("second_yes_phase", "DONE", r.phase.name)


def _boom(_r):
    raise CrashNow("after provider applied the change")


@scenario("restart_mid_cancel", "crash during cancel: if provider never applied it, user can retry; if it did, resume verifies via bookings list")
def restart_cancel(ctx):
    with tempfile.TemporaryDirectory() as d:
        # (a) process dies BEFORE the cancel reaches the provider
        path = os.path.join(d, "a.db")
        w = ctx.world(path=path, flaky=True)
        ctx.say(w, "Cancel my AC servicing")
        w.api.crash_on.add("cancel")
        ctx.check("crashed_before_cancel", True, _crash(ctx, w, "yes"))
        w.api.crash_on.clear()
        w2 = _restart(ctx, w, path)
        ctx.check("provider_still_booked", "booked", w.booking("BK-SEED-2").status)
        ctx.check("not_stuck_in_recovering", True, w2.phase != "RECOVERING")
        ctx.check("no_success_claim_on_resume", False, "cancelled" in (w2.agent.startup_message or "").lower().replace("did not", ""))
        ctx.say(w2, "Cancel my AC servicing")
        ctx.check("fresh_proposal_possible", "AWAITING_APPROVAL", w2.phase)
        r = ctx.say(w2, "yes")
        ctx.check("cancel_completes_after_retry", ("DONE", "cancelled"), (r.phase.name, w.booking("BK-SEED-2").status))
        # (b) process dies AFTER the provider cancelled
        path = os.path.join(d, "b.db")
        v = ctx.world(ctx.new_run(), path=path, flaky=True)
        ctx.say(v, "Cancel my AC servicing")
        v.api.post["cancel"] = _boom
        ctx.check("crashed_after_cancel", True, _crash(ctx, v, "yes"))
        v.api.post.clear()
        v2 = _restart(ctx, v, path)
        ctx.check("provider_cancelled", "cancelled", v.booking("BK-SEED-2").status)
        ctx.check("resume_verifies_and_reports", ("DONE", True), (v2.phase, "BK-SEED-2" in (v2.agent.startup_message or "")))
        ctx.check("cancel_calls_total", True, len({c["args"]["idempotency_key"] for c in v.calls("cancel")}) == 1)


@scenario("restart_mid_reschedule", "crash during reschedule before provider applied it: booking unchanged, user not stuck, retry works once")
def restart_resched(ctx):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a.db")
        w = ctx.world(path=path, flaky=True)
        ctx.say(w, "Move my AC servicing to Saturday")
        ctx.say(w, "1")
        orig = w.booking("BK-SEED-2").slot
        w.api.crash_on.add("reschedule")
        ctx.check("crashed", True, _crash(ctx, w, "yes"))
        w.api.crash_on.clear()
        w2 = _restart(ctx, w, path)
        ctx.check("slot_unchanged", orig, w.booking("BK-SEED-2").slot)
        ctx.check("not_stuck_in_recovering", True, w2.phase != "RECOVERING")
        ctx.say(w2, "Move my AC servicing to Saturday")
        ctx.say(w2, "1")
        r = ctx.say(w2, "yes")
        ctx.check("reschedule_completes", ("DONE", True), (r.phase.name, w.booking("BK-SEED-2").slot != orig))
