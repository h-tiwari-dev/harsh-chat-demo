# Bugs log (found by `evals/`)

Process: the eval suite and shared invariant checker were written first and run against the unmodified code; fixes
were made only afterwards. **Six genuine product bugs were found and fixed (BUG-1/2 by the eval suite, BUG-3/4/5 by an independent adversarial review, BUG-6 by watching the live trace panel).** A 400-seed random-walk fuzz
(random user messages, 20/30/50 success/slot_taken/timeout outcomes, random tool budgets, 503 on/off) checked with
`evals/invariants.py` found nothing further. Failures that turned out to be mistakes in my own first-draft
scenarios/checker (wrong snapshot index, budget below the RuleBasedLLM's 50 minimum, regex in the secrets check
matching `password=[REDACTED]`, an option that sorted out of the top 3, a model-call limit that approval turns never
consume) were test bugs, fixed in `evals/`, and are not counted here.

## BUG-1: "forget my area" left the forgotten value in persisted workflow state

- **What broke:** Memory `area` pre-filled a search ("Find a plumber this week" used the saved area `Secretville`).
  After `forget my area` the memory store was wiped and nothing leaked to the LLM, replies or trace, but the durable
  `workflow` record in Storage still contained `Secretville` in `candidates[*].area` and `search_key`.
  The forget contract is that the value is gone, so the persisted state contradicted it.
- **Found by:** eval `memory_forget`, check `workflow_state_clean` (expected False, got True).
- **Root cause:** `Agent._drop_prefilled` cleared the pre-filled slot (and `options`) but not the search results and
  search key derived from the dropped value.
- **Fix:** `src/errand/agent.py::_drop_prefilled` also resets `candidates`, `search_key`, `review_notes`, `selected`
  when a pre-filled memory value is dropped (forget / correction). Three added lines.
- **Regression tests:** `tests/test_evals.py::test_regression_forget_prefilled_area_leaves_no_trace_in_workflow_state`;
  eval scenario `memory_forget`.
- **Commit-ready description:**
  `fix(agent): scrub search state derived from forgotten/corrected memory facts` - forgetting a pre-filled fact cleared
  the slot but left candidates and search_key (which embed the area) in the persisted workflow record. Reset them in
  `_drop_prefilled`.

## BUG-2: restart after a crash during cancel/reschedule leaves the user stuck in RECOVERING forever

- **What broke:** If the process died after the cancel/reschedule intent was journaled (state PENDING) but before the
  request reached the provider, `resume` -> `executor.recover` -> `_reconcile` returned `unverified` on every
  attempt. The agent stayed in `RECOVERING` and answered "I re-checked, but still can't confirm the outcome" to every
  message ("hello", a fresh "Cancel my AC servicing", "yes"), while the provider's booking was still `booked`.
  No wrong claim was made (safe), but the user could never cancel or reschedule again in that session or after
  further restarts. (The equivalent booking crash was fine because `get_booking_status` exists.)
- **Found by:** evals `restart_mid_cancel` (a) and `restart_mid_reschedule` (not_stuck_in_recovering,
  fresh_proposal_possible, cancel_completes_after_retry all failed before the fix).
- **Root cause:** for cancel/reschedule there is no status endpoint, so `_reconcile` verifies via `list_bookings`, but
  it only recognised the "applied" outcomes. A booking that is visibly unchanged fell through to `unverified`
  instead of "not applied".
- **Fix:** `src/errand/executor.py::_reconcile`: if the provider's own booking list shows the booking still `booked`
  (cancel) or still on the original slot (reschedule), finish the journal entry as `failed`, message "the <action> was
  not applied; your booking is unchanged. Please confirm again to retry". Re-proposing reuses the same fingerprint and
  idempotency key, so a request that lands late cannot apply twice. The "applied" and "cannot read the list" paths are
  unchanged (the latter stays `unverified`).
- **Regression tests:**
  `tests/test_evals.py::test_regression_restart_after_unapplied_cancel_or_reschedule_is_not_stuck[cancel|reschedule]`;
  eval scenarios `restart_mid_cancel` (both crash-before and crash-after-provider-applied variants) and
  `restart_mid_reschedule`.
- **Commit-ready description:**
  `fix(executor): reconcile cancel/reschedule that never reached the provider` - recovery treated an untouched booking
  as "unverified", trapping the user in RECOVERING. Report it as not applied so a fresh confirmation can retry safely.

