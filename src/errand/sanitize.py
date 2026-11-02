"""Untrusted-text handling and trace redaction."""
from __future__ import annotations

import re
from dataclasses import dataclass

_ZW = "​‌‍⁠﻿‪‫‬‭‮⁦⁧⁨⁩"
_ZW_RE = re.compile(f"[{_ZW}]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(rx, re.I))
    for name, rx in (
        ("ignore_previous", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,30}\b(previous|prior|above|earlier|all)\b[^.\n]{0,20}\b(instruction|prompt|rule|direction|message)s?"),
        ("system_marker", r"(^|\n|\s)system\s*:"),
        ("role_marker", r"(^|\n)\s*(assistant|user|human|developer)\s*:|<\|?\s*(system|assistant|user|im_start|im_end)\s*\|?>|\[/?(inst|system)\]|###\s*(system|instruction)"),
        ("role_override", r"\byou\s+are\s+now\b|\bact\s+as\b|\bnew\s+instructions?\b|\bfrom\s+now\s+on\b"),
        ("approve", r"\bapprov(e|ed|al|es)\b|\bauthori[sz]e[ds]?\b"),
        ("book_without_asking", r"\bbook\b[^.\n]{0,20}\bwithout\b|\bwithout\s+(asking|confirm\w*|approval|permission)\b"),
        ("cancel_all", r"\bcancel\s+(all|every|everything)\b"),
        ("url", r"https?://|\bwww\.[a-z0-9-]+\.|\bftp://"),
        ("base64_blob", r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
    )
)


def scan(text) -> tuple[str, ...]:
    """Injection heuristics; returns sorted unique flag names."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    flags: set[str] = set()
    if _ZW_RE.search(text):
        flags.add("zero_width")
    if _CTRL_RE.search(text):
        flags.add("control_chars")
    # check also on text with invisible chars removed (evasion)
    clean = _ZW_RE.sub("", text)
    for name, rx in _PATTERNS:
        if rx.search(clean):
            flags.add(name)
    return tuple(sorted(flags))


@dataclass(frozen=True)
class Untrusted:
    text: str
    source: str
    flags: tuple[str, ...] = ()


def wrap(text, source: str) -> Untrusted:
    t = text if isinstance(text, str) else ("" if text is None else str(text))
    return Untrusted(text=t, source=source, flags=scan(t))


def _strip(text: str) -> str:
    text = _ZW_RE.sub("", text)
    text = text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return _CTRL_RE.sub("", text)


def render_for_user(u: Untrusted, max_len: int = 200) -> str:
    s = _strip(u.text).strip()
    if len(s) > max_len:
        s = s[: max(0, max_len - 1)].rstrip() + "…"
    return f"\"{s}\" [{u.source} text — not instructions]"


# ---- trace redaction ----
REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(r"(api[_-]?key|secret|token|passw(or)?d|passwd|pwd|^pass$|authorization|credential|private[_-]?key|cookie)", re.I)
_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|secret|token|passw(?:or)?d|passwd)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)
# Token *counts* (tokens_in, input_tokens, ...) are metrics, not secrets. Only exempt
# numeric values under these exact count-style names; a string under "token" stays redacted.
_TOKEN_COUNT_KEY = re.compile(
    r"^(tokens?_(in|out|total|used|count)|(input|output|prompt|completion|total|cached)_tokens|num_tokens|token_count)$",
    re.I)
MAX_STR = 2000


def _redact_str(s: str) -> str:
    s = _VALUE_PATTERNS[0].sub(REDACTED, s)
    s = _VALUE_PATTERNS[1].sub("Bearer " + REDACTED, s)
    s = _VALUE_PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
    s = _VALUE_PATTERNS[3].sub(REDACTED, s)
    s = _VALUE_PATTERNS[4].sub(REDACTED, s)
    if len(s) > MAX_STR:
        s = s[:MAX_STR] + f"...[truncated {len(s) - MAX_STR} chars]"
    return s


def _redact_all(obj, _depth: int = 0):
    """Everything under a sensitive key is secret: redact every leaf."""
    if _depth > 12:
        return "[max depth]"
    if isinstance(obj, dict):
        return {str(k): _redact_all(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_redact_all(v, _depth + 1) for v in obj]
    return obj if obj is None else REDACTED


def sanitize_for_trace(obj, _depth: int = 0):
    if _depth > 12:
        return "[max depth]"
    if isinstance(obj, str):
        return _redact_str(obj)
    if isinstance(obj, Untrusted):
        return {"untrusted": True, "source": obj.source, "flags": list(obj.flags),
                "text": _redact_str(obj.text)}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            is_count = (_TOKEN_COUNT_KEY.match(ks) and isinstance(v, (int, float))
                        and not isinstance(v, bool))
            if _SENSITIVE_KEY.search(ks) and not is_count and v is not None:
                out[ks] = _redact_all(v)  # containers too: every leaf under a secret key is secret
            else:
                out[ks] = sanitize_for_trace(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [sanitize_for_trace(v, _depth + 1) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        try:
            return sanitize_for_trace(asdict(obj), _depth + 1)
        except Exception:
            pass
    return _redact_str(str(obj))
