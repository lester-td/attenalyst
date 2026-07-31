from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


STATUS_NONE = "NONE"
STATUS_COMING = "COMING"
STATUS_NOT_COMING = "NOT_COMING"
STATUS_KIV = "KIV"
VALID_STATUSES = {STATUS_NONE, STATUS_COMING, STATUS_NOT_COMING, STATUS_KIV}


@dataclass(frozen=True)
class EventInput:
    name: str
    start_at: datetime
    end_at: datetime
    rsvp_deadline: datetime
    venue: str
    description: str
    items_to_bring: str
    form_required: bool
    form_url: str
    decline_reason_required: bool = True


@dataclass(frozen=True)
class RosterRow:
    member_id: str
    name: str
    telegram_user_id: int | None = None
    telegram_username: str = ""


@dataclass(frozen=True)
class ImportResult:
    imported: int
    prelinked: int


@dataclass(frozen=True)
class LinkResult:
    status: str
    member: dict | None = None
