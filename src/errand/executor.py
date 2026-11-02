"""Deterministic executor for mutating tools (book / cancel / reschedule)."""
from __future__ import annotations

from dataclasses import dataclass

from .approval import ApprovalError, fingerprint, idempotency_key
from .models import ToolError, ToolTimeout

NS_JOURNAL = "journal"
NS_KEYGEN = "keygen"
PENDING, BOOKED, FAILED, UNKNOWN = "PENDING", "BOOKED", "FAILED", "UNKNOWN"
ACTIONS = ("book", "cancel", "reschedule")
SUCCESS_STATUS = {"book": "booked", "cancel": "cancelled", "reschedule": "rescheduled"}


@dataclass
class ExecResult:
    status: str  # booked|cancelled|rescheduled|needs_reapproval|slot_taken|slot_conflict|failed|already_done|unverified
    booking_id: str | None = None
    message: str = ""
    verified: bool = False
    new_price: int | None = None
    idempotency_key: str | None = None


def _is_limit(e: Exception) -> bool:
    return type(e).__name__ == "LimitExceeded"


class BookingExecutor:
    def __init__(self, api, approvals, storage, tracer, budget, clock, max_retries=3):
        self.api, self.approvals, self.storage = api, approvals, storage
        self.tracer, self.budget, self.clock = tracer, budget, clock
        self.max_retries = max_retries

    # ---------------------------------------------------------------- plumbing
    def _call(self, tool, fn, **args):
        if self.budget is not None:
            self.budget.count_tool_call()
        t0 = self.clock.now()
        try:
            result = fn(*args.values())
        except Exception as e:
            self._trace(tool, args, None, f"{type(e).__name__}: {e}", t0)
            raise
        self._trace(tool, args, result, None, t0)
        return result

    def _trace(self, tool, args, result, error, t0):
        if self.tracer is None:
            return
        ms = int((self.clock.now() - t0).total_seconds() * 1000)
        self.tracer.tool_call(tool, args, result=result, error=error, latency_ms=ms)

    def _event(self, kind, **f):
        if self.tracer is not None:
            self.tracer.event(kind, **f)

    def _backoff(self, i):
        self.clock.sleep(min(0.5 * (2 ** i), 8.0))

    def current_key(self, user_id, action, fp) -> str:
        """Idempotency key for this fingerprint. Once a booking made under a key has been cancelled or
        rescheduled away, the key is retired (generation bump) so re-booking the same provider/slot/price
        is a genuinely new request instead of replaying the old, no-longer-live booking."""
        base = idempotency_key(user_id, action, fp)
        gen = self.storage.kv_get(NS_KEYGEN, base, 0)
        return base if not gen else f"{base}:g{gen}"

    def _retire_booking_keys(self, user_id, booking_id):
        for e in self.journal_entries(user_id):
            if e["action"] == "book" and e["state"] == BOOKED and e.get("result_booking_id") == booking_id:
                base = idempotency_key(user_id, "book", e["fingerprint"])
                self.storage.kv_set(NS_KEYGEN, base, self.storage.kv_get(NS_KEYGEN, base, 0) + 1)
                e["state"] = "SUPERSEDED"
                self.storage.kv_set(NS_JOURNAL, e["key"], e)

    def journal_get(self, key):
        return self.storage.kv_get(NS_JOURNAL, key)

    def journal_entries(self, user_id):
        return [e for e in self.storage.kv_items(NS_JOURNAL).values() if e["user_id"] == user_id]

    def _jset(self, entry, state, **upd):
        entry["state"] = state
        entry.update(upd)
        entry["updated_at"] = self.clock.now().isoformat()
        self.storage.kv_set(NS_JOURNAL, entry["key"], entry)

    def _finish(self, entry, state, status, message, booking_id=None, verified=False, **kw):
        self._jset(entry, state, result_status=status, result_booking_id=booking_id, detail=message)
        return ExecResult(status, booking_id, message, verified, idempotency_key=entry["key"], **kw)

    # ---------------------------------------------------------------- execute
    def execute(self, user_id, action, provider_id=None, slot=None, price=None,
                booking_id=None, new_slot=None) -> ExecResult:
        if action not in ACTIONS:
            return ExecResult("failed", message=f"unsupported action {action!r}")
        fp = fingerprint(action, user_id, provider_id, slot, price, booking_id, new_slot)
        key = self.current_key(user_id, action, fp)

        existing = self.journal_get(key)
        if existing and existing["state"] == BOOKED:
            return ExecResult("already_done", existing.get("result_booking_id") or existing.get("booking_id"),
                              "This was already completed; no second action taken.", True,
                              idempotency_key=key)

        try:
            rec = self.approvals.check(user_id, action, provider_id, slot, price, booking_id, new_slot)
        except ApprovalError as e:
            self._event("approval_denied", action=action, reason=str(e))
            return ExecResult("failed", message=f"Not approved: {e}", idempotency_key=key)

        entry = {"key": key, "user_id": user_id, "action": action, "fingerprint": fp,
                 "provider_id": provider_id, "slot": slot, "price": price,
                 "booking_id": booking_id, "new_slot": new_slot, "approval_id": rec.id}
        # journal intent BEFORE any tool call, then consume (single use)
        self._jset(entry, PENDING)
        try:
            self.approvals.consume(rec.id)
        except ApprovalError as e:
            return self._finish(entry, FAILED, "failed", f"Not approved: {e}")
        self._event("approval_consumed", approval_id=rec.id, key=key)

        try:
            if action == "book":
                return self._book(entry, rec)
            return self._mutate(entry)
        except Exception as e:  # never raise to caller, except budget exhaustion
            self._jset(entry, UNKNOWN, detail=f"{type(e).__name__}: {e}")
            if _is_limit(e):
                raise
            return ExecResult("failed", message=f"Unexpected error ({type(e).__name__}): {e}. "
                              "Outcome unknown; will be reconciled before any retry.",
                              idempotency_key=key)

    # ---------------------------------------------------------------- book
    def _hold(self, entry):
        h = self._call("hold_slot", self.api.hold_slot, provider_id=entry["provider_id"], slot=entry["slot"])
        return h

    def _price_drift(self, entry, rec, hold):
        q = hold["quoted_price"]
        if q == entry["price"]:
            return None
        self._event("approval_voided", reason="price_changed", approved=entry["price"], quoted=q)
        self.approvals.void_pending(entry["user_id"], "price_changed")
        return self._finish(entry, FAILED, "needs_reapproval",
                            f"Price changed from {entry['price']} to {q}; please re-confirm.",
                            new_price=q)

    def _book(self, entry, rec):
        try:
            hold = self._hold(entry)
        except ToolError as e:
            if e.code == "slot_unavailable":
                return self._finish(entry, FAILED, "slot_taken", "That slot is no longer available.")
            return self._finish(entry, FAILED, "failed", f"Could not hold slot: {e.code}")
        drift = self._price_drift(entry, rec, hold)
        if drift:
            return drift

        key = entry["key"]
        tries = 0
        rehold_used = False
        while True:
            try:
                r = self._call("book", self.api.book, hold_id=hold["hold_id"], idempotency_key=key)
                return self._finish(entry, BOOKED, "booked", "Booked.", r["booking_id"], True)
            except ToolTimeout:
                st = self._status(key)
                if st and st.get("status") == "booked":
                    return self._finish(entry, BOOKED, "booked",
                                        "Booked (verified via status check after a timeout).",
                                        st.get("booking_id"), True)
                if st and st.get("status") == "failed":
                    return self._finish(entry, FAILED, "failed", "Booking failed at the provider.")
                tries += 1
                if tries > self.max_retries:
                    return self._unverified(entry)
                self._backoff(tries - 1)
            except ToolError as e:
                if e.code == "slot_taken":
                    return self._finish(entry, FAILED, "slot_taken", "That slot was just taken by someone else.")
                if e.code == "hold_expired":
                    if rehold_used:
                        return self._finish(entry, FAILED, "failed", "Hold expired again; please retry.")
                    rehold_used = True
                    try:
                        hold = self._hold(entry)
                    except ToolError as e2:
                        st = "slot_taken" if e2.code == "slot_unavailable" else "failed"
                        return self._finish(entry, FAILED, st, f"Could not re-hold slot: {e2.code}")
                    drift = self._price_drift(entry, rec, hold)
                    if drift:
                        return drift
                    continue
                if e.transient:
                    tries += 1
                    if tries > self.max_retries:
                        return self._unverified(entry)
                    self._backoff(tries - 1)
                    continue
                return self._finish(entry, FAILED, "failed", f"Booking failed: {e.code}")

    def _status(self, key):
        try:
            return self._call("get_booking_status", self.api.get_booking_status, idempotency_key=key)
        except Exception as e:
            if _is_limit(e):
                raise
            return None

    def _unverified(self, entry):
        self._jset(entry, UNKNOWN)
        return ExecResult("unverified", message="I could not confirm whether the action went through. "
                          "I will not assume success; it will be re-checked.",
                          idempotency_key=entry["key"])

    # ---------------------------------------------------------------- cancel / reschedule
    def _mutate(self, entry):
        action, key = entry["action"], entry["key"]
        tries = 0
        while True:
            try:
                if action == "cancel":
                    r = self._call("cancel", self.api.cancel, booking_id=entry["booking_id"], idempotency_key=key)
                    res = self._finish(entry, BOOKED, "cancelled", "Cancelled.", entry["booking_id"], True)
                    self._retire_booking_keys(entry["user_id"], entry["booking_id"])
                    return res
                r = self._call("reschedule", self.api.reschedule, booking_id=entry["booking_id"],
                               new_slot=entry["new_slot"], idempotency_key=key)
                res = self._finish(entry, BOOKED, "rescheduled", f"Rescheduled to {r.get('slot', entry['new_slot'])}.",
                                   r.get("booking_id", entry["booking_id"]), True)
                self._retire_booking_keys(entry["user_id"], entry["booking_id"])
                return res
            except ToolError as e:
                if e.code == "slot_conflict":
                    return self._finish(entry, FAILED, "slot_conflict",
                                        "That new time conflicts; your booking is unchanged.", entry["booking_id"])
                if e.code == "not_found":
                    return self._finish(entry, FAILED, "failed", "Booking not found.")
                if e.transient or isinstance(e, ToolTimeout):
                    tries += 1
                    if tries > self.max_retries:
                        return self._unverified(entry)
                    self._backoff(tries - 1)
                    continue
                return self._finish(entry, FAILED, "failed", f"{action} failed: {e.code}")

    # ---------------------------------------------------------------- recovery
    def recover(self, user_id) -> list[ExecResult]:
        out = []
        for entry in self.journal_entries(user_id):
            if entry["state"] not in (PENDING, UNKNOWN):
                continue
            try:
                out.append(self._reconcile(entry))
            except Exception as e:
                if _is_limit(e):
                    raise
                out.append(ExecResult("failed", message=f"Recovery error: {e}", idempotency_key=entry["key"]))
        for r in out:
            self._event("recovery", key=r.idempotency_key, status=r.status)
        return out

    def _reconcile(self, entry):
        key, action = entry["key"], entry["action"]
        if action == "book":
            st = self._status(key)
            if st is None:
                return self._unverified(entry)
            if st["status"] == "booked":
                return self._finish(entry, BOOKED, "booked", "Recovered: booking confirmed, not rebooked.",
                                    st.get("booking_id"), True)
            if st["status"] == "failed":
                return self._finish(entry, FAILED, "failed", "Recovered: booking had failed.")
            return self._finish(entry, FAILED, "failed",
                                "Recovered: no booking was made; please confirm again to retry.")
        # cancel / reschedule: no status endpoint, verify via bookings list
        try:
            bks = self._call("list_bookings", self.api.list_bookings, user_id=entry["user_id"])
        except Exception as e:
            if _is_limit(e):
                raise
            return self._unverified(entry)
        b = next((x for x in bks if x.id == entry["booking_id"]), None)
        if b is not None and action == "cancel" and b.status == "cancelled":
            return self._finish(entry, BOOKED, "cancelled", "Recovered: cancellation confirmed.", b.id, True)
        if b is not None and action == "reschedule" and b.slot == entry["new_slot"]:
            return self._finish(entry, BOOKED, "rescheduled", "Recovered: reschedule confirmed.", b.id, True)
        if b is not None and ((action == "cancel" and b.status == "booked")
                              or (action == "reschedule" and b.status == "booked" and b.slot == entry["slot"])):
            # the provider's own list shows the booking untouched: the change was never applied.
            # (Re-proposing reuses the same idempotency key, so a late-landing request cannot double-apply.)
            return self._finish(entry, FAILED, "failed",
                                f"Recovered: the {action} was not applied; your booking is unchanged. "
                                "Please confirm again to retry.")
        return self._unverified(entry)
