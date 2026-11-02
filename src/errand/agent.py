"""Deterministic orchestrator. The LLM interprets text; this code decides, calls tools and executes.

Safety rules enforced here (not by the model):
* approvals are created only by ApprovalStore.grant_from_user_message on the user's RAW message,
  BEFORE any LLM interpretation;
* only whitelisted intents / slot keys from the LLM are honoured;
* mutating calls happen only inside BookingExecutor.execute (needs a matching active approval);
* tool text (reviews, names) never reaches the LLM and is shown only as quoted, flagged data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import sanitize
from .approval import AFFIRMATIVE, ApprovalStore, Proposal, idempotency_key
from .executor import BookingExecutor, ExecResult
from .limits import Budget, LimitExceeded
from .llm import ALLOWED_INTENTS, LLM, Interpretation
from .memory import MemoryStore, parse_memory_command
from .models import ToolError
from .state import KNOWN_PREFERENCES, Phase, Slots, WorkflowState
from .trace import Tracer

NEGATIVE = re.compile(r"^\s*(no|nope|nah|cancel|never ?mind|stop|don'?t|no thanks?)\s*[.!]*\s*$", re.I)
YESNO_NO = re.compile(r"^\s*(no|nope|nah|no thanks?)\s*[.!]*\s*$", re.I)
SERVICE_LABEL = {"ac_servicing": "AC servicing"}
MAX_OPTIONS = 3
SUCCESS = {"booked", "cancelled", "rescheduled"}
TERMINAL_MSG = {
    "turns": "I've reached the maximum number of messages for this session",
    "model_calls": "I've used up my allowed number of language-model calls for this session",
    "tool_calls": "I've used up my allowed number of provider lookups for this session",
    "wall_clock": "This session has run longer than the time limit I'm allowed",
}


@dataclass
class AgentReply:
    text: str
    phase: Phase
    trace_path: str | None = None
    data: dict = field(default_factory=dict)  # e.g. {"proposal": {...}, "booking_id": "BK-1"}


def label(service: str | None) -> str:
    return SERVICE_LABEL.get(service or "", (service or "service").replace("_", " "))


def pretty_slot(slot: str | None) -> str:
    try:
        return datetime.fromisoformat(slot).strftime("%a %d %b, %H:%M")
    except Exception:
        return str(slot)


def safe_text(s, max_len: int = 60) -> str:
    """Tool-provided names: strip control chars; hide if it looks like an injection."""
    u = sanitize.wrap(s, "provider name")
    if any(f in u.flags for f in ("ignore_previous", "system_marker", "role_marker", "role_override",
                                  "approve", "book_without_asking", "cancel_all", "url")):
        return "(name hidden)"
    return sanitize.render_for_user(u, max_len).split('" [')[0].strip('"')


class Agent:
    def __init__(self, user_id, api, llm, storage, clock, tracer=None, budget_cfg=None,
                 memory=None, approvals=None, phrase_questions=False):
        self.user_id = user_id
        self.api, self.llm, self.storage, self.clock = api, llm, storage, clock
        self.tracer = tracer or Tracer(None, clock)
        self.budget = Budget(clock, **(budget_cfg or {}))
        self.approvals = approvals or ApprovalStore(storage)
        self.memory = memory or MemoryStore(storage, clock)
        self.executor = BookingExecutor(api, self.approvals, storage, self.tracer, self.budget, clock)
        self.phrase_questions = phrase_questions
        self.state = WorkflowState.load(storage, user_id) or WorkflowState(user_id)
        self._notes: list[str] = []
        self.startup_message: str | None = None
        self.recovery_results: list[ExecResult] = []

    # ================================================================== public API
    def handle(self, user_text: str) -> AgentReply:
        text = (user_text if isinstance(user_text, str) else str(user_text)).strip()[:2000]
        data: dict = {}
        self._notes = []
        if self.state.phase == Phase.LIMIT_EXHAUSTED:
            return self._reply(self._limit_text(self.state.limit_kind), data)
        try:
            self.budget.start_turn()
            msg = self._turn(text, data)
        except LimitExceeded as e:
            msg = self._on_limit(e)
        except Exception as e:  # handle() never raises (BaseException = simulated crash passes through)
            msg = self._on_failure(e)
        if self._notes and self.state.phase not in (Phase.LIMIT_EXHAUSTED, Phase.FAILED):
            msg = " ".join(self._notes) + "\n" + msg
        self._save()
        return self._reply(msg, data)

    @classmethod
    def resume(cls, user_id, api, llm, storage, clock, tracer=None, budget_cfg=None, memory=None,
               approvals=None, phrase_questions=False) -> "Agent":
        """Rebuild an agent from durable storage after a restart and reconcile in-flight actions."""
        a = cls(user_id, api, llm, storage, clock, tracer, budget_cfg, memory, approvals, phrase_questions)
        a.tracer.event("resume", phase=a.state.phase.name)
        st = a.state
        if st.phase in (Phase.LIMIT_EXHAUSTED, Phase.FAILED):
            a._set_phase(Phase.COLLECTING, "resume after terminal state")  # fresh budget in new process
        lines: list[str] = []
        try:
            a.budget.start_turn()
            lines += a._recover_startup()
        except LimitExceeded as e:
            lines.append(a._on_limit(e))
        except Exception as e:
            lines.append(a._on_failure(e))
        pend = a.approvals.pending(user_id)
        if pend is not None and st.phase == Phase.AWAITING_APPROVAL:
            lines.append("Welcome back. Still waiting on your confirmation: " + pend.text)
        a._save()
        a.startup_message = "\n".join(l for l in lines if l) or None
        return a

    # ================================================================== proactive follow-up
    def check_followups(self) -> AgentReply | None:
        """Detect bookings the PROVIDER cancelled (status 'provider_cancelled' via list_bookings), once per
        booking (journaled in storage ns 'followup'). Tells the user, searches for a replacement and presents
        options. Rebooking still goes through the normal choose -> Proposal -> user 'yes' -> executor path;
        nothing is booked here. Returns None when there is nothing new to report."""
        st = self.state
        if st.phase in (Phase.LIMIT_EXHAUSTED, Phase.FAILED, Phase.AWAITING_APPROVAL, Phase.EXECUTING,
                        Phase.RECOVERING):
            return None  # never disturb an in-flight approval/execution; checked again next time
        data: dict = {}
        try:
            bks = self._tool("list_bookings", self.api.list_bookings, user_id=self.user_id)
            todo = [b for b in bks if b.status == "provider_cancelled"
                    and (self.storage.kv_get("followup", b.id) or {}).get("state") != "done"]
            if not todo:
                return None
            lines = []
            for b in todo:
                self.storage.kv_set("followup", b.id, {"state": "processing"})
                lines.append(self._followup_one(b, data))
                self.storage.kv_set("followup", b.id, {"state": "done", "options": len(self.state.options)})
        except LimitExceeded as e:
            return self._reply(self._on_limit(e), data)
        except Exception as e:
            return self._reply(self._on_failure(e), data)
        self._save()
        return self._reply("\n".join(lines), data)

    def _followup_one(self, b, data: dict) -> str:
        st = self.state
        name = safe_text(self._provider_name(b.provider_id))
        self._event("provider_cancelled_detected", booking_id=b.id, provider_id=b.provider_id)
        head = (f"Heads up: {name} cancelled your {label(b.service)} booking {b.id} "
                f"({pretty_slot(b.slot)}). That was the provider's decision, not yours. ")
        old = st.slots  # keep the user's saved constraints (budget, time window, preferences)
        area = (self._booking_area(b.id) or old.area or self.memory.usable(self.user_id).get("area") or "")
        if not area:
            try:
                area = self._tool("get_provider", self.api.get_provider, provider_id=b.provider_id)["provider"].area
            except ToolError:
                area = ""
        earliest = old.time_window.get("earliest") or self.memory.usable(self.user_id).get("earliest_time")
        keep = {"budget": old.budget, "preferences": list(old.preferences),
                "time_window": {"earliest": earliest, "latest": old.time_window.get("latest")}}
        today = self._today()
        last = min(date.fromisoformat(b.slot[:10]) + timedelta(days=1), today + timedelta(days=13))
        self._reset("book")
        sl = st.slots
        sl.service, sl.area, sl.date_range = b.service, area, [today.isoformat(), max(today, last).isoformat()]
        sl.budget, sl.preferences, sl.time_window = keep["budget"], keep["preferences"], keep["time_window"]
        try:
            res = self._tool_retry("search_providers", self.api.search_providers, service=b.service, area=area,
                                   date_range=tuple(sl.date_range))
        except ToolError as e:
            self._set_phase(Phase.COLLECTING, "followup search failed")
            return head + f"I couldn't search for a replacement right now ({e.code}). Nothing has been booked."
        st.candidates = [self._prov(p) for p in res if p.id != b.provider_id]
        st.search_key = sl.search_key()
        orig_limit = (datetime.fromisoformat(b.slot) + timedelta(days=1)).isoformat(timespec="minutes")
        for c in st.candidates:  # similar or earlier slot only
            c["free_slots"] = [s for s in c["free_slots"] if s <= orig_limit]
        opts, stages, cheap = self._book_options()
        if not opts:
            st.options = []
            self._set_phase(Phase.COLLECTING, "followup: no replacement")
            return (head + f"I looked for a replacement in {area or 'your area'} around the same time and found "
                    "nothing that fits your saved preferences and budget. Nothing has been booked. "
                    "Want me to widen the dates, time window or budget?")
        self._fetch_reviews(opts)
        st.options = opts
        self._set_phase(Phase.PRESENTING, "followup options")
        data["followup"] = {"booking_id": b.id, "options": len(opts)}
        return (head + "I searched for a replacement around the same time. "
                + self._options_text().replace("Here's what I found:", "Options:", 1)
                + " I won't book anything unless you pick one and confirm.")

    # ================================================================== plumbing
    def _reply(self, msg: str, data: dict) -> AgentReply:
        return AgentReply(msg, self.state.phase, getattr(self.tracer, "path", None), data)

    def _save(self):
        self.state.save(self.storage)

    def _set_phase(self, to: Phase, reason: str = ""):
        if to != self.state.phase:
            self.tracer.state_transition(self.state.phase, to, reason)
            self.state.phase = to
        self._save()

    def _event(self, kind, **f):
        self.tracer.event(kind, **f)

    def _tool(self, name, fn, **args):
        self.budget.count_tool_call()
        t0 = self.clock.now()
        try:
            r = fn(**args)
        except Exception as e:
            self.tracer.tool_call(name, args, error=f"{type(e).__name__}: {e}",
                                  latency_ms=int((self.clock.now() - t0).total_seconds() * 1000))
            raise
        self.tracer.tool_call(name, args, result=r, latency_ms=int((self.clock.now() - t0).total_seconds() * 1000))
        return r

    def _tool_retry(self, name, fn, **args):
        for i in range(3):
            try:
                return self._tool(name, fn, **args)
            except ToolError as e:
                if e.transient and i < 2:
                    self.clock.sleep(0.5 * 2 ** i)
                    continue
                raise

    def _reset(self, flow: str | None):
        st = self.state
        if self.approvals.pending(self.user_id) is not None:
            self.approvals.void_pending(self.user_id, "workflow_reset")
        st.flow, st.slots = flow, Slots()
        st.candidates, st.search_key, st.options, st.selected = [], None, [], None
        st.target_booking, st.excluded, st.rebook = None, [], False
        st.pending_proposal_id, st.in_flight, st.last_result = None, None, None
        st.pending_memory_key = None
        st.review_notes, st.review_flags = {}, []

    def _today(self) -> date:
        return self.clock.now().date()

    # ================================================================== failure / limits
    def _open_journal(self) -> list:
        try:
            return [e for e in self.executor.journal_entries(self.user_id) if e["state"] in ("PENDING", "UNKNOWN")]
        except Exception:
            return []

    def _on_limit(self, e: LimitExceeded) -> str:
        try:
            self.state.limit_kind = e.kind
            self._event("limit_exceeded", limit=e.kind, budget=self.budget.snapshot())
            self.approvals.void_pending(self.user_id, f"limit_{e.kind}")
            self._set_phase(Phase.LIMIT_EXHAUSTED, f"limit {e.kind}")
        except Exception:
            self.state.phase = Phase.LIMIT_EXHAUSTED
        return self._limit_text(e.kind)

    def _limit_text(self, kind: str | None) -> str:
        kind = kind or "turns"
        txt = TERMINAL_MSG.get(kind, "I've hit a safety limit") + ", so I have stopped working on this request."
        if self._open_journal():
            txt += (" One earlier action may not have finished; I have not assumed it succeeded and will "
                    "re-check it with the provider when you come back.")
        else:
            txt += " Nothing further was booked, cancelled or changed. Please start a new session to continue."
        return txt

    def _on_failure(self, e: Exception) -> str:
        try:
            self._event("agent_error", error=f"{type(e).__name__}: {e}")
            self.approvals.void_pending(self.user_id, "error")
            self._set_phase(Phase.FAILED, f"{type(e).__name__}")
        except Exception:
            self.state.phase = Phase.FAILED
        txt = f"Sorry, something went wrong on my side ({type(e).__name__}). I stopped before changing anything further."
        if self._open_journal():
            txt += " An earlier action may be unfinished; I will not assume it succeeded and will re-check it."
        return txt + " You can send a new message to try again."

    # ================================================================== the turn
    def _turn(self, text: str, data: dict) -> str:
        st = self.state
        if st.phase == Phase.FAILED:
            self._set_phase(Phase.COLLECTING, "retry after failure")
        if st.phase == Phase.RECOVERING:
            return self._recover_now(data)

        # 1. approval: ONLY from the user's raw message, BEFORE any LLM interpretation
        prop = self.approvals.pending(self.user_id)
        if prop is not None:
            rec = self.approvals.grant_from_user_message(self.user_id, text)
            if rec is not None and rec.fingerprint == prop.fingerprint:
                self._event("approval_granted", approval_id=rec.id, action=prop.action, fingerprint=prop.fingerprint[:16])
                return self._execute(prop, data)
        # 2. deterministic yes/no answers
        if st.pending_memory_key and re.match(r"^\s*(yes|y|yep|yeah|ok|okay|no|nope|nah)\s*[.!]*\s*$", text, re.I):
            return self._answer_memory_question(text, data)
        if AFFIRMATIVE.match(text):
            if st.phase == Phase.DONE and st.last_result:
                self._event("repeat_confirmation", booking_id=st.last_result.get("booking_id"))
                return (f"That is already done: {st.last_result.get('summary', '')} "
                        f"(reference {st.last_result.get('booking_id')}). I did not do anything again.")
            return "There is nothing waiting for your confirmation right now."
        # 3. memory commands, deterministic
        cmd = parse_memory_command(text)
        if cmd:
            return self._memory_command(cmd)
        if st.phase == Phase.AWAITING_APPROVAL and NEGATIVE.match(text):
            self.approvals.void_pending(self.user_id, "user_declined")
            self._set_phase(Phase.PRESENTING if st.options and st.flow != "cancel" else Phase.COLLECTING, "declined")
            st.pending_proposal_id = None
            return "Okay, I won't do that. Nothing was booked, cancelled or changed. Tell me what you'd like instead."
        if st.pending_memory_key:
            st.pending_memory_key = None  # question ignored; user said something else

        it = self._interpret(text)
        return self._route(it, text, data)

    # ------------------------------------------------------------------ interpretation
    def _snapshot(self) -> dict:
        st = self.state
        return {"phase": st.phase.name, "flow": st.flow, "slots": st.slots.public(),
                "missing": st.slots.missing(st.flow or "book"), "n_options": len(st.options),
                "awaiting_confirmation": st.phase == Phase.AWAITING_APPROVAL,
                "today": self._today().isoformat(), "review_flags": sorted(st.review_flags)}

    def _interpret(self, text: str) -> Interpretation:
        self.budget.count_model_call()
        snap = self._snapshot()
        t0 = self.clock.now()
        try:
            raw = self.llm.interpret(text, snap)
            it = Interpretation.coerce(raw)
        except LimitExceeded:
            raise
        except Exception as e:
            self._event("llm_error", error=f"{type(e).__name__}: {e}")
            it = Interpretation("unknown")
        ms = int((self.clock.now() - t0).total_seconds() * 1000)
        self.tracer.model_call(type(self.llm).__name__, f"phase={snap['phase']} chars={len(text)}",
                               f"intent={it.intent} keys={sorted(it.slots_update)}", ms)
        if it.intent not in ALLOWED_INTENTS:
            self._event("llm_intent_rejected", intent=str(it.intent)[:40],
                        note="not a permitted intent; ignored by code")
            it = Interpretation("unknown")
        it.slots_update = self._clean_update(it.slots_update)
        return it

    def _clean_update(self, upd) -> dict:
        out: dict = {}
        if not isinstance(upd, dict):
            return out
        rejected = []
        for k, v in upd.items():
            try:
                if k == "service" and isinstance(v, str) and re.fullmatch(r"[a-z_ ]{2,30}", v.strip().lower()):
                    out[k] = v.strip().lower().replace(" ", "_")
                elif k == "area" and isinstance(v, str) and 1 < len(v.strip()) <= 40 and not sanitize.scan(v):
                    out[k] = v.strip()
                elif k == "date_range" and isinstance(v, (list, tuple)) and len(v) == 2:
                    a, b = date.fromisoformat(str(v[0])[:10]), date.fromisoformat(str(v[1])[:10])
                    out[k] = [a.isoformat(), max(a, b).isoformat()]
                elif k in ("earliest", "latest") and isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                    out[k] = v
                elif k == "budget" and isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 1_000_000:
                    out[k] = v
                elif k == "preferences" and isinstance(v, (list, tuple)):
                    out[k] = [p for p in v if p in KNOWN_PREFERENCES]
                elif k == "any_provider" and v is True:
                    out[k] = True
                else:
                    rejected.append(k)
            except (ValueError, TypeError):
                rejected.append(k)
        if rejected:
            self._event("llm_slot_keys_rejected", keys=sorted(map(str, rejected)))
        return out

    # ------------------------------------------------------------------ routing
    def _route(self, it: Interpretation, text: str, data: dict) -> str:
        st = self.state
        intent = it.intent
        if intent == "abort":
            if st.phase == Phase.AWAITING_APPROVAL:
                self.approvals.void_pending(self.user_id, "user_declined")
            self._reset(None)
            self._set_phase(Phase.COLLECTING, "abort")
            return "Okay, I've dropped that. Nothing was booked, cancelled or changed."
        if intent in ("new_request", "rebook_same", "cancel_booking", "reschedule_booking"):
            flow = {"cancel_booking": "cancel", "reschedule_booking": "reschedule"}.get(intent, "book")
            self._reset(flow)
            st.rebook = intent == "rebook_same"
            st.slots.merge(it.slots_update)
            if flow in ("cancel", "reschedule"):
                st.target_booking = {"hint": it.target if isinstance(it.target, dict) else {}}
                tg = st.target_booking["hint"]
                if flow == "reschedule" and tg.get("service") and not st.slots.service:
                    st.slots.service = str(tg["service"])[:30]
            return self._advance(data)
        if intent == "update_slots":
            if st.flow is None or st.phase == Phase.DONE:
                self._reset("book")
                st.slots.merge(it.slots_update)
                return self._advance(data)
            if not it.slots_update:
                return self._reshow(data)
            if st.phase == Phase.AWAITING_APPROVAL:
                self.approvals.void_pending(self.user_id, "user_changed_request")
                self._event("proposal_voided", reason="user_changed_request")
                st.pending_proposal_id = None
            st.slots.merge(it.slots_update)
            st.selected = None
            return self._advance(data)
        if intent == "choose_option":
            return self._choose(it.option_index, data)
        # unknown / chitchat (and any rejected intent): change nothing, remind the user where we are
        return self._reshow(data, unknown=True)

    def _reshow(self, data: dict, unknown: bool = False) -> str:
        st = self.state
        pend = self.approvals.pending(self.user_id)
        if pend is not None and st.phase == Phase.AWAITING_APPROVAL:
            data["proposal"] = {"id": pend.id, "action": pend.action}
            return "I haven't changed anything. " + pend.text
        if st.phase == Phase.PRESENTING and st.options:
            return "Nothing has been booked yet. " + self._options_text()
        if st.phase == Phase.COLLECTING and st.flow and st.slots.missing(st.flow):
            return self._ask_missing()
        return ("I can find and book a service (e.g. 'Find a plumber for my kitchen tap this week, mornings "
                "only, under ₹1,500'), or cancel / reschedule an existing booking. What would you like to do?")

    # ================================================================== memory
    def _memory_command(self, cmd: dict) -> str:
        op, key, val = cmd["op"], cmd["key"], cmd["value"]
        uid = self.user_id
        self._event("memory_command", op=op, key=key)  # never traces the value of forgotten facts
        if op == "view":
            facts = self.memory.view(uid)
            if not facts:
                return "I don't have anything saved about you."
            return "Here is what I remember:\n" + "\n".join(
                f"- {f.key.replace('_', ' ')}: {f.value} ({f.provenance})" for f in facts)
        if op == "forget_all":
            n = self.memory.forget(uid)
            self._scrub_booking_areas()
            self._drop_prefilled(None)
            return f"Done. I've forgotten everything I had saved about you ({n} item{'s' if n != 1 else ''})."
        if op == "forget":
            n = self.memory.forget(uid, key)
            if key == "area":
                self._scrub_booking_areas()
            self._drop_prefilled(key)
            return (f"Done. I've forgotten your {key.replace('_', ' ')}." if n else
                    f"I didn't have a {key.replace('_', ' ')} saved.")
        if op in ("set", "correct"):
            f = (self.memory.correct if op == "correct" else self.memory.set_user_fact)(uid, key, val)
            self._drop_prefilled(key)
            extra = " Your pending confirmation is unchanged." if self.state.phase == Phase.AWAITING_APPROVAL else ""
            return f"Saved: your {key.replace('_', ' ')} is {f.value}.{extra}"
        return "Sorry, I couldn't do that memory command."

    def _scrub_booking_areas(self):
        """A forgotten area must not survive in booking history, where reschedule would silently reuse it."""
        for bid, m in self.storage.kv_items("agent_bookings").items():
            if m.get("area") is not None:
                m["area"] = None
                self.storage.kv_set("agent_bookings", bid, m)

    def _drop_prefilled(self, key):
        """A forgotten/changed fact must not linger in the workflow slots we show an LLM."""
        sl = self.state.slots
        slot_for = {"area": "area", "earliest_time": "earliest"}
        for mk, sk in slot_for.items():
            if key in (None, mk) and mk in sl.prefilled:
                if sk == "area":
                    sl.area = None
                else:
                    sl.time_window["earliest"] = None
                sl.prefilled.pop(mk, None)
                st = self.state
                # search results / key / selection were derived from the dropped value: don't persist them
                st.candidates, st.search_key, st.options = [], None, []
                st.review_notes, st.selected = {}, None
                if st.phase in (Phase.PRESENTING, Phase.AWAITING_APPROVAL):
                    if st.phase == Phase.AWAITING_APPROVAL:
                        self.approvals.void_pending(self.user_id, "memory_changed")
                    self._set_phase(Phase.COLLECTING, "memory changed")
                    st.options = []
        self._save()

    def _prefill_from_memory(self) -> str | None:
        """Pre-fill slots from usable() facts; ask first about inferred, unconfirmed ones."""
        st, sl = self.state, self.state.slots
        usable = self.memory.usable(self.user_id)
        if not sl.area and usable.get("area"):
            sl.area = usable["area"]; sl.prefilled["area"] = usable["area"]
            self._notes.append(f"(Using your saved area: {usable['area']}.)")
        if not sl.time_window.get("earliest") and usable.get("earliest_time") and st.flow != "cancel":
            sl.time_window["earliest"] = usable["earliest_time"]; sl.prefilled["earliest_time"] = usable["earliest_time"]
            self._notes.append(f"(Using your saved earliest time: {usable['earliest_time']}.)")
        for f in self.memory.needs_confirmation(self.user_id):
            if f.key == "area" and not sl.area or f.key == "earliest_time" and not sl.time_window.get("earliest"):
                st.pending_memory_key = f.key
                self._set_phase(Phase.COLLECTING, "confirm inferred fact")
                return (f"I think your {f.key.replace('_', ' ')} is {f.value} (inferred from your past activity, "
                        f"not confirmed). Use it? (yes/no)")
        return None

    def _answer_memory_question(self, text: str, data: dict) -> str:
        st = self.state
        key = st.pending_memory_key
        st.pending_memory_key = None
        if YESNO_NO.match(text):
            self.memory.forget(self.user_id, key)
            self._event("memory_inference_rejected", key=key)
            ans = f"Okay, I won't use that. What's your {key.replace('_', ' ')}?" if key == "area" else "Okay, I won't use that."
            return ans
        self.memory.confirm(self.user_id, key)
        self._event("memory_confirmed", key=key)
        return self._advance(data)

    # ================================================================== advance (collect -> search -> present)
    def _ask_missing(self) -> str:
        st = self.state
        miss = st.slots.missing(st.flow or "book")
        prompts = {"service": "what service you need (plumber, electrician, AC servicing...)",
                   "area": "your area", "date_range": "which day(s) work (e.g. 'this week' or 'Saturday')"}
        parts = [prompts[m] for m in miss]
        q = "To go ahead I just need " + " and ".join(parts) + "."
        if self.phrase_questions and hasattr(self.llm, "phrase"):
            try:
                self.budget.count_model_call()
                p = self.llm.phrase({"ask_for": miss})
                if isinstance(p, str) and p.strip() and not sanitize.scan(p) and len(p) < 300:
                    q = p.strip()
            except LimitExceeded:
                raise
            except Exception:
                pass
        return q

    def _advance(self, data: dict) -> str:
        st = self.state
        if st.flow == "cancel":
            return self._advance_cancel(data)
        if st.flow == "book":
            q = self._prefill_from_memory()
            if q:
                return q
        miss = st.slots.missing(st.flow or "book")
        if miss:
            self._set_phase(Phase.COLLECTING, f"missing {','.join(miss)}")
            return self._ask_missing()
        if st.flow == "reschedule":
            return self._advance_reschedule(data)
        return self._advance_book(data)

    # ---------------- book
    def _advance_book(self, data):
        st, sl = self.state, self.state.slots
        if st.rebook and not sl.rebook_provider_id and not sl.any_provider:
            r = self._resolve_rebook()
            if r:
                return r
        if st.search_key != sl.search_key():
            self._set_phase(Phase.SEARCHING, "search")
            try:
                res = self._tool_retry("search_providers", self.api.search_providers, service=sl.service,
                                       area=sl.area, date_range=tuple(sl.date_range))
            except ToolError as e:
                self._set_phase(Phase.COLLECTING, "search failed")
                return (f"The provider search is unavailable right now ({e.code}). Nothing has been booked. "
                        "Please try again in a moment.")
            st.candidates = [self._prov(p) for p in res]
            st.search_key = sl.search_key()
            st.excluded, st.options, st.review_notes = [], [], {}
        return self._present(data)

    @staticmethod
    def _prov(p) -> dict:
        return {"id": p.id, "name": p.name, "service": p.service, "area": p.area, "price": p.price,
                "rating": p.rating, "free_slots": list(p.free_slots), "attributes": dict(p.attributes)}

    def _resolve_rebook(self) -> str | None:
        st, sl = self.state, self.state.slots
        if not sl.service:
            return None
        bks = self._tool("list_bookings", self.api.list_bookings, user_id=self.user_id)
        mine = [b for b in bks if b.service == sl.service and b.status in ("completed", "booked")]
        if not mine:
            self._set_phase(Phase.COLLECTING, "no past booking")
            st.rebook = False
            return (f"I couldn't find a past {label(sl.service)} booking, so I can't repeat it. "
                    "I can search for options instead; just tell me your area and which days work.")
        past = [b for b in mine if b.status == "completed"] or mine
        last = max(past, key=lambda b: b.slot)
        sl.rebook_provider_id = last.provider_id
        self._event("rebook_resolved", booking_id=last.id, provider_id=last.provider_id)
        return None

    def _in_window(self, slot: str) -> bool:
        sl = self.state.slots
        hhmm = slot[11:16]
        e, l = sl.time_window.get("earliest"), sl.time_window.get("latest")
        if e and hhmm < e:
            return False
        if l and hhmm >= l:
            return False
        if sl.date_range and not (sl.date_range[0] <= slot[:10] <= sl.date_range[1]):
            return False
        return slot > self.clock.now().isoformat(timespec="minutes")

    def _pref_ok(self, c) -> bool:
        a = c["attributes"]
        for p in self.state.slots.preferences:
            if p == "licensed" and not a.get("licensed"):
                return False
            if p == "experienced" and (a.get("years_experience") or 0) < 10:
                return False
            if p == "top_rated" and c["rating"] < 4.5:
                return False
        return True

    def _book_options(self):
        st, sl = self.state, self.state.slots
        preferred = (self.memory.usable(self.user_id).get("preferred_technician") or "").lower()
        taken = {tuple(x) for x in st.excluded}
        stages = {"found": len(st.candidates), "over_budget": 0, "prefs": 0, "no_slot": 0, "not_rebook": 0}
        cheapest_over = None
        opts = []
        for c in st.candidates:
            if sl.rebook_provider_id and not sl.any_provider and c["id"] != sl.rebook_provider_id:
                stages["not_rebook"] += 1
                continue
            if sl.budget and c["price"] > sl.budget:
                stages["over_budget"] += 1
                cheapest_over = c["price"] if cheapest_over is None else min(cheapest_over, c["price"])
                continue
            if not self._pref_ok(c):
                stages["prefs"] += 1
                continue
            free = [s for s in c["free_slots"] if (c["id"], s) not in taken and self._in_window(s)]
            if not free:
                stages["no_slot"] += 1
                continue
            opts.append({"provider_id": c["id"], "name": c["name"], "price": c["price"], "rating": c["rating"],
                         "slot": free[0], "more": free[1:3],
                         "pref": bool(preferred and preferred in c["name"].lower())})
        opts.sort(key=lambda o: (not o["pref"], -o["rating"], o["price"]))
        return opts[:MAX_OPTIONS], stages, cheapest_over

    def _explain_empty(self, stages, cheapest_over) -> str:
        sl = self.state.slots
        if stages["found"] == 0:
            return f"I couldn't find any {label(sl.service)} providers in {sl.area} for those dates."
        bits = []
        if stages["not_rebook"] and sl.rebook_provider_id:
            bits.append("your previous provider has no free slot that matches (say 'show other options' to see anyone)")
        if stages["over_budget"]:
            bits.append(f"{stages['over_budget']} were over your ₹{sl.budget} budget (cheapest ₹{cheapest_over})")
        if stages["prefs"]:
            bits.append(f"{stages['prefs']} didn't meet your preferences")
        if stages["no_slot"]:
            tw = sl.time_window
            bits.append(f"{stages['no_slot']} had no free slot in your time window"
                        + (f" ({tw.get('earliest') or '…'}–{tw.get('latest') or '…'})" if tw.get("earliest") or tw.get("latest") else ""))
        return (f"I found {stages['found']} provider(s) but none fit: " + "; ".join(bits)
                + ". Want to loosen the budget, time window or dates?")

    def _present(self, data, notice: str = "") -> str:
        st = self.state
        if st.flow == "reschedule":
            opts, why = self._reschedule_options(), "I couldn't find another free slot with your provider in that window. Try other days or times?"
        else:
            opts, stages, cheap = self._book_options()
            why = self._explain_empty(stages, cheap)
        if not opts:
            st.options = []
            self._set_phase(Phase.COLLECTING, "no options")
            return notice + why
        if st.flow != "reschedule":
            self._fetch_reviews(opts)
        st.options = opts
        self._set_phase(Phase.PRESENTING, "options ready")
        return notice + self._options_text()

    def _fetch_reviews(self, opts):
        st = self.state
        for o in opts:
            pid = o["provider_id"]
            if pid in st.review_notes:
                continue
            note = {"snippet": None, "flagged": 0}
            try:
                r = self._tool("get_provider", self.api.get_provider, provider_id=pid)
                for rv in r.get("reviews", []):
                    u = sanitize.wrap(rv.text, "review")
                    if u.flags:
                        note["flagged"] += 1
                        self._event("injection_flagged", provider_id=pid, review_id=rv.id, flags=list(u.flags),
                                    action="ignored; quarantined from LLM and workflow")
                        for f in u.flags:
                            if f not in st.review_flags:
                                st.review_flags.append(f)
                    elif note["snippet"] is None:
                        note["snippet"] = sanitize.render_for_user(u, 90)
            except ToolError:
                pass
            st.review_notes[pid] = note

    def _options_text(self) -> str:
        st = self.state
        lines = ["Here's what I found:" if st.flow != "reschedule" else "These times are free with your provider:"]
        flagged = 0
        for i, o in enumerate(st.options, 1):
            if st.flow == "reschedule":
                lines.append(f"{i}. {pretty_slot(o['slot'])}")
                continue
            line = f"{i}. {safe_text(o['name'])}: ₹{o['price']}, rating {o['rating']}, {pretty_slot(o['slot'])}"
            if o.get("more"):
                line += " (also " + ", ".join(pretty_slot(s) for s in o["more"]) + ")"
            lines.append(line)
            n = st.review_notes.get(o["provider_id"]) or {}
            if n.get("snippet"):
                lines.append("   Review: " + n["snippet"])
            flagged += n.get("flagged", 0)
        if flagged:
            lines.append(f"Note: {flagged} review(s) contained instruction-like text; I ignored them (reviews are never commands).")
        lines.append("Reply with a number to choose, or tell me what to change (budget, time, dates). Nothing is booked until you confirm.")
        return "\n".join(lines)

    # ---------------- choose -> proposal
    def _choose(self, idx, data) -> str:
        st = self.state
        if not st.options or st.flow is None:
            return "There's nothing to choose from yet. Tell me what you need and I'll look for options."
        if not isinstance(idx, int) or isinstance(idx, bool) or not 1 <= idx <= len(st.options):
            return f"Please pick a number between 1 and {len(st.options)}."
        if st.phase == Phase.AWAITING_APPROVAL:
            self.approvals.void_pending(self.user_id, "user_changed_choice")
            self._event("proposal_voided", reason="user_changed_choice")
        o = st.options[idx - 1]
        st.selected = dict(o)
        st.slots.provider_id, st.slots.slot = o["provider_id"], o["slot"]
        if st.flow == "reschedule":
            b = st.target_booking
            return self._propose("reschedule", b["provider_id"], b["slot"], b["price"], b["id"], o["slot"],
                                 f"Move your {label(b['service'])} booking {b['id']} with {safe_text(b.get('provider_name') or o['name'])} "
                                 f"from {pretty_slot(b['slot'])} to {pretty_slot(o['slot'])}", data)
        return self._propose("book", o["provider_id"], o["slot"], o["price"], None, None,
                             f"Book {safe_text(o['name'])} for {label(st.slots.service)} on {pretty_slot(o['slot'])} at ₹{o['price']}", data)

    def _propose(self, action, provider_id, slot, price, booking_id, new_slot, desc, data) -> str:
        st = self.state
        st.proposal_counter += 1
        pid = f"P-{self.user_id}-{st.proposal_counter}"
        text = f"{desc}. Reply yes to confirm."
        p = Proposal.build(pid, action, self.user_id, provider_id, slot, price, booking_id, new_slot, text)
        self.approvals.propose(p)
        st.pending_proposal_id = pid
        self._event("proposal_created", proposal_id=pid, action=action, fingerprint=p.fingerprint[:16], price=price)
        self._set_phase(Phase.AWAITING_APPROVAL, "proposal shown")
        data["proposal"] = {"id": pid, "action": action, "provider_id": provider_id, "slot": slot,
                            "price": price, "booking_id": booking_id, "new_slot": new_slot}
        return text

    # ---------------- cancel / reschedule: resolve the target booking
    def _resolve_target(self):
        """Returns (booking_dict|None, message|None)."""
        st = self.state
        hint = (st.target_booking or {}).get("hint", {}) if st.target_booking and "hint" in st.target_booking else {}
        if st.target_booking and "id" in st.target_booking:
            return st.target_booking, None
        bks = self._tool("list_bookings", self.api.list_bookings, user_id=self.user_id)
        live = [b for b in bks if b.status == "booked"]
        if hint.get("booking_id"):
            live = [b for b in live if b.id == hint["booking_id"]]
        svc = hint.get("service") or st.slots.service
        if svc:
            live = [b for b in live if b.service == svc]
        if not live:
            return None, ("I couldn't find an upcoming " + (label(svc) + " " if svc else "") +
                          "booking to change. Nothing was changed.")
        if len(live) > 1:
            return None, ("I found several bookings: " + "; ".join(f"{b.id} ({label(b.service)}, {pretty_slot(b.slot)})" for b in live)
                          + ". Which one do you mean? (give the booking id)")
        b = live[0]
        name = self._provider_name(b.provider_id)
        return {"id": b.id, "provider_id": b.provider_id, "service": b.service, "slot": b.slot,
                "price": b.price, "provider_name": name}, None

    def _provider_name(self, pid) -> str:
        try:
            return self._tool("get_provider", self.api.get_provider, provider_id=pid)["provider"].name
        except LimitExceeded:
            raise
        except Exception:
            return "your provider"

    def _advance_cancel(self, data):
        st = self.state
        b, msg = self._resolve_target()
        if b is None:
            self._set_phase(Phase.COLLECTING, "cancel target unresolved")
            return msg
        st.target_booking = b
        st.selected = dict(b)
        return self._propose("cancel", b["provider_id"], b["slot"], b["price"], b["id"], None,
                             f"Cancel your {label(b['service'])} booking {b['id']} with {safe_text(b['provider_name'])} on {pretty_slot(b['slot'])}", data)

    def _advance_reschedule(self, data):
        st, sl = self.state, self.state.slots
        b, msg = self._resolve_target()
        if b is None:
            self._set_phase(Phase.COLLECTING, "reschedule target unresolved")
            return msg
        st.target_booking = b
        sl.service = b["service"]
        key = ["reschedule", b["id"], sl.date_range, sl.area]
        if st.search_key != key:
            area = self._booking_area(b["id"]) or sl.area or self.memory.usable(self.user_id).get("area") or ""
            if not area:  # e.g. area forgotten: take it from the provider's own record, not from memory
                try:
                    area = self._tool("get_provider", self.api.get_provider,
                                      provider_id=b["provider_id"])["provider"].area
                except ToolError:
                    area = ""
            self._set_phase(Phase.SEARCHING, "search new slots")
            try:
                res = self._tool_retry("search_providers", self.api.search_providers, service=b["service"],
                                       area=area, date_range=tuple(sl.date_range))
            except ToolError as e:
                self._set_phase(Phase.COLLECTING, "search failed")
                return f"The provider search is unavailable right now ({e.code}). Your booking is unchanged."
            st.candidates = [self._prov(p) for p in res if p.id == b["provider_id"]]
            st.search_key, st.excluded, st.options, st.review_notes = key, [], [], {}
        return self._present(data)

    def _booking_area(self, bid):
        m = self.storage.kv_get("agent_bookings", bid)
        return (m or {}).get("area")

    def _reschedule_options(self):
        st = self.state
        b = st.target_booking
        taken = {tuple(x) for x in st.excluded}
        opts = []
        for c in st.candidates:
            for s in c["free_slots"]:
                if s != b["slot"] and (c["id"], s) not in taken and self._in_window(s):
                    opts.append({"provider_id": c["id"], "name": b.get("provider_name") or c["name"], "price": b["price"],
                                 "rating": c["rating"], "slot": s, "more": [], "pref": False})
        return opts[:4]

    # ================================================================== execute + map results
    def _execute(self, prop: Proposal, data: dict) -> str:
        st = self.state
        key = self.executor.current_key(self.user_id, prop.action, prop.fingerprint)
        st.in_flight = {"key": key, "action": prop.action, "provider_id": prop.provider_id, "slot": prop.slot,
                        "price": prop.price, "booking_id": prop.booking_id, "new_slot": prop.new_slot}
        if key not in st.journal_refs:
            st.journal_refs.append(key)
        self._set_phase(Phase.EXECUTING, "approved; executing")
        res = self.executor.execute(self.user_id, prop.action, prop.provider_id, prop.slot, prop.price,
                                    prop.booking_id, prop.new_slot)
        return self._map_result(st.in_flight, res, data)

    def _map_result(self, fl: dict, res: ExecResult, data: dict) -> str:
        st = self.state
        self._event("exec_result", status=res.status, verified=res.verified, booking_id=res.booking_id)
        if res.status in SUCCESS and res.verified:
            return self._complete(fl, res, data)
        if res.status == "already_done":
            return self._complete(fl, res, data, already=True)
        if res.status == "needs_reapproval":
            st.in_flight = None
            new_price = res.new_price
            old = fl["price"]
            if fl["action"] == "book" and new_price is not None:
                o = st.selected or {}
                desc = f"Book {safe_text(o.get('name', 'this provider'))} for {label(st.slots.service)} on {pretty_slot(fl['slot'])} at ₹{new_price}"
                if st.selected:
                    st.selected["price"] = new_price
                    for op in st.options:
                        if op["provider_id"] == fl["provider_id"]:
                            op["price"] = new_price
                    for c in st.candidates:
                        if c["id"] == fl["provider_id"]:
                            c["price"] = new_price
                prefix = f"The price changed from ₹{old} to ₹{new_price} when I checked with the provider, so your earlier confirmation no longer applies. "
                return prefix + self._propose("book", fl["provider_id"], fl["slot"], new_price, None, None, desc, data)
            self._set_phase(Phase.FAILED, "reapproval unsupported")
            return "The price changed, so I stopped. Nothing was changed; please ask again."
        if res.status in ("slot_taken", "slot_conflict"):
            st.in_flight = None
            bad = fl["slot"] if fl["action"] == "book" else fl["new_slot"]
            st.excluded.append([fl["provider_id"], bad])
            sorry = (f"Sorry, {pretty_slot(bad)} just became unavailable"
                     + ("" if fl["action"] == "book" else "; your existing booking is unchanged") + ". ")
            st.selected = None
            return self._present(data, notice=sorry)
        if res.status == "unverified" or (res.status in SUCCESS and not res.verified):
            self._set_phase(Phase.RECOVERING, "outcome unverified")
            return ("I couldn't confirm whether that went through, so I'm not going to tell you it did. "
                    "I'll check with the provider; send any message (e.g. 'check status') and I'll report what I find.")
        st.in_flight = None
        self._set_phase(Phase.FAILED, f"exec {res.status}")
        return f"I couldn't complete that: {res.message} Nothing further was changed. You can ask me to try again."

    def _complete(self, fl: dict, res: ExecResult, data: dict, already: bool = False) -> str:
        st = self.state
        a, bid = fl["action"], res.booking_id or fl.get("booking_id")
        name = (st.selected or {}).get("name") or (st.target_booking or {}).get("provider_name") or "the provider"
        if a == "book":
            summ = f"booked {safe_text(name)} on {pretty_slot(fl['slot'])} for ₹{fl['price']}"
            msg = f"Booked and confirmed. Reference {bid}: {safe_text(name)}, {pretty_slot(fl['slot'])}, ₹{fl['price']}."
            self.storage.kv_set("agent_bookings", bid, {"area": st.slots.area, "provider_name": name})
            self._learn_after_booking(name)
        elif a == "cancel":
            summ = f"cancelled booking {bid}"
            msg = f"Cancelled. Booking {bid} is cancelled (confirmed with the provider)."
        else:
            summ = f"rescheduled booking {bid} to {pretty_slot(fl['new_slot'])}"
            msg = f"Rescheduled. Booking {bid} is now on {pretty_slot(fl['new_slot'])} (confirmed with the provider)."
        if already:
            msg = f"That was already completed earlier (reference {bid}); I did not do it again."
        st.last_result = {"action": a, "booking_id": bid, "summary": summ, "key": fl.get("key")}
        st.in_flight, st.options, st.flow, st.pending_proposal_id = None, [], None, None
        data["booking_id"] = bid
        self._set_phase(Phase.DONE, f"{a} verified")
        return msg

    def _learn_after_booking(self, name):
        """Inferred facts are stored unconfirmed; they are only used after the user says yes."""
        try:
            if self.state.slots.area:
                self.memory.propose_inferred(self.user_id, "area", self.state.slots.area)
            if name and safe_text(name) != "(name hidden)":
                self.memory.propose_inferred(self.user_id, "preferred_technician", safe_text(name))
        except Exception:
            pass

    # ================================================================== recovery
    def _recover_now(self, data) -> str:
        results = self.executor.recover(self.user_id)
        if not results:
            if self.state.in_flight:
                return "I still can't confirm the earlier action. I haven't assumed it succeeded."
            self._set_phase(Phase.COLLECTING, "nothing to recover")
            return "Nothing is pending. What would you like to do?"
        return "\n".join(self._apply_recovery(results, data))

    def _recover_startup(self) -> list[str]:
        st = self.state
        results = self.executor.recover(self.user_id)
        self.recovery_results = results
        lines = self._apply_recovery(results, {})
        fl = st.in_flight
        if st.phase == Phase.EXECUTING and fl and not any(r.idempotency_key == fl["key"] for r in results):
            if self.executor.journal_get(fl["key"]) is None:
                # approved, but the process died before the executor journaled it: run it exactly once
                res = self.executor.execute(self.user_id, fl["action"], fl["provider_id"], fl["slot"], fl["price"],
                                            fl["booking_id"], fl["new_slot"])
                lines.append(self._map_result(fl, res, {}))
        return lines

    def _apply_recovery(self, results, data) -> list[str]:
        st = self.state
        fl = st.in_flight
        lines = []
        for r in results:
            mine = bool(fl) and r.idempotency_key == fl["key"]
            if r.status in SUCCESS and r.verified:
                if mine:
                    lines.append("After the restart I re-checked with the provider. " + self._complete(fl, r, data))
                else:
                    lines.append(f"Recovered an earlier action: {r.message} (reference {r.booking_id}).")
            elif r.status == "unverified":
                lines.append("I still can't confirm an earlier action went through; I haven't assumed it did and will keep checking."
                             if not mine else "I re-checked, but still can't confirm the outcome. I won't claim it succeeded.")
                if mine:
                    self._set_phase(Phase.RECOVERING, "still unverified")
            else:
                lines.append(f"Earlier action did not complete: {r.message}")
                if mine:
                    st.in_flight = None
                    self._set_phase(Phase.PRESENTING if st.options else Phase.COLLECTING, "recovered: not done")
        return lines