## BUG-3 (independent review): re-booking the same provider/slot/price after a cancel was reported as "already completed"

- **What broke:** book P/slot/price, cancel that booking, then book the same P/slot/price again and say yes. The
  executor found the old journal entry for the identical idempotency key in state BOOKED and returned `already_done`
  (the agent said "already completed earlier; I did not do it again") while no live booking existed. The mock (like a
  real idempotent provider) would also replay the old cancelled booking for that key.
- **Found by:** reviewer repro `tests/test_regression_review.py::test_regression_rebook_same_slot_after_cancel_is_really_booked`.
- **Root cause:** the idempotency key is a pure function of (user, action, fingerprint), so it never changes even when
  the booking it produced has since been cancelled or moved.
- **Fix:** `executor.current_key()` adds a generation suffix (`:gN`, stored in ns `keygen`). A successful cancel or
  reschedule retires the key of the book entry that produced that booking (journal state `SUPERSEDED`), so the next
  identical booking uses a fresh key. `Agent._execute` uses `current_key`.
- **Regression test:** `tests/test_regression_review.py::test_regression_rebook_same_slot_after_cancel_is_really_booked`.
- **Not covered:** a booking cancelled by the provider (not through the agent) is not detected at execute time.

## BUG-4 (independent review): trace redaction missed secrets nested under a sensitive key

- **What broke:** `sanitize_for_trace({"api_key": ["abc123..."]})` and `{"credentials": {"pass": "hunter2"}}` were
  written to traces unredacted; only scalar values directly under a sensitive key were masked, and `pass`/`pwd` keys
  were not recognised.
- **Fix:** `src/errand/sanitize.py`: everything under a sensitive key is redacted leaf by leaf (`_redact_all`);
  `pwd` and `pass` added to the key pattern (kept the token-count exemption added separately).
- **Regression test:** `tests/test_regression_review.py::test_regression_trace_redacts_secrets_inside_lists_under_sensitive_keys`.

## BUG-5 (independent review): forgotten area kept in booking history and silently reused by reschedule

- **What broke:** after `forget my area`, `agent_bookings` still held the area and the next reschedule searched with
  it (reproduced: search_providers called with `area='Indiranagar'` after the forget). This promoted the earlier
  "observation" below into a defect: a forgotten value was reused.
- **Fix:** `agent.py::_scrub_booking_areas` nulls the stored area on forget (area or everything); reschedule falls
  back to the provider's own record (`get_provider(...).area`) when no area is known.
- **Regression test:** `tests/test_regression_review.py::test_regression_forgotten_area_not_kept_in_booking_history_or_reused_by_reschedule`.
- **Still open:** `agent_bookings.provider_name` and the mock's provider ids (which embed an area slug) remain in
  booking history; that is the provider's identity, not a memory fact.

## BUG-6 (found in the live demo): token counts were redacted in every model-call trace record

**What broke.** In the demo's trace panel every model call showed `tok [REDACTED]/[REDACTED]`, and the raw JSONL had
`"tokens_in": "[REDACTED]"`. The brief requires tokens and cost in traces where available; they were being destroyed.

**Why.** `sanitize._SENSITIVE_KEY` redacts any dict key containing `token` (meant for auth tokens). `tokens_in` /
`tokens_out` contain the substring, so the trace sanitiser wiped them. The unit tests only asserted that secrets were
redacted, never that usage numbers survived, so nothing caught it.

**Fix.** `src/errand/sanitize.py`: `_TOKEN_COUNT_KEY` exempts numeric values under count-style names only
(`tokens_in/out/total/used/count`, `input_tokens`, ...). A string under those names, or any other token-like key
(`token`, `access_token`), is still redacted.

**Regression test.** `tests/test_regression_token_counts.py` (fails without the fix; checks the counts survive, secrets
do not, and `Tracer.close()` summarises the real token totals).

## Observations (not fixed, not counted as bugs)

- `agent_bookings` (KV, written on booking completion) keeps the request's `area` for reschedule searches, and the
  mock's provider ids embed an area slug (`plumber-secretville-0`), so booking history retains an area string after
  `forget`. This is booking history rather than memory, so I left it alone; worth a product decision.
