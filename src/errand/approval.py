"""Approval gate. Approvals are created ONLY by code from the user's raw text."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

AFFIRMATIVE = re.compile(
    r"^\s*(yes|y|yep|yeah|confirm|confirmed|ok|okay|go ahead|book it)\s*[.!]*\s*$",
    re.IGNORECASE,
)

NS_PENDING = "approval_pending"
NS_APPROVAL = "approval"


class ApprovalError(Exception):
    """No valid approval for the requested mutation."""


def fingerprint(action, user_id, provider_id, slot, price, booking_id=None, new_slot=None) -> str:
    payload = {
        "action": action, "user_id": user_id, "provider_id": provider_id,
        "slot": slot, "price": price, "booking_id": booking_id, "new_slot": new_slot,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def idempotency_key(user_id: str, action: str, fp: str) -> str:
    return f"{user_id}:{action}:{fp[:16]}"


@dataclass(frozen=True)
class Proposal:
    id: str
    action: str
    user_id: str
    provider_id: str | None
    slot: str | None
    price: int | None
    booking_id: str | None
    new_slot: str | None
    fingerprint: str
    text: str

    @classmethod
    def build(cls, id, action, user_id, provider_id=None, slot=None, price=None,
              booking_id=None, new_slot=None, text="") -> "Proposal":
        fp = fingerprint(action, user_id, provider_id, slot, price, booking_id, new_slot)
        return cls(id, action, user_id, provider_id, slot, price, booking_id, new_slot, fp, text)


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    user_id: str
    action: str
    fingerprint: str
    status: str  # active | consumed | voided
    reason: str = ""


class ApprovalStore:
    def __init__(self, storage):
        self.storage = storage

    # -- proposals
    def propose(self, proposal: Proposal) -> None:
        self.storage.kv_set(NS_PENDING, proposal.user_id, asdict(proposal))

    def pending(self, user_id: str) -> Proposal | None:
        d = self.storage.kv_get(NS_PENDING, user_id)
        return Proposal(**d) if d else None

    def void_pending(self, user_id: str, reason: str) -> None:
        """Drop the pending proposal and void any active approvals of the user."""
        self.storage.kv_delete(NS_PENDING, user_id)
        for aid, rec in self.storage.kv_items(NS_APPROVAL).items():
            if rec["user_id"] == user_id and rec["status"] == "active":
                rec["status"] = "voided"
                rec["reason"] = reason
                self.storage.kv_set(NS_APPROVAL, aid, rec)

    # -- the only creator of approvals
    def grant_from_user_message(self, user_id: str, raw_text: str) -> ApprovalRecord | None:
        if not isinstance(raw_text, str) or not AFFIRMATIVE.match(raw_text):
            return None
        prop = self.pending(user_id)
        if prop is None or prop.user_id != user_id:
            return None
        # proposal must be internally consistent (fingerprint unchanged)
        expected = fingerprint(prop.action, prop.user_id, prop.provider_id, prop.slot,
                               prop.price, prop.booking_id, prop.new_slot)
        if expected != prop.fingerprint:
            self.void_pending(user_id, "fingerprint_mismatch")
            return None
        n = len(self.storage.kv_items(NS_APPROVAL)) + 1
        aid = f"AP-{prop.fingerprint[:10]}-{n}"
        rec = ApprovalRecord(aid, user_id, prop.action, prop.fingerprint, "active")
        self.storage.kv_set(NS_APPROVAL, aid, asdict(rec))
        self.storage.kv_delete(NS_PENDING, user_id)  # a repeated "yes" finds nothing pending
        return rec

    def check(self, user_id, action, provider_id, slot, price, booking_id=None,
              new_slot=None) -> ApprovalRecord:
        fp = fingerprint(action, user_id, provider_id, slot, price, booking_id, new_slot)
        matches = [r for r in self.storage.kv_items(NS_APPROVAL).values()
                   if r["user_id"] == user_id and r["fingerprint"] == fp]
        for r in matches:
            if r["status"] == "active":
                return ApprovalRecord(**r)
        if matches:
            raise ApprovalError(f"approval {matches[-1]['status']} for this action")
        raise ApprovalError("no approval matches this exact action, provider, slot and price")

    def consume(self, approval_id: str) -> None:
        rec = self.storage.kv_get(NS_APPROVAL, approval_id)
        if rec is None:
            raise ApprovalError("unknown approval")
        if rec["status"] != "active":
            raise ApprovalError(f"approval already {rec['status']}")
        rec["status"] = "consumed"
        self.storage.kv_set(NS_APPROVAL, approval_id, rec)

    def void(self, approval_id: str, reason: str) -> None:
        rec = self.storage.kv_get(NS_APPROVAL, approval_id)
        if rec and rec["status"] == "active":
            rec["status"] = "voided"
            rec["reason"] = reason
            self.storage.kv_set(NS_APPROVAL, approval_id, rec)
