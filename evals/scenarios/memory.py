"""Memory: correction, forgetting, inferred facts need confirmation."""
import json

from ..harness import World, scenario
from .execution import to_proposal


def searches(w):
    return [c["args"]["area"] for c in w.calls("search_providers")]


@scenario("memory_correct", "user states then corrects a fact; the corrected value (only) drives the next search")
def mem_correct(ctx):
    w = ctx.world()
    ctx.say(w, "my area is Koramangala")
    ctx.say(w, "actually my area is Indiranagar")
    r = ctx.say(w, "show my memory")
    ctx.check("usable_memory", {"area": "Indiranagar"}, w.agent.memory.usable("u1"))
    ctx.check("view_shows_new_not_old", (True, False), ("Indiranagar" in r.text, "Koramangala" in r.text))
    ctx.say(w, "Find a plumber this week")
    ctx.check("search_uses_corrected_area", ["Indiranagar"], searches(w))
    ctx.check("phase", "PRESENTING", w.phase)


@scenario("memory_forget", "forgotten fact never appears in snapshots/replies/traces/storage, and a NEW session does not reuse it")
def mem_forget(ctx):
    secret = "Secretville"
    w = ctx.world(path=":memory:")
    ctx.say(w, f"my area is {secret}")
    ctx.say(w, "Find a plumber this week")
    ctx.check("used_saved_area_before_forget", [secret], searches(w))
    ctx.say(w, "forget my area")
    ctx.mark_forgotten(w, secret)
    ctx.check("usable_empty", {}, w.agent.memory.usable("u1"))
    r = ctx.say(w, "Find a plumber this week")
    ctx.check("asks_for_area_again", True, "area" in r.text.lower() and w.phase == "COLLECTING")
    ctx.check("no_new_search_with_forgotten", [secret], searches(w))
    ctx.check("workflow_state_clean", False, secret.lower() in json.dumps(w.storage.kv_items("workflow")).lower())
    # brand-new session (new process, same durable storage, fresh agent)
    run2 = ctx.runs[0]
    w2 = World(mock=w.mock, storage=w.storage, clock=w.clock)
    run2.worlds.append(w2)
    ctx.mark_forgotten(w2, secret)
    r2 = ctx.say(w2, "Find an electrician this week")
    ctx.check("new_session_asks_area", True, "area" in r2.text.lower())
    ctx.check("new_session_no_search", [secret], searches(w2))
    ctx.say(w2, "forget everything")
    ctx.check("forget_all_usable_empty", {}, w2.agent.memory.usable("u1"))
    # user may restate it explicitly afterwards
    ctx.say(w2, "my area is Indiranagar")
    ctx.check("restated_fact_usable", {"area": "Indiranagar"}, w2.agent.memory.usable("u1"))


def _book_once(ctx, run=None):
    w = ctx.world(run, force_book_outcomes=["success"])
    to_proposal(ctx, w)
    ctx.say(w, "yes")
    return w


@scenario("inferred_memory_requires_confirmation", "facts inferred after a booking are never used silently; yes -> used, no -> forgotten")
def inferred(ctx):
    w = _book_once(ctx)
    mem = w.agent.memory
    ctx.check("inferred_not_usable", {}, mem.usable("u1"))
    ctx.check("needs_confirmation_keys", ["area", "preferred_technician"], sorted(f.key for f in mem.needs_confirmation("u1")))
    n0 = len(w.calls("search_providers"))
    r = ctx.say(w, "Find a plumber this week")
    ctx.check("asks_before_use", (True, "area"), ("Use it?" in r.text, w.agent.state.pending_memory_key))
    ctx.check("no_search_before_answer", n0, len(w.calls("search_providers")))
    ctx.say(w, "yes")
    ctx.check("confirmed_then_used", (["Indiranagar"], "Indiranagar"), (searches(w)[n0:], mem.usable("u1").get("area")))
    ctx.check("technician_still_unconfirmed", False, "preferred_technician" in mem.usable("u1"))
    # rejection path in a second independent run
    w2 = _book_once(ctx, ctx.new_run())
    ctx.say(w2, "Find a plumber this week")
    r = ctx.say(w2, "no")
    ctx.check("rejected_not_usable", {}, w2.agent.memory.usable("u1"))
    ctx.check("rejected_asks_area_instead", True, "area" in r.text.lower())
    n = len(w2.calls("search_providers"))
    ctx.say(w2, "Find a plumber this week")
    ctx.check("rejected_inference_not_reproposed", n, len(w2.calls("search_providers")))
