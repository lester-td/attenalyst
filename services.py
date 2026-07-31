from __future__ import annotations

import csv
import html
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dateutil import parser as dtparser
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from app_config import Settings
from database import Database
from models import STATUS_COMING, STATUS_KIV, STATUS_NONE, STATUS_NOT_COMING, EventInput


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    event_id: int
    attempted: int
    delivered: int


def now_local(settings: Settings) -> datetime:
    return datetime.now(tz=settings.timezone)


def parse_local_datetime(value: str, settings: Settings) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.timezone)
    return parsed.astimezone(settings.timezone)


def in_quiet_hours(moment: datetime, settings: Settings) -> bool:
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= moment.hour < end
    return moment.hour >= start or moment.hour < end


def rsvp_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Coming", callback_data=f"rsvp:{event_id}:{STATUS_COMING}"),
                InlineKeyboardButton("Not Coming", callback_data=f"rsvp:{event_id}:{STATUS_NOT_COMING}"),
                InlineKeyboardButton("KIV", callback_data=f"rsvp:{event_id}:{STATUS_KIV}"),
            ]
        ]
    )


def form_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("I've submitted the form", callback_data=f"form:{event_id}")]]
    )


def render_event(event: dict[str, Any], settings: Settings) -> str:
    start_at = dtparser.parse(event["start_at"]).astimezone(settings.timezone)
    end_at = dtparser.parse(event["end_at"]).astimezone(settings.timezone)
    deadline = dtparser.parse(event["rsvp_deadline"]).astimezone(settings.timezone)
    items = event.get("items_to_bring") or "None"
    return (
        f"<b>{html.escape(event['name'])}</b>\n"
        f"{html.escape(settings.group_name)}\n\n"
        f"{html.escape(event['description'])}\n\n"
        f"📅 <b>When:</b> {start_at.strftime('%a, %d %b %Y %I:%M %p')}"
        f" – {end_at.strftime('%I:%M %p')}\n"
        f"📍 <b>Where:</b> {html.escape(event['venue'])}\n"
        f"🎒 <b>Bring:</b> {html.escape(items)}\n"
        f"⏰ <b>Respond by:</b> {deadline.strftime('%a, %d %b %Y %I:%M %p')}\n\n"
        "Please choose your response below. You can change it later."
    )


async def send_event(bot: Bot, telegram_user_id: int, event: dict[str, Any], settings: Settings) -> None:
    await bot.send_message(
        chat_id=telegram_user_id,
        text=render_event(event, settings),
        parse_mode=ParseMode.HTML,
        reply_markup=rsvp_keyboard(int(event["id"])),
        disable_web_page_preview=True,
    )


async def publish_event(
    database: Database,
    bot: Bot,
    event_input: EventInput,
    created_by: int,
    settings: Settings,
) -> PublishResult:
    now = now_local(settings)
    event_id = await database.create_event(event_input, created_by, now)
    event = await database.event_by_id(event_id)
    if event is None:
        raise RuntimeError("Event was created but could not be loaded")

    members = await database.contactable_members()
    delivered = 0
    for member in members:
        try:
            await send_event(bot, int(member["telegram_user_id"]), event, settings)
            delivered += 1
        except Exception:
            log.exception("Could not deliver event %s to member %s", event_id, member["member_id"])
    return PublishResult(event_id, len(members), delivered)


def classify_outstanding(event: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "unreachable": [],
        "no_response": [],
        "kiv": [],
        "missing_form": [],
        "missing_reason": [],
    }
    for row in rows:
        if row["telegram_user_id"] is None or row["bot_started_at"] is None:
            groups["unreachable"].append(row)
        if row["status"] == STATUS_NONE:
            groups["no_response"].append(row)
        elif row["status"] == STATUS_KIV:
            groups["kiv"].append(row)
        elif row["status"] == STATUS_COMING and event["form_required"] and not row["form_acknowledged"]:
            groups["missing_form"].append(row)
        elif (
            row["status"] == STATUS_NOT_COMING
            and event["decline_reason_required"]
            and len(row["reason_text"].strip()) < 3
        ):
            groups["missing_reason"].append(row)
    return groups


def event_csv(event_id: int, rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "event_id",
            "member_id",
            "name",
            "telegram_user_id",
            "telegram_username",
            "status",
            "reason",
            "form_acknowledged",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                event_id,
                row["member_id"],
                row["name"],
                row["telegram_user_id"] or "",
                row["telegram_username"] or "",
                row["status"],
                row["reason_text"],
                int(row["form_acknowledged"]),
            ]
        )
    return output.getvalue().encode("utf-8-sig")


def reminder_kind(row: dict[str, Any]) -> tuple[str, str] | None:
    if row["status"] == STATUS_NONE:
        return "last_rsvp_nudge_at", "response"
    if row["status"] == STATUS_KIV:
        return "last_kiv_nudge_at", "KIV response"
    if row["status"] == STATUS_COMING and row["form_required"] and not row["form_acknowledged"]:
        return "last_form_nudge_at", "form acknowledgement"
    if (
        row["status"] == STATUS_NOT_COMING
        and row["decline_reason_required"]
        and len(row["reason_text"].strip()) < 3
    ):
        return "last_reason_nudge_at", "reason"
    return None


def reminder_is_due(row: dict[str, Any], column: str, now: datetime, settings: Settings) -> bool:
    last_value = row[column] or row["published_at"]
    last = dtparser.parse(last_value).astimezone(settings.timezone)
    return now - last >= timedelta(hours=settings.reminder_interval_hours)
