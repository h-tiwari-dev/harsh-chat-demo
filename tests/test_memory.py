import pytest
from errand.storage import Storage
from errand.memory import MemoryStore, parse_memory_command


@pytest.fixture
def m():
    return MemoryStore(Storage(":memory:"))


def test_persists_across_sessions(tmp_path):
    p = str(tmp_path / "e.db")
    s1 = Storage(p); MemoryStore(s1).set_user_fact("u", "area", "Indiranagar")
    MemoryStore(s1).propose_inferred("u", "earliest_time", "10:00"); s1.close()
    m2 = MemoryStore(Storage(p))
    assert m2.usable("u") == {"area": "Indiranagar"}
    assert [f.key for f in m2.needs_confirmation("u")] == ["earliest_time"]


def test_rejects_unknown_keys(m):
    for fn in (m.set_user_fact, m.propose_inferred, m.correct):
        with pytest.raises(ValueError):
            fn("u", "favorite_color", "blue")
    with pytest.raises(ValueError):
        m.forget("u", "ssn")


def test_user_fact_usable_immediately(m):
    f = m.set_user_fact("u", "area", "Koramangala")
    assert f.source == "user" and f.confirmed
    assert m.usable("u") == {"area": "Koramangala"}


def test_inferred_not_usable_until_confirmed(m):
    f = m.propose_inferred("u", "preferred_technician", "Priya")
    assert f.source == "inferred" and not f.confirmed
    assert m.usable("u") == {}
    assert [x.key for x in m.needs_confirmation("u")] == ["preferred_technician"]
    m.confirm("u", "preferred_technician")
    assert m.usable("u") == {"preferred_technician": "Priya"}
    assert m.needs_confirmation("u") == []


def test_correct_replaces_and_marks_user(m):
    m.propose_inferred("u", "area", "HSR")
    f = m.correct("u", "area", "Whitefield")
    assert (f.value, f.source, f.confirmed) == ("Whitefield", "user", True)
    assert m.usable("u") == {"area": "Whitefield"}
    assert m.needs_confirmation("u") == []


def test_inferred_does_not_overwrite_user_fact(m):
    m.set_user_fact("u", "area", "HSR")
    m.propose_inferred("u", "area", "Other")
    assert m.usable("u") == {"area": "HSR"}


def test_forget_one_tombstones(m):
    m.set_user_fact("u", "area", "HSR"); m.set_user_fact("u", "earliest_time", "10:00")
    assert m.forget("u", "area") == 1
    raw = m.s.kv_get("memory", "u:area")
    assert raw["value"] is None and raw["forgotten"] is True
    assert "HSR" not in str(m.s.kv_items("memory"))
    assert m.usable("u") == {"earliest_time": "10:00"}
    assert [f.key for f in m.view("u")] == ["earliest_time"]


def test_forget_all(m):
    m.set_user_fact("u", "area", "HSR"); m.propose_inferred("u", "earliest_time", "10:00")
    assert m.forget("u") == 2
    assert m.usable("u") == {} and m.view("u") == [] and m.needs_confirmation("u") == []
    assert "HSR" not in str(m.s.kv_items("memory")) and "10:00" not in str(m.s.kv_items("memory"))
    # other users unaffected
    m.set_user_fact("v", "area", "X"); m.forget("u"); assert m.usable("v") == {"area": "X"}


def test_inferred_on_forgotten_is_noop_until_user_restates(m):
    m.set_user_fact("u", "area", "HSR"); m.forget("u", "area")
    f = m.propose_inferred("u", "area", "HSR")
    assert f.forgotten and f.value is None
    assert m.view("u") == [] and m.usable("u") == {} and m.needs_confirmation("u") == []
    m.set_user_fact("u", "area", "Indiranagar")
    assert m.usable("u") == {"area": "Indiranagar"}


def test_forget_all_blocks_inference(m):
    m.forget("u")
    m.propose_inferred("u", "earliest_time", "09:00")
    assert m.view("u") == []


def test_forgotten_persists_across_sessions(tmp_path):
    p = str(tmp_path / "e.db")
    s = Storage(p); MemoryStore(s).set_user_fact("u", "area", "HSR"); MemoryStore(s).forget("u", "area"); s.close()
    m2 = MemoryStore(Storage(p)); m2.propose_inferred("u", "area", "HSR")
    assert m2.view("u") == []


def test_view_shows_provenance(m):
    m.set_user_fact("u", "area", "HSR"); m.propose_inferred("u", "earliest_time", "10:00")
    v = {f.key: f for f in m.view("u")}
    assert v["area"].source == "user" and "stated" in v["area"].provenance
    assert v["earliest_time"].source == "inferred" and "unconfirmed" in v["earliest_time"].provenance
    m.confirm("u", "earliest_time")
    assert "confirmed" in {f.key: f for f in m.view("u")}["earliest_time"].provenance


def test_confirm_missing_raises(m):
    with pytest.raises(KeyError):
        m.confirm("u", "area")


@pytest.mark.parametrize("text,exp", [
    ("show my memory", {"op": "view", "key": None, "value": None}),
    ("Show my memory!", {"op": "view", "key": None, "value": None}),
    ("forget my area", {"op": "forget", "key": "area", "value": None}),
    ("forget everything", {"op": "forget_all", "key": None, "value": None}),
    ("my area is Indiranagar", {"op": "set", "key": "area", "value": "Indiranagar"}),
    ("actually my area is HSR Layout", {"op": "correct", "key": "area", "value": "HSR Layout"}),
    ("remember I prefer technician Priya", {"op": "set", "key": "preferred_technician", "value": "Priya"}),
    ("no, earliest time is 10am", {"op": "correct", "key": "earliest_time", "value": "10:00"}),
    ("no, earliest time is 9:30 pm", {"op": "correct", "key": "earliest_time", "value": "21:30"}),
    ("my earliest time is 12am", {"op": "set", "key": "earliest_time", "value": "00:00"}),
    ("earliest time is 14:15", {"op": "set", "key": "earliest_time", "value": "14:15"}),
])
def test_parse(text, exp):
    assert parse_memory_command(text) == exp


@pytest.mark.parametrize("text", ["book me a haircut", "yes", "earliest time is banana", "earliest time is 25:00", ""])
def test_parse_none(text):
    assert parse_memory_command(text) is None
