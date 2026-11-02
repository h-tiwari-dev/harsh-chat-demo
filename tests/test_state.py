from errand.clock import FakeClock
from errand.state import Phase, Slots, WorkflowState
from errand.storage import Storage


def test_phase_values():
    names = {p.name for p in Phase}
    assert names == {"COLLECTING", "SEARCHING", "PRESENTING", "AWAITING_APPROVAL", "EXECUTING",
                     "RECOVERING", "DONE", "FAILED", "LIMIT_EXHAUSTED"}


def test_missing_only_required():
    s = Slots()
    assert s.missing() == ["service", "area", "date_range"]
    s.merge({"service": "plumber", "budget": 1500})
    assert s.missing() == ["area", "date_range"]
    s.merge({"area": "Indiranagar", "date_range": ["2026-11-02", "2026-11-08"]})
    assert s.missing() == []  # time window / budget / preferences are optional
    assert Slots().missing("cancel") == []
    assert Slots().missing("reschedule") == ["date_range"]


def test_correction_merges_and_keeps_everything_else():
    s = Slots()
    s.merge({"service": "plumber", "area": "Indiranagar", "date_range": ["2026-11-02", "2026-11-08"],
             "latest": "12:00", "budget": 1500, "preferences": ["licensed"]})
    changed = s.merge({"budget": 2000})
    assert changed == ["budget"]
    assert (s.service, s.area, s.budget) == ("plumber", "Indiranagar", 2000)
    assert s.time_window["latest"] == "12:00" and s.preferences == ["licensed"]
    s.merge({"earliest": "10:00"})
    assert s.time_window == {"earliest": "10:00", "latest": "12:00"}
    s.merge({"preferences": ["licensed", "top_rated"]})
    assert s.preferences == ["licensed", "top_rated"]


def test_none_never_wipes():
    s = Slots(service="plumber")
    s.merge({"service": None, "area": None})
    assert s.service == "plumber"


def test_public_has_no_provider_fields():
    s = Slots(service="x", provider_id="p1", slot="2026-11-03T09:00")
    assert "provider_id" not in s.public() and "slot" not in s.public()


def test_roundtrip_and_persistence(tmp_path):
    ws = WorkflowState("u1", phase=Phase.AWAITING_APPROVAL, flow="book")
    ws.slots.merge({"service": "plumber", "latest": "12:00"})
    ws.options = [{"provider_id": "p", "slot": "2026-11-03T09:00"}]
    d = ws.to_dict()
    assert d["phase"] == "AWAITING_APPROVAL"
    back = WorkflowState.from_dict(d)
    assert back.phase == Phase.AWAITING_APPROVAL and back.slots.time_window["latest"] == "12:00"
    p = str(tmp_path / "s.db")
    st = Storage(p); ws.save(st); st.close()
    again = WorkflowState.load(Storage(p), "u1")
    assert again.to_dict() == d
    assert WorkflowState.load(Storage(p), "nobody") is None
