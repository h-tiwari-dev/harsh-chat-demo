# Errand agent

A chat agent that books, cancels and reschedules home-service appointments (plumber, electrician, AC servicing) against a mock marketplace. Python 3.11+, standard library only at runtime; `pytest` for tests.

The design rule: the LLM only turns text into a structured `Interpretation`. Approval, tool calls, idempotency, limits and recovery are ordinary code.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                      # unit + regression tests
.venv/bin/python -m evals.run         # 32 scenarios -> evals/report.md, evals/report.json
```

## CLI

Offline: mock marketplace plus the regex-based `RuleBasedLLM`. No API key or network.

```bash
.venv/bin/python -m errand.cli --user u1 --scenario timeout
```

Options: `--scenario {default,happy,timeout,slot_taken,price_drift,no_injection}`, `--db PATH` (a file keeps memory, workflow state, approvals and journal across runs and resumes via `Agent.resume`; default is in-memory), `--trace-file PATH` (default `traces/<user>-<time>-<pid>.jsonl`), `--script FILE` (one message per line, non-interactive), `--real-clock` (default is a fake clock starting 2026-11-02). Type `quit` to exit.

Example: `Book an electrician in Indiranagar this week`, then `1`, then `yes`.

## Demo viewer

```bash
.venv/bin/python -m demo.server       # http://127.0.0.1:8765
```

Chat on the left, live colour-coded JSONL trace on the right, scenario dropdown, and a "Restart agent process" button. See `demo/README.md` for the 4-step walkthrough. Headless replay: `.venv/bin/python -m demo.replay script.txt --scenario timeout`. To regenerate the sample trace: `.venv/bin/python -m demo.make_trace`.

## Layout

```
src/errand/   agent.py (orchestrator) state.py approval.py executor.py sanitize.py limits.py
              trace.py memory.py llm.py llm_anthropic.py storage.py clock.py models.py cli.py
              mocks/provider.py (mock marketplace with scripted failures)
tests/        unit tests per module, test_invariants.py, test_regression_*.py, test_followup.py
evals/        scenarios/*.py, invariants.py (shared checker), run.py, report.md, report.json
demo/         server.py, session.py, replay.py, make_trace.py, index.html, out/
docs/         CONTRACT.md (spec), DESIGN.md, BUGS.md, EVALS.md, bugs_log.md, trace_timeout_after_success.jsonl
```

## Assumptions and how ambiguities were read

These come from `docs/CONTRACT.md`; each was checked against the code.

- Money is integer rupees. Slots are one-hour, timezone-naive ISO strings such as `2026-11-04T09:00`. "Now" comes from an injected `Clock`.
- Approval is a stored record that only `ApprovalStore.grant_from_user_message` creates. It needs the user's whole raw message to be an affirmative (`yes`, `y`, `yep`, `yeah`, `confirm`, `ok`, `okay`, `go ahead`, `book it`; case-insensitive, trailing `.`/`!` allowed), a pending proposal, and an unchanged fingerprint. It is checked before the LLM is called.
- The fingerprint is sha256 over `{action, user_id, provider_id, slot, price, booking_id, new_slot}`. A different provider, slot or price needs a new approval. Approvals are single use.
- Idempotency key is `user:action:fingerprint[:16]`, plus a `:gN` suffix after a booking made under that key is cancelled or rescheduled (so re-booking the same thing is a new request). The intent is journaled as `PENDING` before the first tool call.
- A `book` timeout always triggers `get_booking_status(key)`: `booked` is a verified success, `failed` is reported, `not_found` retries with the same key up to 3 times with backoff, then reports "unverified" and enters `RECOVERING`. Success is never claimed without verification.
- A price at hold that differs from the approved price voids the approval and re-asks. An expired hold is re-held once; a changed price then also forces re-approval.
- Tool text (reviews, provider names) is wrapped as `Untrusted`, scanned for injection patterns, shown to the user only as quoted data, and never passed to the LLM. The LLM sees only a state snapshot, and its intent must be in a fixed allow-list or code ignores it.
- Limits: 12 user turns, 30 model calls, 40 tool calls, 60 s wall clock (injected clock). Exhaustion gives the terminal phase `LIMIT_EXHAUSTED`; an uncaught exception gives `FAILED`. The CLI overrides wall clock to 3600 s. The CLI and demo use a fake clock, so wall-clock only advances through backoff sleeps and fake time; it cannot be hit by slow typing there.
- Memory keys are `area`, `earliest_time`, `preferred_technician`. Inferred facts are stored unconfirmed and only used after the user says yes. `forget` tombstones and wipes the value.
- Persistence is SQLite only (one `kv` table). The contract's "or JSON dir" alternative was not built.
- The mock marketplace is the only provider. The tool-call failure modes (timeout where the booking did or did not land, 503 on first cancel, slot taken, hold expiry, price drift, reschedule conflict, injection in reviews) are scripted through `Scenario`.

Small differences between the contract and the code:
- The repeated-"yes" reply reads "That is already done: ... (reference BK-...)", not the contract's literal "already booked: BK-..".
- `Tracer` writes each event to the JSONL file as it happens; `close()` only returns the summary.
- `phrase()` on the LLM interface is optional; the agent's replies come from templates.

## Optional live LLM

`src/errand/llm_anthropic.py` has `AnthropicLLM`, which implements `interpret()` with the Anthropic SDK. It needs `pip install anthropic` and `ANTHROPIC_API_KEY` (it raises if the key is missing; model from `ERRAND_MODEL`, default `claude-sonnet-4-5`). It is not imported anywhere by default, not wired into the CLI or demo, and not tested, live or otherwise; use it by passing `llm=AnthropicLLM()` to `Agent`. It also does not report token counts to the tracer, so traces from a live model would still show 0 tokens. Everything in the tests and evals uses `RuleBasedLLM`/`ScriptedLLM`, so token and cost fields in the bundled traces are 0.

## Nice-to-have chosen: proactive follow-up

`Agent.check_followups()` polls `list_bookings`. If the provider cancelled a booking (`status == "provider_cancelled"`), it tells the user once (state kept in the `followup` namespace), searches for a replacement at a similar or earlier time using the user's saved budget, window and preferences, and presents options. It books nothing: the user still picks an option and says `yes` through the normal proposal, approval and executor path. The CLI calls it at startup and after each turn. Tests: `tests/test_followup.py`. Limits: detection happens only when polled, and only the mock (`provider_cancels()`) can produce a provider cancellation, so the CLI and demo scenarios cannot trigger it.
