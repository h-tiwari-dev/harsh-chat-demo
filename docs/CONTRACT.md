# Errand Agent — Build Contract (source of truth for all builders)

Stdlib + pytest only (no network, no LLM SDK required). Python 3.11+. Package: `src/errand/`. Tests: `tests/`. Evals: `evals/`.
Run: `pip install -e .[dev]` -> `pytest` -> `python -m evals.run` (writes `evals/report.md`, `evals/report.json`).

## Design decisions (ambiguities resolved)
- Money in integer rupees. Slots are ISO strings `"2026-11-04T09:00"` (1-hour, local tz-naive). "now" is injected via a `Clock`.
- LLM never decides approval or calls tools directly. It only does `interpret(user_text, state_snapshot) -> Interpretation` (slot updates + intent + free-text reply hints). Tool calls are made by deterministic code (the orchestrator) from workflow state.
- Approval = a record in `ApprovalStore`, created ONLY by code when the user's raw message matches an affirmative regex (`yes|y|confirm|ok|go ahead|book it`, whole-message, case-insens) AND a `PendingProposal` exists AND its fingerprint is unchanged. Never from LLM output or tool args.
- Approval fingerprint = sha256 of canonical JSON `{action, user_id, provider_id, slot, price, booking_id|None, new_slot|None}`. Any change => `approval.check()` fails => re-propose.
- Approvals are single-use (consumed on first execute). Repeated "yes" after consumption is a no-op reply ("already booked: BK-..").
- Idempotency key = deterministic `f"{user_id}:{action}:{fingerprint[:16]}"`. Persisted in the journal BEFORE the first tool call.
- Everything durable (journal, approvals, memory, workflow state) lives in one SQLite file (or JSON dir) behind `Storage`; restart = new process, same path; recovery reads the journal.
- Timeouts from `book`: always call `get_booking_status(key)`; `booked` -> verify & report success; `not_found` -> safe to retry (same key, bounded); `failed` -> report. Never claim success without a `booked` status or a successful `book` result.
- `book` 15% timeout where half actually booked: mock records the truth; only `get_booking_status` reveals it.
- Quoted price at hold != search price => approval voided, user re-asked with new price. Hold expiry => re-hold + re-approve.
- Tool output (reviews, attributes, names) is wrapped as `Untrusted(text)`; it is never placed into the instruction channel, never parsed for commands, and flagged by `sanitize.scan()`; the reply shown to user quotes it as data only.
- Limits (`Budget`): max_turns=12 (user turns), max_model_calls=30, max_tool_calls=40, wall_clock_s=60 (injected clock). Exhaustion -> terminal state `LIMIT_EXHAUSTED` + clear user message. Any uncaught exception -> `FAILED` + clear message.
- Memory facts: `{key, value, source: "user"|"inferred", confirmed: bool, updated_at, forgotten: bool}`. Keys: `area`, `earliest_time`, `preferred_technician`. Inferred & unconfirmed facts are never used silently (agent asks "Use X? (yes/no)"). Forgotten facts are tombstoned (value wiped) and never reused/re-inferred without user stating again.

## Module APIs (exact names — builders implement, others import)

