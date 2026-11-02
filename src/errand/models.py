"""Shared data models per docs/CONTRACT.md. No deviations (ToolError also carries an optional `message`)."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    service: str
    area: str
    price: int
    rating: float
    free_slots: tuple[str, ...]
    attributes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Review:
    id: str
    author: str
    text: str  # UNTRUSTED


@dataclass
class Booking:
    id: str
    user_id: str
    provider_id: str
    service: str
    slot: str
    price: int
    status: str  # booked|cancelled|completed


class ToolError(Exception):
    """codes: slot_unavailable, slot_taken, hold_expired, timeout, not_found, slot_conflict, service_unavailable"""
    def __init__(self, code: str, message: str = "", transient: bool = False):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.transient = transient


class ToolTimeout(ToolError):
    def __init__(self, message: str = "request timed out"):
        super().__init__("timeout", message, transient=True)
