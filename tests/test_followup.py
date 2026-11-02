"""Proactive follow-up: provider cancels -> agent finds a replacement -> normal approval before rebooking."""
from agent_env import Env
from errand.state import Phase

SEED = "BK-SEED-2"  # AC servicing, 2026-11-05T11:00, status booked


def booked(e):
    return e.calls("book")


def test_cancel_notifies_but_books_nothing_until_yes():
    e = Env(force_book_outcomes=["success"])
    assert e.agent.check_followups() is None          # nothing cancelled yet
    e.mock.provider_cancels(SEED)
    r = e.agent.check_followups()
    assert r is not None and SEED in r.text and "cancelled" in r.text
    assert r.phase == Phase.PRESENTING and r.data["followup"]["options"] >= 1
    assert not e.mutating() and not e.new_bookings()
    e.say("1")                                         # picking only creates a Proposal
    assert e.agent.state.phase == Phase.AWAITING_APPROVAL
    assert e.agent.approvals.pending("u1") is not None
    assert not e.mutating() and len(booked(e)) == 0
    r = e.say("yes")
    assert r.phase == Phase.DONE and len(booked(e)) == 1 and len(e.new_bookings()) == 1
    assert e.new_bookings()[0].service == "ac_servicing"
    assert e.mock.list_notifications("u1")[0]["booking_id"] == SEED


def test_repeated_check_does_not_renotify():
    e = Env()
    e.mock.provider_cancels(SEED)
    assert e.agent.check_followups() is not None
    assert e.agent.check_followups() is None
    assert e.agent.check_followups() is None
    assert len(e.events("provider_cancelled_detected")) == 1
    assert not e.mutating()


def test_restart_between_notification_and_approval_keeps_proposal():
    e = Env(path=":memory:", force_book_outcomes=["success"])
    e.mock.provider_cancels(SEED)
    e.agent.check_followups()
    e.say("1")
    prop = e.agent.approvals.pending("u1")
    assert prop is not None
    e2 = Env(mock=e.mock, storage=e.storage, clock=e.clock, resume=True)
    assert e2.agent.approvals.pending("u1").fingerprint == prop.fingerprint
    assert "Still waiting" in (e2.agent.startup_message or "")
    assert e2.agent.check_followups() is None          # not re-notified after restart
    assert not e2.mutating()
    r = e2.say("yes")
    assert r.phase == Phase.DONE and len(booked(e2)) == 1 and len(e2.new_bookings()) == 1


def test_no_replacement_gives_clear_message():
    e = Env()
    e.mock.provider_cancels(SEED)
    e.agent.state.slots.budget = 1                     # saved budget nobody can meet
    r = e.agent.check_followups()
    assert "nothing that fits" in r.text and "Nothing has been booked" in r.text
    assert e.agent.state.options == [] and not e.mutating()
    assert e.agent.check_followups() is None


def test_followup_respects_saved_constraints_and_counts_budget():
    e = Env()
    e.mock.provider_cancels(SEED)
    e.agent.state.slots.time_window["earliest"] = "14:00"
    before = e.agent.budget.snapshot()
    e.agent.check_followups()
    assert all(o["slot"][11:16] >= "14:00" and o["slot"] <= "2026-11-06T11:00" for o in e.agent.state.options)
    assert all(o["provider_id"] != "prov-ac-coolcare" for o in e.agent.state.options)
    assert e.agent.budget.snapshot() != before         # tool calls were counted


def test_cancelled_provider_review_injection_stays_data():
    e = Env()
    e.mock.provider_cancels(SEED)
    r = e.agent.check_followups()
    assert not e.mutating() and e.agent.approvals.pending("u1") is None
    assert "ignored" in r.text or "Review" in r.text or r.data
