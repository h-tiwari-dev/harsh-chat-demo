from __future__ import annotations
import re
from dataclasses import dataclass, asdict
from typing import Optional

NS = "memory"
ALLOWED_KEYS = ("area", "earliest_time", "preferred_technician")


@dataclass
class Fact:
    key: str
    value: Optional[str]
    source: str            # "user" | "inferred"
    confirmed: bool
    updated_at: str
    forgotten: bool = False

    @property
    def provenance(self) -> str:
        if self.source == "user":
            return "stated by you"
        return "inferred, confirmed by you" if self.confirmed else "inferred, unconfirmed"


class MemoryStore:
    def __init__(self, storage, clock=None):
        self.s = storage
        self.clock = clock

    # -- internals
    def _now(self) -> str:
        if self.clock is not None:
            return self.clock.now().isoformat(timespec="seconds")
        from datetime import datetime
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _k(user_id: str, key: str) -> str:
        return f"{user_id}:{key}"

    @staticmethod
    def _check_key(key: str) -> None:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unsupported memory key {key!r}; allowed: {ALLOWED_KEYS}")

    def _get(self, user_id: str, key: str) -> Optional[Fact]:
        d = self.s.kv_get(NS, self._k(user_id, key))
        return Fact(**d) if d else None

    def _put(self, user_id: str, f: Fact) -> Fact:
        self.s.kv_set(NS, self._k(user_id, f.key), asdict(f))
        return f

    @staticmethod
    def _check_value(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("memory value must be a non-empty string")
        return value.strip()

    # -- writes
    def set_user_fact(self, user_id, key, value) -> Fact:
        self._check_key(key)
        return self._put(user_id, Fact(key, self._check_value(value), "user", True, self._now(), False))

    def propose_inferred(self, user_id, key, value) -> Fact:
        self._check_key(key)
        value = self._check_value(value)
        cur = self._get(user_id, key)
        if cur is not None:
            if cur.forgotten:
                return cur            # tombstone: no-op until user restates
            if cur.source == "user" or cur.confirmed:
                return cur            # never downgrade a user/confirmed fact
        return self._put(user_id, Fact(key, value, "inferred", False, self._now(), False))

    def confirm(self, user_id, key) -> Fact:
        self._check_key(key)
        cur = self._get(user_id, key)
        if cur is None or cur.forgotten:
            raise KeyError(f"no fact to confirm for {key!r}")
        cur.confirmed = True
        cur.updated_at = self._now()
        return self._put(user_id, cur)

    def correct(self, user_id, key, value) -> Fact:
        return self.set_user_fact(user_id, key, value)

    def forget(self, user_id, key=None) -> int:
        keys = ALLOWED_KEYS if key is None else (key,)
        if key is not None:
            self._check_key(key)
        n = 0
        for k in keys:
            cur = self._get(user_id, k)
            if cur is not None and not cur.forgotten:
                n += 1
            self._put(user_id, Fact(k, None, cur.source if cur else "user", False, self._now(), True))
        return n

    # -- reads
    def _live(self, user_id):
        out = []
        for k in ALLOWED_KEYS:
            f = self._get(user_id, k)
            if f and not f.forgotten and f.value is not None:
                out.append(f)
        return out

    def usable(self, user_id) -> dict:
        return {f.key: f.value for f in self._live(user_id) if f.source == "user" or f.confirmed}

    def needs_confirmation(self, user_id) -> list:
        return [f for f in self._live(user_id) if f.source == "inferred" and not f.confirmed]

    def view(self, user_id) -> list:
        return self._live(user_id)


# ---------------- deterministic command parsing ----------------
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"


def normalize_time(text: str) -> Optional[str]:
    m = re.fullmatch(_TIME, text.strip().lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.startswith("p") else 0)
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


_KEYWORDS = {
    "area": "area", "location": "area",
    "earliest time": "earliest_time", "earliest": "earliest_time",
    "technician": "preferred_technician", "preferred technician": "preferred_technician",
}


def parse_memory_command(text: str) -> Optional[dict]:
    t = text.strip().rstrip(".!").strip()
    low = t.lower()
    if re.fullmatch(r"(please )?(show|view|display) (me )?(my )?memory|what do you remember( about me)?", low):
        return {"op": "view", "key": None, "value": None}
    if re.fullmatch(r"forget (everything|all)( about me)?|clear (my )?memory", low):
        return {"op": "forget_all", "key": None, "value": None}
    m = re.fullmatch(r"forget (?:my )?(area|location|earliest time|technician|preferred technician)", low)
    if m:
        return {"op": "forget", "key": _KEYWORDS[m.group(1)], "value": None}
    m = re.fullmatch(r"(actually,? |no,? )?(?:my )?area is (.+)", t, re.I)
    if m:
        op = "correct" if m.group(1) else "set"
        return {"op": op, "key": "area", "value": m.group(2).strip()}
    m = re.fullmatch(r"(actually,? |no,? )?(?:my )?earliest(?: time)? is (.+)", t, re.I)
    if m:
        v = normalize_time(m.group(2))
        if v is None:
            return None
        return {"op": "correct" if m.group(1) else "set", "key": "earliest_time", "value": v}
    m = re.fullmatch(r"(actually,? |no,? )?(?:please )?remember (?:that )?i prefer (?:technician )?(.+)", t, re.I)
    if m:
        return {"op": "correct" if m.group(1) else "set", "key": "preferred_technician", "value": m.group(2).strip()}
    m = re.fullmatch(r"(actually,? |no,? )?(?:my )?preferred technician is (.+)", t, re.I)
    if m:
        return {"op": "correct" if m.group(1) else "set", "key": "preferred_technician", "value": m.group(2).strip()}
    return None
