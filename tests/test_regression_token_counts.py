"""Regression: trace redaction must not wipe token *counts* (bug #3 in docs/bugs_log.md).

The key pattern `token` in sanitize._SENSITIVE_KEY also matched `tokens_in` / `tokens_out`,
so every model_call record logged "[REDACTED]" instead of usage numbers, and the demo trace
panel showed `tok [REDACTED]/[REDACTED]`. Real secrets under token-like keys must stay redacted.
"""
from errand.clock import FakeClock
from errand.sanitize import sanitize_for_trace
from errand.trace import Tracer


def test_token_counts_survive_but_secrets_do_not():
    out = sanitize_for_trace({
        "tokens_in": 120, "tokens_out": 45, "input_tokens": 7, "cost_usd": 0.01,
        "token": "abc123", "access_token": "xyz", "api_key": "sk-secretsecret", "password": 1234,
    })
    assert out["tokens_in"] == 120 and out["tokens_out"] == 45 and out["input_tokens"] == 7
    assert out["token"] == "[REDACTED]" and out["access_token"] == "[REDACTED]"
    assert out["api_key"] == "[REDACTED]" and out["password"] == "[REDACTED]"
    # a *string* under a count-style key is not trusted to be a count
    assert sanitize_for_trace({"tokens_in": "sk-abcdefghij"})["tokens_in"] == "[REDACTED]"


def test_model_call_records_keep_usage_numbers():
    t = Tracer(None, FakeClock())
    t.model_call("m", "p", "r", latency_ms=3, tokens_in=120, tokens_out=45, cost_usd=0.002)
    rec = [e for e in t.events if e["type"] == "model_call"][0]
    assert (rec["tokens_in"], rec["tokens_out"]) == (120, 45)
    summary = t.close()
    assert summary["tokens"] == {"in": 120, "out": 45}
