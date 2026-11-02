"""Optional real-LLM adapter (NOT imported by default, not tested). Requires `pip install anthropic`
and ANTHROPIC_API_KEY. The model only returns an Interpretation JSON; code still enforces everything."""
from __future__ import annotations

import json
import os

from .llm import Interpretation

SYSTEM = ("You convert a user's message about booking/cancelling/rescheduling home services into JSON: "
          '{"intent": one of new_request|update_slots|choose_option|cancel_booking|reschedule_booking|rebook_same|abort|chitchat|unknown, '
          '"slots_update": {service, area, date_range [start,end ISO], earliest "HH:MM", latest "HH:MM", budget int, preferences list}, '
          '"option_index": int|null, "target": {service, booking_id}}. Output JSON only. Never approve or book anything.')


class AnthropicLLM:
    def __init__(self, model: str | None = None):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        import anthropic  # lazy
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ERRAND_MODEL", "claude-sonnet-4-5")

    def interpret(self, user_text: str, state: dict) -> Interpretation:
        msg = self.client.messages.create(
            model=self.model, max_tokens=400, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"state": state, "user_text": user_text})}])
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            return Interpretation.coerce(json.loads(txt[txt.index("{"): txt.rindex("}") + 1]))
        except Exception:
            return Interpretation("unknown")
