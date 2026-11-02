import pytest
from errand.clock import FakeClock
from errand.mocks.provider import MockProviderAPI, Scenario, default_seed_bookings
from errand.models import ToolError, ToolTimeout

DR = ("2026-11-03", "2026-11-09")


def mk(**kw):
    clock = FakeClock()
    seed_b = kw.pop("seed_bookings", ())
    return MockProviderAPI(Scenario(**kw), clock, seed_b), clock


def held(api, i=0):
    p = api.search_providers("electrician", "Indiranagar", DR)[i]
    h = api.hold_slot(p.id, p.free_slots[0])
    return p, h


def test_search_shape_and_determinism():
    a, _ = mk(seed=3)
    b, _ = mk(seed=3)
    ra, rb = a.search_providers("electrician", "Indiranagar", DR), b.search_providers("electrician", "Indiranagar", DR)
    assert ra == rb and 3 <= len(ra) <= 8
    for p in ra:
        assert p.free_slots
        assert all("2026-11-03" <= s[:10] <= "2026-11-09" for s in p.free_slots)
    c, _ = mk(seed=4)
    assert c.search_providers("electrician", "Indiranagar", DR) != ra


def test_search_count_range_over_seeds():
    for s in range(30):
        a, _ = mk(seed=s)
        assert 3 <= len(a.search_providers("ac_servicing", "Koramangala", DR)) <= 8


def test_reviews_contain_injection():
    a, _ = mk()
    p = a.search_providers("electrician", "X", DR)[0]
    d = a.get_provider(p.id)
    assert d["provider"].id == p.id
    assert any("ignore previous instructions" in r.text.lower() for r in d["reviews"])
    a2, _ = mk(injection_in_reviews=False)
    p = a2.search_providers("electrician", "X", DR)[0]
    assert not any("ignore previous" in r.text.lower() for r in a2.get_provider(p.id)["reviews"])
    with pytest.raises(ToolError) as e:
        a.get_provider("nope")
    assert e.value.code == "not_found"


def test_hold_and_price_delta():
    a, clock = mk()
    p = a.search_providers("electrician", "X", DR)[0]
    h = a.hold_slot(p.id, p.free_slots[0])
    assert h["quoted_price"] == p.price
    assert h["expires_at"] == "2026-11-02T08:10:00"
    a2, _ = mk(hold_price_delta={"electrician-x-0": 150})
    p2 = a2.search_providers("electrician", "X", DR)[0]
    assert p2.id == "electrician-x-0"
    assert a2.hold_slot(p2.id, p2.free_slots[0])["quoted_price"] == p2.price + 150


def test_hold_unavailable_slot():
    a, _ = mk()
    p = a.search_providers("electrician", "X", DR)[0]
    with pytest.raises(ToolError) as e:
        a.hold_slot(p.id, "2026-11-03T03:00")
    assert e.value.code == "slot_unavailable" and not e.value.transient


def test_book_success_idempotent():
    a, _ = mk(force_book_outcomes=["success"])
    p, h = held(a)
    r1 = a.book(h["hold_id"], "u1:book:abc")
    r2 = a.book(h["hold_id"], "u1:book:abc")
    assert r1 == r2
    bs = a.all_bookings()
    assert len(bs) == 1 and bs[0].user_id == "u1" and bs[0].status == "booked"
    assert a.get_booking_status("u1:book:abc") == {"status": "booked", "booking_id": r1["booking_id"]}
    assert a.get_booking_status("other") == {"status": "not_found"}


def test_book_slot_taken():
    a, _ = mk(force_book_outcomes=["slot_taken"])
    p, h = held(a)
    with pytest.raises(ToolError) as e:
        a.book(h["hold_id"], "k")
    assert e.value.code == "slot_taken"
    assert a.all_bookings() == []
    assert a.get_booking_status("k") == {"status": "failed"}
    with pytest.raises(ToolError):  # same key, same result
        a.book(h["hold_id"], "k")
    with pytest.raises(ToolError) as e:  # slot now gone
        a.hold_slot(p.id, p.free_slots[0])
    assert e.value.code == "slot_unavailable"


def test_timeout_booked_hidden_until_status():
    a, _ = mk(force_book_outcomes=["timeout_booked"])
    p, h = held(a)
    with pytest.raises(ToolTimeout) as e:
        a.book(h["hold_id"], "u:book:1")
    assert e.value.code == "timeout" and e.value.transient
    assert a.get_booking_status("u:book:1")["status"] == "booked"
    assert len(a.all_bookings()) == 1 and len(a.list_bookings("u")) == 1
    # retry with same key never double-books
    r = a.book(h["hold_id"], "u:book:1")
    assert len(a.all_bookings()) == 1 and r["booking_id"] == a.all_bookings()[0].id


def test_timeout_lost_then_retry():
    a, _ = mk(force_book_outcomes=["timeout_lost", "success"])
    p, h = held(a)
    with pytest.raises(ToolTimeout):
        a.book(h["hold_id"], "k")
    assert a.get_booking_status("k") == {"status": "not_found"}
    assert a.all_bookings() == []
    r = a.book(h["hold_id"], "k")
    assert len(a.all_bookings()) == 1 and a.get_booking_status("k")["booking_id"] == r["booking_id"]


