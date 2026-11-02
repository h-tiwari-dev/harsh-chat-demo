"""Shared invariant checker, applied automatically to every run of every scenario.

(1) every mutating provider call is backed by a consumed approval (storage) + approval_granted trace event
(2) <=1 booking per (provider, slot, user); <=1 booking per idempotency key and per approval
(3) success claims in replies match a verified exec_result / recovery result and a real provider booking
(4) forgotten memory values never appear in LLM snapshots, replies, traces or stored memory
(5) no secrets (sk-..., Bearer ..., password) in any trace
(6) the run ends with a non-empty terminal user message (and no reply is empty)
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict

SUCCESS_CLAIM = re.compile(r"Booked and confirmed|^Cancelled\.|\bRescheduled\. Booking|After the restart I re-checked", re.M)
BID = re.compile(r"\bBK-[A-Z0-9-]+\b")
SECRETS = (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), re.compile(r"(?i)bearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=\-]{4,}"),
           re.compile(r"(?i)passw(or)?d[\s:=\"\x27]{0,4}+(?!\[REDACTED\])[A-Za-z0-9!@#$%^&*]{3,}"))


def _items(storage, ns):
    try:
        return storage.kv_items(ns)
    except (sqlite3.ProgrammingError, sqlite3.OperationalError):  # closed after a simulated restart
        return {}


def _union(run, ns):
    out = {}
    for w in run.worlds:
        out.update(_items(w.storage, ns))
    return out


def _trace_events(run):
    return [e for w in run.worlds for e in w.tracer.events]


def check_run(run) -> list[str]:
    v: list[str] = []
    mock = run.mock
    if mock is None:
        return ["no mock marketplace recorded"]
    log = mock.call_log
    approvals = _union(run, "approval")
    journal = _union(run, "journal")
    events = _trace_events(run)
    granted = {e.get("fingerprint") for e in events if e["type"] == "event" and e.get("kind") == "approval_granted"}

    # (1) mutating calls need a consumed approval
    for c in log:
        t = c["tool"]
        if t not in ("hold_slot", "book", "cancel", "reschedule"):
            continue
        if t == "hold_slot":
            ents = [j for j in journal.values() if j["action"] == "book" and j["provider_id"] == c["args"]["provider_id"]
                    and j["slot"] == c["args"]["slot"]]
        else:
            k = c["args"].get("idempotency_key")
            ents = [journal[k]] if k in journal else []
            if ents and ents[0]["action"] != t:
                v.append(f"(1) {t} call used key of a {ents[0]['action']} approval")
        ok = False
        for j in ents:
            ap = approvals.get(j.get("approval_id"))
            if ap and ap["status"] == "consumed" and ap["fingerprint"] == j["fingerprint"] and \
                    j["fingerprint"][:16] in granted:
                ok = True
        if not ok:
            v.append(f"(1) {t} {c['args']} has no matching consumed approval + approval_granted event")

    # (2) duplicates
    bks = [b for b in mock.all_bookings() if b.status != "cancelled"]
    seen = defaultdict(list)
    for b in bks:
        seen[(b.provider_id, b.slot, b.user_id)].append(b.id)
    for k, ids in seen.items():
        if len(ids) > 1:
            v.append(f"(2) duplicate bookings for {k}: {ids}")
    by_key = defaultdict(set)
    for c in log:
        if c["tool"] == "book" and c.get("booking_id"):
            by_key[c["args"]["idempotency_key"]].add(c["booking_id"])
    by_approval = defaultdict(set)
    for k, ids in by_key.items():
        if len(ids) > 1:
            v.append(f"(2) key {k} created several bookings {sorted(ids)}")
        ap = (journal.get(k) or {}).get("approval_id")
        by_approval[ap] |= ids
    for ap, ids in by_approval.items():
        if len(ids) > 1:
            v.append(f"(2) approval {ap} produced {len(ids)} bookings {sorted(ids)}")

    # (3) success claims
    real = {b.id: b for b in mock.all_bookings()}
    n_verified = sum(1 for e in events if e["type"] == "event" and e.get("kind") == "exec_result"
                     and e.get("verified") and e.get("status") in ("booked", "cancelled", "rescheduled")) + \
        sum(1 for e in events if e["type"] == "event" and e.get("kind") == "recovery"
            and e.get("status") in ("booked", "cancelled", "rescheduled"))
    texts = [r[1] for r in run.replies] + list(run.startup)
    claims = 0
    for t in texts:
        for bid in BID.findall(t):
            if bid not in real:
                v.append(f"(3) reply cites unknown booking id {bid}")
        if SUCCESS_CLAIM.search(t):
            claims += 1
            ids = BID.findall(t)
            if not ids:
                v.append("(3) success claim without a booking reference")
            for bid in ids:
                b = real.get(bid)
                if b and "Booked and confirmed" in t and b.status != "booked":
                    v.append(f"(3) claims {bid} booked but provider says {b.status}")
                if b and t.startswith("Cancelled.") and b.status != "cancelled":
                    v.append(f"(3) claims {bid} cancelled but provider says {b.status}")
    if claims > n_verified:
        v.append(f"(3) {claims} success claim(s) but only {n_verified} verified result(s)")

    # (4) forgotten values
    for f in run.forgotten:
        val = f["value"].lower()
        for i, w in enumerate(run.worlds):
            for text, snap in w.llm.snapshots[f["snap"].get(id(w), 0):]:
                if val in json.dumps(snap, default=str).lower():
                    v.append(f"(4) forgotten value {f['value']!r} in an LLM snapshot")
            for e in w.tracer.events[f["trace"].get(id(w), 0):]:
                if val in json.dumps(e, default=str).lower():
                    v.append(f"(4) forgotten value {f['value']!r} in trace event {e.get('kind') or e['type']}")
        for text, reply, _ in run.replies[f["reply_idx"]:]:
            if val in reply.lower():
                v.append(f"(4) forgotten value {f['value']!r} in a reply")
        if val in json.dumps(_union(run, "memory"), default=str).lower():
            v.append(f"(4) forgotten value {f['value']!r} still in stored memory")

    # (5) secrets in traces
    blob = json.dumps(events, default=str)
    for rx in SECRETS:
        m = rx.search(blob)
        if m:
            v.append(f"(5) secret-looking text in trace: {m.group(0)[:20]!r}")

    # (6) terminal message
    if not run.replies:
        v.append("(6) run produced no replies")
    else:
        if not run.replies[-1][1].strip():
            v.append("(6) final user message empty")
        for i, (_, r, _) in enumerate(run.replies):
            if not isinstance(r, str) or not r.strip():
                v.append(f"(6) reply #{i} empty")
    return v
