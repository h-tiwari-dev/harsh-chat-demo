# Three real bugs

These are three of the six bugs found and fixed (full text for all six, plus the observations that were not counted, is in `docs/bugs_log.md`). I picked the ones that taught the most about the design: an idempotency key that outlived its booking, a recovery path that trapped the user, and an observability gap that no test had asserted.

## 1. BUG-3: re-booking after a cancel was reported as "already completed"

- **What broke.** Book provider P at slot S for price X, cancel it, then book the same P, S, X and say yes. The agent replied that it was already completed and did nothing, with no live booking.
- **Why.** The idempotency key is a pure function of (user, action, fingerprint), so it stays the same after the booking it produced is cancelled. The executor found the old journal entry in state `BOOKED` for that key and returned `already_done`. A real idempotent provider would also replay the old cancelled booking for that key.
- **Fix.** `executor.current_key()` appends a generation suffix (`:gN`, stored under namespace `keygen`). A successful cancel or reschedule retires the key of the book entry that produced that booking (journal state `SUPERSEDED`), so the next identical request gets a fresh key. `Agent._execute` uses `current_key`. Not covered: a booking cancelled by the provider outside the agent is not noticed at execute time.
- **Regression test.** `tests/test_regression_review.py::test_regression_rebook_same_slot_after_cancel_is_really_booked` (found by an independent review).

## 2. BUG-2: restart after a crash during cancel/reschedule left the user stuck in RECOVERING

- **What broke.** If the process died after the cancel or reschedule intent was journaled (`PENDING`) but before the request reached the provider, every later message got "I re-checked, but still can't confirm the outcome". The user could never cancel or reschedule again, even though the booking was untouched. No false claim was made, so it was safe but unusable.
- **Why.** Cancel and reschedule have no status endpoint, so `_reconcile` checks `list_bookings`. It recognised only the "applied" outcomes; a booking that was visibly unchanged fell through to `unverified`.
- **Fix.** `executor._reconcile`: if the provider's list shows the booking still `booked` (cancel) or still on its original slot (reschedule), finish the journal entry as `failed` with "not applied; your booking is unchanged, please confirm again". Re-proposing reuses the same idempotency key, so a request that lands late cannot apply twice. Applied and unreadable-list paths are unchanged.
- **Regression tests.** `tests/test_evals.py::test_regression_restart_after_unapplied_cancel_or_reschedule_is_not_stuck[cancel]` and `[reschedule]`; eval scenarios `restart_mid_cancel` and `restart_mid_reschedule` (found by the eval suite).

## 3. BUG-6: token counts were redacted in every model-call trace record

- **What broke.** In the demo trace panel each model call showed `tok [REDACTED]/[REDACTED]`, and the JSONL had `"tokens_in": "[REDACTED]"`. Usage numbers were destroyed.
- **Why.** `sanitize._SENSITIVE_KEY` redacts any key containing `token`, meant for auth tokens. `tokens_in` and `tokens_out` match it. The unit tests only asserted that secrets were redacted, never that metrics survived.
- **Fix.** `src/errand/sanitize.py`: `_TOKEN_COUNT_KEY` exempts numeric values under count-style names only (`tokens_in/out/total/used/count`, `input_tokens`, ...). Strings under those names, and keys like `token` or `access_token`, are still redacted.
- **Regression tests.** `tests/test_regression_token_counts.py::test_token_counts_survive_but_secrets_do_not` and `::test_model_call_records_keep_usage_numbers` (found by watching the live trace panel).

Verified by running each test by name; all pass. Three more bugs (BUG-1 forgotten value left in persisted workflow state, BUG-4 secrets nested under sensitive keys not redacted, BUG-5 forgotten area reused by reschedule) are in `docs/bugs_log.md`.
