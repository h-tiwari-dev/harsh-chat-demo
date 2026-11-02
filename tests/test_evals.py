"""Runs the offline eval suite under pytest, and tests that the shared invariant checker really catches violations."""
from dataclasses import replace

import pytest
from evals import run as evals_run
from evals.harness import REGISTRY, RunRecord, World, run_scenario
from evals.invariants import check_run

evals_run.load()
NAMES = [s.name for s in REGISTRY]


def test_at_least_18_scenarios_with_required_coverage():
    assert len(NAMES) >= 18
    required = {"happy_path_book", "budget_time_window_filtering", "correction_keeps_other_slots", "never_reasks_known_fact",
                "prompt_injection_in_reviews", "prompt_injection_in_provider_name", "price_change_after_approval",
                "slot_change_after_approval", "timeout_after_success", "timeout_lost", "slot_taken_at_book",
                "reschedule_slot_conflict", "reschedule_success", "cancel_503_then_success", "repeated_confirmation_yes_x3",
                "memory_correct", "memory_forget", "inferred_memory_requires_confirmation", "restart_mid_booking",
                "restart_while_awaiting_approval", "limit_turns", "limit_tool_calls", "limit_wall_clock",
                "unknown_exception_failed_with_message", "cancel_everything_refused"}
    assert required <= set(NAMES), required - set(NAMES)
    assert any(n.startswith("statistical") for n in NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_scenario_passes(name):
    sd = next(s for s in REGISTRY if s.name == name)
    r = run_scenario(sd)
    assert r["passed"], r["notes"]
    assert r["expected"] and r["actual"]


def test_report_is_generated_from_results():
    results = evals_run.run_all({"happy_path_book", "limit_turns"})
    md = evals_run.render_md(results)
    assert "| scenario | expected | actual | pass/fail | notes |" in md
    assert md.count("| PASS |") == 2
    # a failing result renders as FAIL (the report is not hardcoded)
    results[0]["passed"] = False
    assert "| FAIL |" in evals_run.render_md(results)


# ---------------------------------------------------------------- the checker must catch violations
def _run_with_booking():
    w = World(force_book_outcomes=["success"], cancel_first_call_503=False)
    run = RunRecord(); run.worlds.append(w); run.mock = w.mock
    for t in ("Find a plumber in Indiranagar this week", "1", "yes"):
        r = w.agent.handle(t)
        run.replies.append((t, r.text, r.phase.name))
    assert check_run(run) == []
    return w, run


def test_checker_flags_unapproved_mutation():
    w, run = _run_with_booking()
    w.mock.cancel("BK-SEED-2", "evil-key")
    assert any(v.startswith("(1)") for v in check_run(run))


def test_checker_flags_duplicate_booking():
    w, run = _run_with_booking()
    b = w.new_bookings()[0]
    w.mock._bookings["BK-DUP"] = replace(b, id="BK-DUP")
    assert any(v.startswith("(2)") for v in check_run(run))


def test_checker_flags_unverified_success_claim():
    w, run = _run_with_booking()
    run.replies.append(("x", "Booked and confirmed. Reference BK-9999: nobody.", "DONE"))
    vs = check_run(run)
    assert any(v.startswith("(3)") for v in vs)


def test_checker_flags_forgotten_value_and_secret_and_empty_reply():
    w, run = _run_with_booking()
    run.forgotten.append({"value": "Vikram", "reply_idx": 0, "trace": {}, "snap": {}})
    w.tracer.events.append({"type": "event", "kind": "leak", "t": "key sk-abcdef123456"})
    run.replies.append(("hi", "   ", "DONE"))
    vs = check_run(run)
    assert any(v.startswith("(4)") for v in vs)
    assert any(v.startswith("(5)") for v in vs)
    assert any(v.startswith("(6)") for v in vs)


# ---------------------------------------------------------------- regressions for bugs found by the evals (docs/bugs_log.md)
def test_regression_forget_prefilled_area_leaves_no_trace_in_workflow_state():
    import json
    w = World()
    for t in ("my area is Secretville", "Find a plumber this week", "forget my area"):
        w.agent.handle(t)
    assert "secretville" not in json.dumps(w.storage.kv_items("workflow")).lower()
    assert w.agent.state.candidates == [] and w.agent.state.search_key is None


@pytest.mark.parametrize("action_text,tool,pick", [("Cancel my AC servicing", "cancel", None),
                                                    ("Move my AC servicing to Saturday", "reschedule", "1")])
def test_regression_restart_after_unapplied_cancel_or_reschedule_is_not_stuck(tmp_path, action_text, tool, pick):
    from evals.harness import CrashNow
    path = str(tmp_path / "x.db")
    w = World(path=path, flaky=True)
    w.agent.handle(action_text)
    if pick:
        w.agent.handle(pick)
    w.api.crash_on.add(tool)
    with pytest.raises(CrashNow):
        w.agent.handle("yes")
    w.storage.close()
    w2 = World(path=path, mock=w.mock, clock=w.clock, resume=True)
    assert w2.agent.state.phase.name != "RECOVERING"
    assert "not applied" in (w2.agent.startup_message or "")
    assert w.booking("BK-SEED-2").status == "booked"
