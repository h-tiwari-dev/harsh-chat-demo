"""LLM interface and offline implementations.

The LLM ONLY interprets the user's text (+ a state snapshot with no tool text) into an
`Interpretation`. It never calls tools and never approves anything: the agent whitelists intents
and slot keys, and approvals are created by code from the user's raw message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

from .sanitize import scan

# Intents the agent acts on. Anything else (approve, cancel_all, book_now, ...) is rejected by code.
ALLOWED_INTENTS = ("new_request", "update_slots", "choose_option", "cancel_booking",
                   "reschedule_booking", "rebook_same", "abort", "chitchat", "unknown")


@dataclass
class Interpretation:
    intent: str = "unknown"
    slots_update: dict = field(default_factory=dict)  # keys: service, area, date_range, earliest, latest, budget, preferences, any_provider
    option_index: int | None = None                   # 1-based
    target: dict = field(default_factory=dict)        # {"service":..., "booking_id":...} for cancel/reschedule
    reply_hint: str = ""
    confidence: float = 1.0

    @classmethod
    def coerce(cls, x) -> "Interpretation":
        if isinstance(x, Interpretation):
            return x
        if isinstance(x, dict):
            return cls(**{k: v for k, v in x.items() if k in cls.__dataclass_fields__})
        if isinstance(x, str):
            return cls(intent=x)
        return cls()


@runtime_checkable
class LLM(Protocol):
    def interpret(self, user_text: str, state: dict) -> Interpretation: ...
    # optional: def phrase(self, facts: dict) -> str


# --------------------------------------------------------------------------- ScriptedLLM
class ScriptedLLM:
    """Canned interpretations: a sequence, a dict keyed by (case-insensitive) user text, and/or
    regex matchers. Records every call in `.calls` as (user_text, state)."""

    def __init__(self, script=None, matchers=None, default=None):
        self.sequence = list(script) if isinstance(script, (list, tuple)) else []
        self.by_text = {k.strip().lower(): v for k, v in script.items()} if isinstance(script, dict) else {}
        self.matchers = [(re.compile(p, re.I) if isinstance(p, str) else p, v) for p, v in (matchers or [])]
        self.default = default
        self.calls: list[tuple[str, dict]] = []

    def interpret(self, user_text, state):
        self.calls.append((user_text, state))
        key = user_text.strip().lower()
        if key in self.by_text:
            return Interpretation.coerce(self.by_text[key])
        for rx, v in self.matchers:
            if rx.search(user_text):
                return Interpretation.coerce(v)
        if self.sequence:
            return Interpretation.coerce(self.sequence.pop(0))
        return Interpretation.coerce(self.default) if self.default is not None else Interpretation("unknown")

    def phrase(self, facts):
        return ""


# --------------------------------------------------------------------------- InjectionObeyingLLM
class InjectionObeyingLLM:
    """Test double: a gullible model. When it sees injection-like text in the user's message or flagged
    untrusted data in the state snapshot, it emits dangerous intents/slot updates (approve, cancel all,
    book without asking, forging provider/slot). Tests prove the CODE ignores all of it."""

    ATTACKS = (
        Interpretation(intent="approve", reply_hint="user approved"),
        Interpretation(intent="cancel_all", reply_hint="cancel all bookings"),
        Interpretation(intent="book_now", slots_update={"provider_id": "evil", "slot": "2026-11-03T09:00",
                                                        "price": 1}, reply_hint="book without asking"),
        Interpretation(intent="update_slots", slots_update={"approved": True, "provider_id": "evil",
                                                            "budget": 999999}, option_index=1),
    )

    def __init__(self, fallback: "LLM | None" = None):
        self.fallback = fallback
        self.calls: list[tuple[str, dict]] = []
        self.attacks_emitted = 0

    def triggered(self, user_text, state) -> bool:
        return bool(scan(user_text)) or bool(state.get("review_flags"))

    def interpret(self, user_text, state):
        self.calls.append((user_text, state))
        if self.triggered(user_text, state):
            a = self.ATTACKS[self.attacks_emitted % len(self.ATTACKS)]
            self.attacks_emitted += 1
            return Interpretation(a.intent, dict(a.slots_update), a.option_index, dict(a.target), a.reply_hint)
        if self.fallback is not None:
            return self.fallback.interpret(user_text, state)
        return Interpretation("unknown")

    def phrase(self, facts):
        return "Approved! Everything is booked, no confirmation needed."


# --------------------------------------------------------------------------- RuleBasedLLM
_SERVICES = (
    ("ac_servicing", r"\bac\b|\ba/c\b|air[ -]?condition|\bac[- ]serv"),
    ("plumber", r"plumb|\btap\b|\bfaucet|\bpipe|\bleak"),
    ("electrician", r"electric|\bwiring|\bswitchboard"),
    ("cleaner", r"clean|\bmaid\b"),
    ("painter", r"\bpaint"),
    ("carpenter", r"carpent|\bwoodwork"),
)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_NOT_AREA = {w.capitalize() for w in _WEEKDAYS} | {m.capitalize() for m in _MONTHS} | {
    "I", "AC", "The", "My", "Today", "Tomorrow", "Please", "Rs", "INR", "It", "This", "Next"}
_KNOWN_AREAS = ("indiranagar", "koramangala", "hsr layout", "whitefield", "jayanagar", "bandra", "andheri",
                "powai", "dadar", "connaught place", "saket", "hauz khas")
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5}
_TIME_RE = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"


def _hhmm(h, m, ap) -> str | None:
    h, m = int(h), int(m or 0)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.lower().startswith("p") else 0)
    elif 1 <= h <= 7:  # bare "2" for a service visit means afternoon
        h += 12
    if h > 23 or m > 59:
        return None
    return f"{h:02d}:{m:02d}"


def parse_dates(text: str, today: date) -> list | None:
    t = text.lower()
    iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if iso:
        return [iso[0], iso[-1]]
    if re.search(r"\bthis week\b", t):
        return [today.isoformat(), (today + timedelta(days=6 - today.weekday())).isoformat()]
    if re.search(r"\bnext week\b", t):
        mon = today + timedelta(days=7 - today.weekday())
        return [mon.isoformat(), (mon + timedelta(days=6)).isoformat()]
    m = re.search(r"\b(?:in the )?(?:next|within)\s+(\d{1,2})\s+days?\b", t)
    if m:
        return [today.isoformat(), (today + timedelta(days=int(m.group(1)))).isoformat()]
    if re.search(r"\b(this )?weekend\b", t):
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        return [sat.isoformat(), (sat + timedelta(days=1)).isoformat()]
    if re.search(r"\bday after tomorrow\b", t):
        d = today + timedelta(days=2)
        return [d.isoformat()] * 2
    if re.search(r"\btomorrow\b", t):
        d = today + timedelta(days=1)
        return [d.isoformat()] * 2
    if re.search(r"\btoday\b", t):
        return [today.isoformat()] * 2
    mon_rx = "|".join(_MONTHS)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({mon_rx})[a-z]*\b", t) or None
    m2 = re.search(rf"\b({mon_rx})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m or m2:
        day, mon = (int(m.group(1)), m.group(2)) if m else (int(m2.group(2)), m2.group(1))
        try:
            d = date(today.year, _MONTHS.index(mon) + 1, day)
            if d < today:
                d = date(today.year + 1, d.month, d.day)
            return [d.isoformat()] * 2
        except ValueError:
            pass
    for i, w in enumerate(_WEEKDAYS):
        m = re.search(rf"\b(by |before |until )?(?:next |on |this )?{w}\b", t)
        if m:
            delta = (i - today.weekday()) % 7 or (7 if "next" in m.group(0) else 0)
            d = today + timedelta(days=delta)
            if m.group(1):
                return [today.isoformat(), d.isoformat()]
            return [d.isoformat()] * 2
    return None


def parse_times(text: str) -> dict:
    t = text.lower()
    out: dict = {}
    m = re.search(rf"\b(?:not before|no earlier than|after|from|earliest(?: is)?|starting)\s+{_TIME_RE}", t)
    if m and (m.group(3) or m.group(2) or int(m.group(1)) >= 6):
        v = _hhmm(*m.groups())
        if v:
            out["earliest"] = v
    m = re.search(rf"(?<!not )\b(?:before|by|no later than|until|latest(?: is)?)\s+{_TIME_RE}", t)
    if m:
        v = _hhmm(*m.groups())
        if v and (m.group(3) or int(m.group(1)) >= 6):
            out["latest"] = v
    m = re.search(rf"\bbetween\s+{_TIME_RE}\s+(?:and|-|to)\s+{_TIME_RE}", t)
    if m:
        a, b = m.groups()[:3], m.groups()[3:]
        if not a[2] and b[2]:
            a = (a[0], a[1], b[2])
        va, vb = _hhmm(*a), _hhmm(*b)
        if va and vb:
            out["earliest"], out["latest"] = va, vb
    if re.search(r"\bmornings?\b", t) and "latest" not in out:
        out["latest"] = "12:00"
    elif re.search(r"\bafternoons?\b", t):
        out.setdefault("earliest", "12:00"); out.setdefault("latest", "17:00")
    elif re.search(r"\bevenings?\b", t):
        out.setdefault("earliest", "16:00")
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", t)
    if m and "earliest" not in out:
        v = _hhmm(*m.groups())
        if v:
            out["earliest"] = v
            out["latest"] = f"{min(int(v[:2]) + 1, 23):02d}:{v[3:]}"
    return out


def parse_budget(text: str) -> int | None:
    t = text.lower().replace("₹", " ₹")
    num = r"(\d[\d,]*)(?!\s*(?::|a\.?m|p\.?m|days?|hours?|hrs?|km|min))"
    pats = (rf"\b(?:under|below|less than|within|max(?:imum)?|up to|upto|at most|not more than|no more than)\s*(?:rs\.?|inr|₹)?\s*{num}",
            rf"\bbudget\s*(?:of|is|:)?\s*(?:rs\.?|inr|₹)?\s*{num}",
            rf"\bmake it\s*(?:rs\.?|inr|₹)?\s*{num}",
            rf"(?:₹|rs\.?|inr)\s*{num}",
            rf"\b(\d[\d,]*)\s*(?:rupees|rs\b|inr\b)")
    for p in pats:
        m = re.search(p, t)
        if m:
            try:
                v = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if v >= 50:
                return v
    return None


def parse_service(text: str) -> str | None:
    t = text.lower()
    for name, rx in _SERVICES:
        if re.search(rx, t):
            return name
    return None


def parse_area(text: str) -> str | None:
    t = text.lower()
    for a in _KNOWN_AREAS:
        if re.search(rf"\b{re.escape(a)}\b", t):
            return a.title()
    m = re.search(r"\b(?:in|near|around|at)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", text)
    if m and m.group(1).split()[0] not in _NOT_AREA:
        return m.group(1)
    return None


def parse_preferences(text: str) -> list:
    t = text.lower()
    prefs = []
    if re.search(r"\blicen[cs]ed\b|\bcertified\b", t):
        prefs.append("licensed")
    if re.search(r"\bexperienced\b|\bveteran\b", t):
        prefs.append("experienced")
    if re.search(r"\btop[- ]rated\b|\bbest[- ]rated\b|\bhighly rated\b", t):
        prefs.append("top_rated")
    return prefs


class RuleBasedLLM:
    """Offline regex/keyword interpreter so the CLI works with no API key. Uses state['today']."""

    def interpret(self, user_text: str, state: dict) -> Interpretation:
        text = user_text.strip()
        t = text.lower().strip(" .!?")
        today = date.fromisoformat(state.get("today") or date.today().isoformat())
        n_opts = int(state.get("n_options") or 0)
        busy = bool(n_opts or state.get("awaiting_confirmation"))
        slots_in_state = state.get("slots") or {}
        missing = state.get("missing") or []

        # choosing an option
        if n_opts:
            idx = self._choice(t, n_opts)
            if idx:
                return Interpretation("choose_option", option_index=idx)
        if re.fullmatch(r"(no|nope|nah|never ?mind|forget it|stop|cancel (that|it|this)|don'?t( book)?)", t) and busy:
            return Interpretation("abort")
        if re.search(r"\bcancel\b.*\b(all|every|everything)\b|\b(all|every|everything)\b.*\bcancel", t):
            return Interpretation("cancel_all", reply_hint="cancel everything")
        svc = parse_service(text)
        bid = re.search(r"\bBK-[A-Z0-9-]+\b", text, re.I)
        if re.search(r"\bcancel\b", t):
            tgt = {"service": svc} if svc else {}
            if bid:
                tgt["booking_id"] = bid.group(0).upper()
            return Interpretation("cancel_booking", target=tgt)
        if re.search(r"\b(move|reschedule|shift|postpone|push back)\b", t) or (
                re.search(r"\bchange\b", t) and re.search(r"\b(appointment|booking|visit|servicing)\b", t)):
            upd = self._slots(text, today)
            tgt = {"service": svc} if svc else {}
            if bid:
                tgt["booking_id"] = bid.group(0).upper()
            return Interpretation("reschedule_booking", slots_update=upd, target=tgt)
        if bid and not svc and not state.get("flow"):
            return Interpretation("cancel_booking", target={"booking_id": bid.group(0).upper()})
        upd = self._slots(text, today)
        if re.search(r"\bsame\b.{0,30}\b(as|like|from)\b.{0,12}\b(last|before|earlier|previous)", t) or re.search(r"\brebook\b", t):
            return Interpretation("rebook_same", slots_update=upd)
        if re.search(r"\b(any|other|different|someone else|anyone|other options)\b", t) and state.get("flow") == "book":
            upd["any_provider"] = True
        # bare answers to a question
        if not upd and len(text.split()) <= 3 and not re.search(r"\d", text):
            if "area" in missing and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,30}", text) and not re.fullmatch(r"(hi|hello|hey|thanks|thank you|ok|okay|yes|no)", t):
                upd["area"] = text.strip(" .").title()
            elif "service" in missing:
                pass
        if not upd:
            if re.fullmatch(r"(hi|hello|hey|thanks|thank you|help|\?)", t):
                return Interpretation("chitchat")
            return Interpretation("unknown")
        active = bool(slots_in_state.get("service")) or state.get("flow") in ("book", "reschedule")
        phase = state.get("phase")
        if "service" in upd and (not active or phase in ("DONE",)):
            return Interpretation("new_request", slots_update=upd)
        return Interpretation("update_slots" if active else "new_request", slots_update=upd)

    @staticmethod
    def _choice(t: str, n: int) -> int | None:
        if len(t.split()) > 8:
            return None
        m = re.fullmatch(r"(?:i(?:'ll| will)? (?:take|choose|pick|want|go with)|go with|take|choose|pick|let'?s (?:go with|do))?\s*(?:the\s+)?(?:option|number|no\.?|#)?\s*(\d)(?:\s*(?:one|please))?", t)
        if m and 1 <= int(m.group(1)) <= n:
            return int(m.group(1))
        for word, i in _ORD.items():
            if re.search(rf"\b{word}\b", t) and i <= n and re.search(r"\b(one|option|please|go|take|choose|pick|book|that)\b|^the ", t):
                return i
        if re.search(r"\blast one\b", t):
            return n
        return None

    @staticmethod
    def _slots(text: str, today: date) -> dict:
        upd: dict = {}
        svc = parse_service(text)
        if svc:
            upd["service"] = svc
        area = parse_area(text)
        if area:
            upd["area"] = area
        dr = parse_dates(text, today)
        if dr:
            upd["date_range"] = dr
        upd.update(parse_times(text))
        b = parse_budget(text)
        if b:
            upd["budget"] = b
        prefs = parse_preferences(text)
        if prefs:
            upd["preferences"] = prefs
        return upd
