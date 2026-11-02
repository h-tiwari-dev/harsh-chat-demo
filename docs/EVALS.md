# Evals

```bash
.venv/bin/python -m evals.run            # all scenarios; writes evals/report.md and evals/report.json
.venv/bin/python -m evals.run NAME ...   # only the named scenarios (note: this also rewrites the report with just those)
```

Each scenario in `evals/scenarios/*.py` is scripted user turns, a `ScriptedLLM` or `RuleBasedLLM`, a mock marketplace `Scenario`, and assertions on the provider call log, storage and trace. Every scenario is additionally checked by `evals/invariants.py`: no mutating call without a matching consumed approval, at most one booking per fingerprint, no success claim without verification, no forgotten value anywhere, no secrets in traces, and a non-empty terminal message.

The results table (scenario, expected, actual, pass/fail, notes) is generated from the real run, never hand-written. See [`evals/report.md`](../evals/report.md) (machine-readable: `evals/report.json`).

`tests/test_evals.py` runs every registered scenario under pytest (`test_scenario_passes`) and holds eval-derived regression tests; the table itself is written only by `python -m evals.run`.