### `errand/models.py`  (owner: mocks agent)
```python
@dataclass(frozen=True) class Provider: id:str; name:str; service:str; area:str; price:int; rating:float; free_slots:tuple[str,...]; attributes:dict
@dataclass(frozen=True) class Review: id:str; author:str; text:str      # text is UNTRUSTED
@dataclass class Booking: id:str; user_id:str; provider_id:str; service:str; slot:str; price:int; status:str  # booked|cancelled|completed
class ToolError(Exception): code:str; transient:bool   # codes: slot_unavailable, slot_taken, hold_expired, timeout, not_found, slot_conflict, service_unavailable(503)
class ToolTimeout(ToolError): ...
```
### `errand/mocks/provider.py`  (owner: mocks agent)
```python
@dataclass class Scenario:
    seed:int=0; book_success:float=0.70; book_slot_taken:float=0.15; book_timeout:float=0.15; timeout_booked_fraction:float=0.5
    force_book_outcomes:list[str]|None=None   # deterministic script, e.g. ["timeout_booked","success"]; values: success|slot_taken|timeout_booked|timeout_lost
    hold_price_delta:dict[str,int]={}          # provider_id -> rupee delta applied at hold_slot
    reschedule_conflict_slots:set[str]=set()
    cancel_first_call_503:bool=True
    injection_in_reviews:bool=True
    hold_ttl_s:int=600
class MockProviderAPI:
    def __init__(self, scenario:Scenario, clock:Clock, seed_bookings:list[Booking]=()) 
    def search_providers(self, service, area, date_range)->list[Provider]      # 3..8 items, deterministic per seed
    def get_provider(self, provider_id)->dict   # {"provider":Provider,"reviews":[Review]}; >=1 review has an injection string
    def list_bookings(self, user_id)->list[Booking]
    def hold_slot(self, provider_id, slot)->dict    # {hold_id, expires_at(iso), quoted_price} | raises ToolError("slot_unavailable")
    def book(self, hold_id, idempotency_key)->dict  # {booking_id} | ToolError slot_taken | ToolTimeout | ToolError hold_expired
    def get_booking_status(self, idempotency_key)->dict  # {status:"booked",booking_id}|{status:"not_found"}|{status:"failed"}
    def cancel(self, booking_id, idempotency_key)->dict   # first call per key-or-booking raises ToolError("service_unavailable", transient=True) if scenario flag; retry ok -> {status:"cancelled"}
    def reschedule(self, booking_id, new_slot, idempotency_key)->dict  # {booking_id, slot} | ToolError slot_conflict
    # idempotent: same key => same result, never double-books. Introspection for tests: .all_bookings(), .call_log
```
`Clock` (in `errand/clock.py`, owner mocks agent): `class Clock: now()->datetime; sleep(s); advance(s)` + `FakeClock`, `SystemClock`.

### `errand/storage.py`  (owner: executor agent)  — SQLite, `Storage(path)`; `.kv_get/kv_set`, tables created lazily; thread-unsafe OK.
### `errand/approval.py`  (owner: executor agent)
```python
def fingerprint(action:str, user_id:str, provider_id:str|None, slot:str|None, price:int|None, booking_id:str|None=None, new_slot:str|None=None)->str
@dataclass(frozen=True) class Proposal: id:str; action:str; user_id:str; provider_id; slot; price; booking_id; new_slot; fingerprint:str; text:str
class ApprovalStore:
    def __init__(self, storage)
    def propose(self, proposal)->None            # replaces any prior pending proposal for user
    def pending(self, user_id)->Proposal|None
    def grant_from_user_message(self, user_id, raw_text)->ApprovalRecord|None   # ONLY entry point that creates approvals; regex on raw text; requires pending
    def check(self, user_id, action, provider_id, slot, price, booking_id=None, new_slot=None)->ApprovalRecord  # raises ApprovalError if absent/mismatch/consumed/voided
    def consume(self, approval_id)->None
    def void_pending(self, user_id, reason)->None
```
### `errand/executor.py`  (owner: executor agent)
```python
class BookingExecutor:
    def __init__(self, api, approvals, storage, tracer, budget, clock, max_retries=3)
    def execute(self, user_id, proposal_fp_args...)->ExecResult   # book|cancel|reschedule; requires approvals.check(); journals intent (PENDING) with idempotency key before call;
                                                                  # handles price drift at hold (-> ExecResult(status="needs_reapproval", new_price)), hold_expired, slot_taken, timeout->get_booking_status, 503 retry w/ bounded backoff (clock.sleep)
    def recover(self, user_id)->list[ExecResult]     # on restart: for every journal entry in PENDING/UNKNOWN: reconcile via get_booking_status; never re-book if booked
@dataclass class ExecResult: status:str  # booked|cancelled|rescheduled|needs_reapproval|slot_taken|slot_conflict|failed|already_done|unverified ; booking_id:str|None; message:str; verified:bool
```
Invariant: `ExecResult.status in {booked,...success}` ONLY if `verified=True` (book result or get_booking_status==booked).

