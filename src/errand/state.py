"""Workflow state: Phase enum, Slots, WorkflowState (persisted to Storage after every transition)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum

NS_WORKFLOW = "workflow"


class Phase(Enum):
    COLLECTING = "COLLECTING"
    SEARCHING = "SEARCHING"
    PRESENTING = "PRESENTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    RECOVERING = "RECOVERING"
    DONE = "DONE"
    FAILED = "FAILED"
    LIMIT_EXHAUSTED = "LIMIT_EXHAUSTED"


REQUIRED_BY_FLOW = {
    "book": ("service", "area", "date_range"),
    "reschedule": ("date_range",),
    "cancel": (),
}
SLOT_UPDATE_KEYS = ("service", "area", "date_range", "earliest", "latest", "budget",
                    "preferences", "any_provider")
KNOWN_PREFERENCES = ("licensed", "experienced", "top_rated")


@dataclass
class Slots:
    service: str | None = None
    area: str | None = None
    date_range: list | None = None            # [start_iso_date, end_iso_date]
    time_window: dict = field(default_factory=lambda: {"earliest": None, "latest": None})  # "HH:MM"; latest exclusive
    budget: int | None = None                 # max price, rupees
    preferences: list = field(default_factory=list)
    provider_id: str | None = None            # set by code on selection
    slot: str | None = None                   # set by code on selection
    rebook_provider_id: str | None = None     # "same provider as last time"
    any_provider: bool = False                # user waived the rebook restriction
    prefilled: dict = field(default_factory=dict)  # slot -> value taken from usable memory

    def missing(self, flow: str = "book") -> list[str]:
        out = []
        for k in REQUIRED_BY_FLOW.get(flow, ()):
            if not getattr(self, k):
                out.append(k)
        return out

    def merge(self, upd: dict) -> list[str]:
        """Merge an (already validated) update; never wipes other slots. Returns changed keys."""
        changed = []
        for k, v in upd.items():
            if v is None:
                continue
            if k in ("earliest", "latest"):
                if self.time_window.get(k) != v:
                    self.time_window[k] = v
                    changed.append(k)
            elif k == "preferences":
                for p in v:
                    if p not in self.preferences:
                        self.preferences.append(p)
                        changed.append("preferences")
            elif k in ("service", "area", "date_range", "budget", "any_provider"):
                if getattr(self, k) != v:
                    setattr(self, k, v)
                    changed.append(k)
                    self.prefilled.pop(k, None)
        return changed

    def search_key(self) -> list:
        return [self.service, self.area, self.date_range]

    def public(self) -> dict:
        """What an LLM may see. No provider/tool data."""
        return {"service": self.service, "area": self.area, "date_range": self.date_range,
                "time_window": dict(self.time_window), "budget": self.budget,
                "preferences": list(self.preferences)}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Slots":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


@dataclass
class WorkflowState:
    user_id: str
    phase: Phase = Phase.COLLECTING
    flow: str | None = None                   # book | cancel | reschedule
    slots: Slots = field(default_factory=Slots)
    candidates: list = field(default_factory=list)   # raw search results (dicts)
    search_key: list | None = None
    options: list = field(default_factory=list)      # what was shown to the user (numbered)
    selected: dict | None = None
    target_booking: dict | None = None
    excluded: list = field(default_factory=list)     # [[provider_id, slot], ...] known taken
    rebook: bool = False
    pending_proposal_id: str | None = None
    proposal_counter: int = 0
    in_flight: dict | None = None             # action being executed (for restart recovery)
    journal_refs: list = field(default_factory=list)
    last_result: dict | None = None
    pending_memory_key: str | None = None
    review_notes: dict = field(default_factory=dict)  # provider_id -> {"snippet","flagged"}
    review_flags: list = field(default_factory=list)  # flag NAMES only
    limit_kind: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.name
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowState":
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names and k not in ("phase", "slots")}
        return cls(phase=Phase[d.get("phase", "COLLECTING")],
                   slots=Slots.from_dict(d.get("slots") or {}), **kw)

    def save(self, storage) -> None:
        storage.kv_set(NS_WORKFLOW, self.user_id, self.to_dict())

    @classmethod
    def load(cls, storage, user_id: str) -> "WorkflowState | None":
        d = storage.kv_get(NS_WORKFLOW, user_id)
        return cls.from_dict(d) if d else None