def test_hold_expired():
    a, clock = mk(force_book_outcomes=["success"])
    p, h = held(a)
    clock.advance(601)
    with pytest.raises(ToolError) as e:
        a.book(h["hold_id"], "k")
    assert e.value.code == "hold_expired"
    assert a.all_bookings() == []


def test_book_unknown_hold():
    a, _ = mk()
    with pytest.raises(ToolError) as e:
        a.book("H-9999", "k")
    assert e.value.code == "not_found"


def test_book_distribution_and_determinism():
    def run(seed):
        out = []
        a, _ = mk(seed=seed)
        wide = ("2026-11-03", "2026-11-16")
        pairs = [(p.id, s) for sv in ("electrician", "ac_servicing") for ar in ("A", "B", "C", "D")
                 for p in a.search_providers(sv, ar, wide) for s in p.free_slots]
        for i, (pid, s) in enumerate(pairs[:300]):
            try:
                h = a.hold_slot(pid, s)
            except ToolError:
                continue
            try:
                a.book(h["hold_id"], f"k{i}")
                out.append("success")
            except ToolTimeout:
                out.append("timeout")
            except ToolError:
                out.append("slot_taken")
        return out
    r = run(1)
    assert r == run(1)
    n = len(r)
    assert n > 80
    assert 0.55 < r.count("success") / n < 0.85
    assert r.count("slot_taken") > 0 and r.count("timeout") > 0


def test_timeout_booked_fraction():
    a, _ = mk(book_success=0, book_slot_taken=0, book_timeout=1, timeout_booked_fraction=1.0)
    p, h = held(a)
    with pytest.raises(ToolTimeout):
        a.book(h["hold_id"], "k")
    assert len(a.all_bookings()) == 1


def _booked(a):
    p, h = held(a)
    return a.book(h["hold_id"], "u:book:1")["booking_id"]


def test_cancel_503_then_ok_idempotent():
    a, _ = mk(force_book_outcomes=["success"])
    bid = _booked(a)
    with pytest.raises(ToolError) as e:
        a.cancel(bid, "c1")
    assert e.value.code == "service_unavailable" and e.value.transient
    assert a.all_bookings()[0].status == "booked"
    assert a.cancel(bid, "c1")["status"] == "cancelled"
    assert a.cancel(bid, "c1")["status"] == "cancelled"
    assert a.all_bookings()[0].status == "cancelled"
    assert a.cancel(bid, "c2")["status"] == "cancelled"  # second key, no 503 again


def test_cancel_no_503_flag_and_not_found():
    a, _ = mk(force_book_outcomes=["success"], cancel_first_call_503=False)
    bid = _booked(a)
    assert a.cancel(bid, "c")["status"] == "cancelled"
    with pytest.raises(ToolError) as e:
        a.cancel("BK-NOPE", "c9")
    assert e.value.code == "not_found"


def test_reschedule_conflict_unavailable_ok():
    seeds = default_seed_bookings("u")
    a, _ = mk(seed_bookings=seeds, reschedule_conflict_slots={"2026-11-07T10:00"})
    ac = [b for b in a.list_bookings("u") if b.service == "ac_servicing"][0]
    with pytest.raises(ToolError) as e:
        a.reschedule(ac.id, "2026-11-07T10:00", "r1")
    assert e.value.code == "slot_conflict"
    with pytest.raises(ToolError) as e:
        a.reschedule(ac.id, "2026-11-07T03:00", "r2")
    assert e.value.code == "slot_unavailable"
    free = next(s for s in (f"2026-11-07T{h:02d}:00" for h in range(11, 18)) if a._is_free(ac.provider_id, s))
    r = a.reschedule(ac.id, free, "r3")
    assert r == {"booking_id": ac.id, "slot": free}
    assert a.reschedule(ac.id, free, "r3") == r
    assert [b for b in a.all_bookings() if b.id == ac.id][0].slot == free
    with pytest.raises(ToolError) as e:
        a.reschedule("BK-NOPE", free, "r4")
    assert e.value.code == "not_found"


def test_seed_bookings_and_search_includes_known_provider():
    a, _ = mk(seed_bookings=default_seed_bookings("u"))
    bs = a.list_bookings("u")
    assert {b.service for b in bs} == {"electrician", "ac_servicing"}
    assert any(b.status == "completed" and b.service == "electrician" for b in bs)
    assert a.list_bookings("someone") == []
    res = a.search_providers("electrician", "Indiranagar", DR)
    assert "prov-electrician-ravi" in [p.id for p in res] and len(res) <= 8
    assert a.get_provider("prov-electrician-ravi")["provider"].name == "Ravi Electricals"


def test_call_log():
    a, _ = mk(force_book_outcomes=["timeout_booked"])
    p, h = held(a)
    with pytest.raises(ToolTimeout):
        a.book(h["hold_id"], "k")
    tools = [c["tool"] for c in a.call_log]
    assert tools == ["search_providers", "hold_slot", "book"]
    assert a.call_log[-1]["outcome"] == "timeout" and a.call_log[-1]["args"]["idempotency_key"] == "k"
