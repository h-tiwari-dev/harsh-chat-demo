"""Statistical scenario: default 70/15/15 book outcomes over many seeds; invariants must hold in every run."""
from collections import Counter

from ..harness import scenario
from ..invariants import check_run
from .core import FIND

N_SEEDS = 50


@scenario("statistical_default_outcomes_50_seeds", "default 70/15/15 outcome mix over 50 seeds: zero duplicate bookings, zero unverified success claims")
def stats(ctx):
    outcomes = Counter()
    dup = unverified = other = 0
    for seed in range(N_SEEDS):
        run = ctx.new_run()
        w = ctx.world(run, seed=seed)  # default Scenario(): 0.70 / 0.15 / 0.15, timeout_booked_fraction 0.5
        ctx.say(w, FIND)
        ctx.say(w, "1")
        r = ctx.say(w, "yes")
        for _ in range(3):  # a human would pick again after slot_taken / ask for a re-check
            if w.phase == "PRESENTING" and w.agent.state.options:
                ctx.say(w, "1"); r = ctx.say(w, "yes")
            elif w.phase == "RECOVERING":
                r = ctx.say(w, "check status")
            else:
                break
        outcomes[(w.phase, len(w.new_bookings()))] += 1
        for v in check_run(run):
            if v.startswith("(2)"):
                dup += 1
            elif v.startswith("(3)"):
                unverified += 1
            else:
                other += 1
        if w.new_bookings():
            ctx.check_true(f"seed{seed}_reply_cites_booking", any(b.id in rr[1] for b in w.new_bookings() for rr in run.replies))
    ctx.check("duplicate_booking_violations", 0, dup)
    ctx.check("unverified_success_claim_violations", 0, unverified)
    ctx.check("other_invariant_violations", 0, other)
    ctx.check("runs", N_SEEDS, len(ctx.runs))
    ctx.check("max_bookings_per_run", True, all(len([b for b in r.mock.all_bookings() if not b.id.startswith("BK-SEED")]) <= 1 for r in ctx.runs))
    ctx.note("outcome mix (final phase, bookings): " + ", ".join(f"{k}x{v}" for k, v in sorted(outcomes.items())))