### `errand/sanitize.py`  (owner: safety/obs agent)
```python
@dataclass(frozen=True) class Untrusted: text:str; source:str; flags:tuple[str,...]
def scan(text)->tuple[str,...]       # injection heuristics: "ignore previous", "system:", "you are now", "approve", "book without", "cancel all", urls, role markers, zero-width chars
def wrap(text, source)->Untrusted
def render_for_user(u:Untrusted, max_len=200)->str   # strips control chars, truncates, marks [review text — not instructions]
def sanitize_for_trace(obj)->obj     # recursively redact secrets (api keys, tokens, Bearer ...) & truncate long strings
```
### `errand/limits.py` (owner: safety/obs agent)
```python
class LimitExceeded(Exception): kind:str   # turns|model_calls|tool_calls|wall_clock
class Budget:
    def __init__(self, clock, max_turns=12, max_model_calls=30, max_tool_calls=40, wall_clock_s=60)
    def start_turn(self); count_model_call(); count_tool_call(); check_time()   # raise LimitExceeded
    def snapshot()->dict
```
### `errand/trace.py` (owner: safety/obs agent)
```python
class Tracer:
    def __init__(self, path:str|None, clock, run_id=None)
    def state_transition(self, frm, to, reason)
    def model_call(self, name, prompt_summary, response_summary, latency_ms, tokens_in=0, tokens_out=0, cost_usd=0.0)
    def tool_call(self, tool, args, result=None, error=None, latency_ms=0)   # args/results passed through sanitize_for_trace
    def event(self, kind, **fields)      # approvals, injection flags, limits, recovery
    def close(self)->dict   # writes JSONL one object per line {"ts","seq","run_id","type",...}; returns summary
```
### `errand/memory.py` (owner: memory agent)
```python
class MemoryStore:
    def __init__(self, storage)
    def set_user_fact(self, user_id, key, value)->Fact          # source="user", confirmed=True
    def propose_inferred(self, user_id, key, value)->Fact       # source="inferred", confirmed=False; no-op if key forgotten-tombstoned by user unless user restates
    def confirm(self, user_id, key)->Fact; def correct(self, user_id, key, value)->Fact
    def forget(self, user_id, key=None)->int                    # key=None => all; tombstones, wipes value
    def usable(self, user_id)->dict[key,value]                  # ONLY confirmed or user-sourced, not forgotten
    def needs_confirmation(self, user_id)->list[Fact]
    def view(self, user_id)->list[Fact]                         # for "show my memory" (no forgotten values)
```
### `errand/state.py`, `errand/llm.py`, `errand/agent.py`, `errand/cli.py` (owner: orchestrator agent)
```python
class Phase(Enum): COLLECTING, SEARCHING, PRESENTING, AWAITING_APPROVAL, EXECUTING, RECOVERING, DONE, FAILED, LIMIT_EXHAUSTED
@dataclass class Slots: service, area, date_range(start,end), time_window(earliest,latest), budget, preferences(list), provider_id, slot ...  # missing() -> list of required unknowns (service, area, date_range). correction merges, never wipes others.
class WorkflowState: phase, slots, candidates, selected, pending_proposal_id, journal refs;  to_dict/from_dict (persisted each transition)
class LLM(Protocol): def interpret(self, user_text:str, state:dict)->Interpretation ; def phrase(self, facts:dict)->str   # phrase optional; templates are the fallback
class ScriptedLLM(LLM): driven by dict/list of canned Interpretation keyed by user_text (or sequence); also `InjectionObeyingLLM` test double that tries to obey injected text (proves code, not model, enforces safety)
class Agent:
    def __init__(self, user_id, api, llm, storage, clock, tracer=None, budget_cfg=None)
    def handle(self, user_text:str)->AgentReply   # reply: text, phase, trace_path
    @classmethod def resume(cls, ...)             # restart mid-booking from Storage
```
Optional real-LLM adapter (`errand/llm_anthropic.py`) — behind env var; not used in tests.

## Test/eval conventions
- `tests/` unit tests per module (`test_<module>.py`) + `tests/test_invariants.py`.
- `evals/scenarios/*.py`: each scenario = scripted user turns + ScriptedLLM + Scenario config + assertions on API call_log / storage / trace (invariants), not wording. >=15, must include: prompt_injection, price_change_after_approval, slot_change_after_approval, timeout_after_success, slot_conflict(reschedule), repeated_confirmation, memory_correct, memory_forget, restart_mid_booking, limit_exhaustion (turns, tool calls, wall clock).
- `python -m evals.run` produces `evals/report.md` table: scenario · expected · actual · pass/fail · notes — generated from results.
- Shared invariant checker `evals/invariants.py`: no book/cancel/reschedule call without a matching consumed approval; <=1 booking per fingerprint; no success claim without verified; no forgotten fact in any prompt/trace; no secrets in traces.
