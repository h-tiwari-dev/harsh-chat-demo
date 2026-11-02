# Design note

The LLM only maps text to an `Interpretation` (intent plus slot updates). Code owns approval, tool calls, idempotency, limits and recovery.

## State model
`WorkflowState` holds a `Phase` (COLLECTING, SEARCHING, PRESENTING, AWAITING_APPROVAL, EXECUTING, RECOVERING, DONE, FAILED, LIMIT_EXHAUSTED) and `Slots` (service, area, dates, time window, budget, preferences, chosen provider and slot). Slot merges never wipe other slots. The state is saved to SQLite on every phase change and after every turn, so `Agent.resume` rebuilds it after a restart.

## Approval enforcement
- The fingerprint is sha256 of `{action, user_id, provider_id, slot, price, booking_id, new_slot}`. Any change means a different fingerprint, so a different approval is needed.
- `ApprovalStore.grant_from_user_message` is the only code that creates an approval. It runs on the user's raw text before the LLM is called, needs a whole-message affirmative, a pending proposal and an intact fingerprint.
- `BookingExecutor.execute` calls `approvals.check()` before any mutating call, writes the `PENDING` journal entry, then consumes the approval (single use). A repeated "yes" finds nothing pending.
- Price drift at hold voids the approval and asks again.
- Tests: `tests/test_executor.py::test_no_approval_no_tool_calls`, `test_wrong_price_approval_rejected`, `test_price_drift_voids_without_booking`; `tests/test_approval.py` (single use, tamper, embedded "yes" denied); evals `price_change_after_approval`, `repeated_confirmation_yes_x3`; `InjectionObeyingLLM` shows a model that obeys injected text still cannot approve anything.

## Idempotency and recovery
The key is `user:action:fingerprint[:16]`, with a `:gN` suffix once the booking made under it is cancelled or rescheduled (BUG-3). The journal entry (`PENDING`) is written before `hold_slot`. On `ToolTimeout` from `book`, the executor calls `get_booking_status(key)`: `booked` is verified success, `failed` is reported, `not_found` retries with the same key (max 3, exponential backoff via the injected clock), then `unverified`, which puts the agent in RECOVERING and says so. `recover()` runs on resume and on any message in RECOVERING: it reads `PENDING`/`UNKNOWN` journal entries and reconciles them with `get_booking_status`, or with `list_bookings` for cancel and reschedule (they have no status endpoint). It never re-books a booking that is already `booked`.

## Untrusted output
Reviews and provider names go through `sanitize.wrap`, which attaches `scan()` flags (ignore-previous, role markers, approve, book-without-asking, cancel-all, URLs, base64, zero-width and control characters). The user sees them only via `render_for_user` (quoted, truncated, marked "not instructions"); flagged provider names show as "(name hidden)". The LLM gets a snapshot built from `Slots.public()` and flag names, never tool text. Intents outside `ALLOWED_INTENTS` (for example `cancel_all`) are dropped and traced.

## Bounded execution
`Budget` caps user turns (12), model calls (30), tool calls (40) and wall clock (60 s on the injected clock). Every tool call, including those in the executor, counts. Exhaustion voids the pending approval, moves to LIMIT_EXHAUSTED and tells the user whether an earlier action is unfinished. Any other exception moves to FAILED with a message, after voiding the pending approval.

## Memory
Facts carry provenance: `user` or `inferred`, plus `confirmed`. `usable()` returns only user-stated or confirmed facts. Inferred facts are asked about ("Use X?") before use. `forget` writes a tombstone with the value wiped; inferred proposals cannot overwrite a tombstone, and forgetting also scrubs derived workflow state and booking history (BUG-1, BUG-5).

## Observability
`Tracer` writes one JSON object per line: `ts, seq, run_id, trace_id, span_id, type` plus fields. Types are `state_transition`, `model_call` (latency, tokens, cost), `tool_call` (args, result or error, latency) and `event` (approvals, injection flags, limits, recovery). Everything passes `sanitize_for_trace` (secret keys and patterns redacted, strings truncated at 2000 characters).

## Nice-to-have: proactive follow-up
`check_followups()` notices a provider-cancelled booking, tells the user once, finds a replacement under the saved constraints and presents options. It never books: the user's `yes` goes through the normal approval path (`tests/test_followup.py`).

## Annotated trace: timeout after success
From `docs/trace_timeout_after_success.jsonl` (trimmed, key `demo-user:book:8a0b335e4892621b` shortened to `KEY`):

```
 5 event      injection_flagged   flags=[approve, book_without_asking, ignore_previous]  -> review quarantined
13 transition PRESENTING -> AWAITING_APPROVAL      proposal 8a0b33.. shown, no tool has mutated anything
14 event      approval_granted    from user's raw "yes", fingerprint 8a0b335e4892621b
15 transition AWAITING_APPROVAL -> EXECUTING
16 event      approval_consumed   key=KEY                      <- single use; journal PENDING already written
17 tool_call  hold_slot           -> quoted_price 960          (equals approved price, so no re-approval)
18 tool_call  book(KEY)           error "ToolTimeout"          <- the provider did book; we cannot know yet
19 tool_call  get_booking_status(KEY) -> booked, BK-0001      <- verify, do not re-book
20 event      exec_result         status=booked verified=true
21 transition EXECUTING -> DONE   "book verified"
22 event      repeat_confirmation booking_id=BK-0001           <- second "yes": no tool call
```
Only one `book` call appears in the run. Token counts are 0 and latencies 0 because this run uses the offline `RuleBasedLLM` and a fake clock.

## What breaks first at 100x
- **SQLite and the KV layer.** One connection, a commit per `kv_set`, single writer. `kv_items(ns)` loads whole namespaces, so `check()` scans every approval and `journal_entries()` scans every journal entry for all users. `recover()` is O(journal). Fix: Postgres, indexed per-user tables, and a partial index on open journal states.
- **No locking on approvals.** Two processes handling the same user could both grant or consume. Fix: compare-and-set on approval status, and a per-user lock or session affinity.
- **Mock assumptions.** Idempotency is only as good as the provider's. The mock honours keys perfectly; a real provider may not, and `get_booking_status` may not exist. Provider-side cancellation is seen only when `check_followups` polls, not pushed by webhook.
- **Regex brittleness.** Approval matching and `RuleBasedLLM` parsing are regexes, so unusual phrasings ("sure, go for it") do not count as approval. That is the safe direction, but it frustrates users. Injection `scan()` is a heuristic and only flags; the real defence is that tool text never reaches the LLM or the instruction path.
- **Wall clock includes user think time.** The 60 s budget starts when the agent is created, so a slow human trips it. Fix: budget active processing time per turn.
- **Tracing.** JSONL files, no sampling, rotation or export, one file handle per run. Fix: OpenTelemetry exporter with sampling, and keep full traces for failures.
- **Token accounting is plumbing only.** The tracer sums tokens, but `Agent._interpret` always records 0, and `AnthropicLLM` does not return usage, so live costs would not show up.
