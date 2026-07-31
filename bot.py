from __future__ import annotations

import csv
import html
import io
import logging
import re

from dateutil import parser as dtparser
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app_config import Settings
from database import Database
from models import RosterRow, STATUS_COMING, STATUS_KIV, STATUS_NONE, STATUS_NOT_COMING
from services import (
    classify_outstanding,
    event_csv,
    form_keyboard,
    in_quiet_hours,
    now_local,
    reminder_is_due,
    reminder_kind,
    render_event,
    rsvp_keyboard,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("attenalyst.bot")

SETTINGS = Settings.from_env()
DATABASE = Database(SETTINGS.database_path)


def is_bootstrap_admin(telegram_user_id: int) -> bool:
    return int(telegram_user_id) in SETTINGS.admin_telegram_ids


async def require_private(update: Update) -> bool:
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Please use this command in a private chat with me.")
    return False


async def require_admin(update: Update) -> bool:
    if update.effective_user and is_bootstrap_admin(update.effective_user.id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Administrator access is required.")
    return False


async def linked_member(update: Update, *, notify: bool = True) -> dict | None:
    user = update.effective_user
    if user is None:
        return None
    member = await DATABASE.mark_bot_started(
        user.id,
        user.username,
        user.full_name,
        now_local(SETTINGS),
    )
    if member is None and notify and update.effective_message:
        await update.effective_message.reply_text(
            f"Your Telegram account is not linked yet. Use /link and enter your {SETTINGS.member_id_label}."
        )
    return member


async def complete_link(update: Update, member_id: str) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return False
    if not SETTINGS.allow_self_link:
        await message.reply_text(
            f"Self-linking is disabled. Please contact {SETTINGS.support_contact}.\n"
            f"Your Telegram user ID is: {user.id}"
        )
        return False

    result = await DATABASE.claim_member(
        member_id,
        user.id,
        user.username,
        user.full_name,
        now_local(SETTINGS),
    )
    if result.status == "not_found":
        await message.reply_text(
            f"That {SETTINGS.member_id_label} is not on the active roster. Check it and try again, "
            f"or contact {SETTINGS.support_contact}."
        )
        return False
    if result.status == "claimed":
        await message.reply_text(
            f"That identity is already linked to another Telegram account. Contact {SETTINGS.support_contact} "
            "to have it unlinked first."
        )
        return False
    if result.status == "already_linked":
        await message.reply_text(
            f"This Telegram account is already linked to {result.member['name']}. Use /whoami to check it."
        )
        return True

    member = result.member
    await DATABASE.open_events_for_member(member["member_id"], now_local(SETTINGS))
    await message.reply_text(
        f"Linked successfully. Welcome, {html.escape(member['name'])}!\n\n"
        "Your link uses your numeric Telegram account ID, so changing your username will not break it. "
        "Use /events to view current events.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    user = update.effective_user
    message = update.effective_message
    member = await linked_member(update, notify=False)
    if member is not None:
        await DATABASE.open_events_for_member(member["member_id"], now_local(SETTINGS))
        admin_note = "\n\nYou are a configured administrator. Use /admin for organizer commands." if is_bootstrap_admin(user.id) else ""
        await message.reply_text(
            f"Welcome back, {html.escape(member['name'])}. You are linked to {html.escape(SETTINGS.group_name)}."
            f"{admin_note}\n\nUse /events to view current events.",
            parse_mode=ParseMode.HTML,
        )
        return

    if context.args:
        if await complete_link(update, context.args[0]):
            return

    context.user_data["awaiting_member_id"] = True
    admin_note = "\nYou are already authorized for organizer commands." if is_bootstrap_admin(user.id) else ""
    await message.reply_text(
        f"Welcome to Attenalyst for {SETTINGS.group_name}.\n\n"
        f"Please enter your {SETTINGS.member_id_label} to link this Telegram account."
        f"{admin_note}\n\nYour Telegram user ID is {user.id}."
    )


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    member = await linked_member(update, notify=False)
    if member is not None:
        await update.effective_message.reply_text(
            f"You are already linked to {member['name']} ({member['member_id']})."
        )
        return
    if context.args:
        await complete_link(update, context.args[0])
        return
    context.user_data["awaiting_member_id"] = True
    await update.effective_message.reply_text(f"Enter your {SETTINGS.member_id_label} in one message.")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    member = await linked_member(update, notify=False)
    user = update.effective_user
    if member is None:
        await update.effective_message.reply_text(
            f"This Telegram account is not linked. Your Telegram user ID is {user.id}. Use /link to continue."
        )
        return
    await update.effective_message.reply_text(
        f"Linked identity: {member['name']}\n"
        f"{SETTINGS.member_id_label.title()}: {member['member_id']}\n"
        f"Telegram user ID: {user.id}"
    )


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    member = await linked_member(update)
    if member is None:
        return
    events = await DATABASE.open_events_for_member(member["member_id"], now_local(SETTINGS))
    if not events:
        await update.effective_message.reply_text("There are no current events.")
        return
    for event in events:
        status = event["status"].replace("_", " ").title()
        await update.effective_message.reply_text(
            render_event(event, SETTINGS) + f"\n\n<b>Your current response:</b> {html.escape(status)}",
            parse_mode=ParseMode.HTML,
            reply_markup=rsvp_keyboard(int(event["id"])),
            disable_web_page_preview=True,
        )


async def cmd_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    if await linked_member(update) is None:
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /reason EVENT_ID your reason")
        return
    event_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip()
    if len(reason) < 3:
        await update.effective_message.reply_text("Please provide a meaningful reason.")
        return
    saved = await DATABASE.set_reason(event_id, update.effective_user.id, reason, now_local(SETTINGS))
    await update.effective_message.reply_text(
        "Reason recorded. Thank you." if saved else "Select Not Coming for that event before submitting a reason."
    )


async def rsvp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await linked_member(update) is None:
        return
    match = re.fullmatch(r"rsvp:(\d+):(NONE|COMING|NOT_COMING|KIV)", query.data or "")
    if match is None:
        return
    event_id = int(match.group(1))
    status = match.group(2)
    event = await DATABASE.event_by_id(event_id)
    if event is None or not event["active"]:
        await query.message.reply_text("This event is no longer available.")
        return
    if dtparser.parse(event["end_at"]) <= now_local(SETTINGS):
        await query.message.reply_text("This event has ended.")
        return
    if not await DATABASE.set_status(event_id, query.from_user.id, status, now_local(SETTINGS)):
        await query.message.reply_text("I could not record that response. Use /events and try again.")
        return

    if status == STATUS_COMING:
        if event["form_required"]:
            text = "Recorded: Coming. Please submit the registration form, then acknowledge it below."
            if event["form_url"]:
                text += f"\n\nForm: {event['form_url']}"
            await query.message.reply_text(
                text,
                reply_markup=form_keyboard(event_id),
                disable_web_page_preview=True,
            )
        else:
            await query.message.reply_text("Recorded: Coming. You have completed the required flow.")
    elif status == STATUS_NOT_COMING:
        if event["decline_reason_required"]:
            context.user_data["awaiting_reason_event_id"] = event_id
            await query.message.reply_text(
                "Recorded: Not Coming. Please send your reason in one message, or use "
                f"/reason {event_id} your reason"
            )
        else:
            await query.message.reply_text("Recorded: Not Coming.")
    elif status == STATUS_KIV:
        await query.message.reply_text(
            "Recorded: KIV. I will remind you to change this to Coming or Not Coming before the deadline."
        )


async def form_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await linked_member(update) is None:
        return
    match = re.fullmatch(r"form:(\d+)", query.data or "")
    if match is None:
        return
    saved = await DATABASE.acknowledge_form(
        int(match.group(1)), query.from_user.id, now_local(SETTINGS)
    )
    await query.message.reply_text(
        "Form submission acknowledged. Your event flow is complete."
        if saved
        else "Select Coming before acknowledging the registration form."
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    await update.effective_message.reply_text(
        "Organizer commands:\n"
        "/import_members — upload or update the whitelist CSV\n"
        "/roster — show linking status\n"
        "/link_member MEMBER_ID TELEGRAM_ID — prelink an account\n"
        "/unlink MEMBER_ID — allow an identity to be relinked\n"
        "/set_active MEMBER_ID yes|no — activate or deactivate a roster identity\n"
        "/delete_event EVENT_ID — delete an event after confirmation\n"
        "/restore_event EVENT_ID — restore a deleted event\n"
        "/outstanding EVENT_ID — show incomplete flows\n"
        "/export EVENT_ID — export responses\n\n"
        f"Create events at: {SETTINGS.web_base_url}"
    )


async def cmd_import_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    context.user_data["awaiting_roster_csv"] = True
    await update.effective_message.reply_text(
        "Upload a CSV document with these columns:\n"
        "member_id,name,telegram_user_id,username\n\n"
        "Only member_id and name are required. telegram_user_id enables instant recognition; otherwise the "
        f"member links privately using their {SETTINGS.member_id_label}."
    )


async def roster_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_roster_csv"):
        return
    if not await require_private(update) or not await require_admin(update):
        context.user_data.pop("awaiting_roster_csv", None)
        return
    document = update.effective_message.document
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        content = bytes(await telegram_file.download_as_bytearray()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        headers = {header.strip() for header in (reader.fieldnames or []) if header}
        if not {"member_id", "name"}.issubset(headers):
            raise ValueError("CSV must contain member_id and name columns")
        rows: list[RosterRow] = []
        for number, row in enumerate(reader, start=2):
            member_id = (row.get("member_id") or "").strip()
            name = (row.get("name") or "").strip()
            raw_telegram_id = (row.get("telegram_user_id") or "").strip()
            if not member_id and not name and not raw_telegram_id:
                continue
            if not member_id or not name:
                raise ValueError(f"Row {number} requires member_id and name")
            telegram_id = None
            if raw_telegram_id:
                try:
                    telegram_id = int(raw_telegram_id)
                except ValueError as exc:
                    raise ValueError(f"Row {number} has an invalid telegram_user_id") from exc
            rows.append(
                RosterRow(
                    member_id=member_id,
                    name=name,
                    telegram_user_id=telegram_id,
                    telegram_username=(row.get("username") or "").strip(),
                )
            )
        result = await DATABASE.import_roster(rows, now_local(SETTINGS))
        context.user_data.pop("awaiting_roster_csv", None)
        await update.effective_message.reply_text(
            f"Roster updated: {result.imported} members, {result.prelinked} rows supplied with Telegram IDs.\n"
            "Prelinked members must still press /start once before the bot can message them."
        )
    except (UnicodeDecodeError, ValueError) as exc:
        await update.effective_message.reply_text(f"Roster import rejected: {exc}")
    except Exception:
        log.exception("Roster import failed")
        await update.effective_message.reply_text("Roster import failed unexpectedly. Check the server logs.")


async def cmd_roster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    counts = await DATABASE.roster_counts()
    issues = await DATABASE.roster_issues()
    lines = [
        f"Active roster: {counts['total']}",
        f"Contactable: {counts['contactable']}",
        f"Linked but /start not received: {counts['not_started']}",
        f"Unlinked: {counts['unlinked']}",
    ]
    if issues:
        lines.append("\nNeeds attention:")
        for row in issues[:100]:
            lines.append(f"{row['name']} ({row['member_id']}) — {row['issue'].replace('_', ' ').title()}")
        if len(issues) > 100:
            lines.append(f"…and {len(issues) - 100} more")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_link_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /link_member MEMBER_ID TELEGRAM_ID")
        return
    try:
        telegram_id = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("TELEGRAM_ID must be numeric.")
        return
    status = await DATABASE.prelink_member(context.args[0], telegram_id, now_local(SETTINGS))
    messages = {
        "linked": "Member prelinked. They must press /start once before receiving messages.",
        "not_found": "That member ID is not on the active roster.",
        "already_linked": "That member is already linked. Use /unlink first to replace the account.",
        "telegram_id_in_use": "That Telegram ID is already linked to another member.",
    }
    await update.effective_message.reply_text(messages[status])


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /unlink MEMBER_ID")
        return
    removed = await DATABASE.unlink_member(context.args[0], now_local(SETTINGS))
    await update.effective_message.reply_text(
        "Telegram account unlinked. Event history remains attached to the member identity."
        if removed
        else "That member ID is not on the active roster."
    )


async def cmd_set_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    if len(context.args) != 2 or context.args[1].lower() not in {"yes", "no"}:
        await update.effective_message.reply_text("Usage: /set_active MEMBER_ID yes|no")
        return
    active = context.args[1].lower() == "yes"
    changed = await DATABASE.set_member_active(context.args[0], active, now_local(SETTINGS))
    if not changed:
        await update.effective_message.reply_text("That member ID is not in the roster.")
        return
    await update.effective_message.reply_text(
        "Member activated and eligible for future events."
        if active
        else "Member deactivated. Existing history is retained and reminders are stopped."
    )


def _event_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if len(context.args) == 1 and context.args[0].isdigit():
        return int(context.args[0])
    return None


async def cmd_outstanding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    event_id = _event_id_arg(context)
    if event_id is None:
        await update.effective_message.reply_text("Usage: /outstanding EVENT_ID")
        return
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        await update.effective_message.reply_text("Event not found.")
        return
    groups = classify_outstanding(event, await DATABASE.event_responses(event_id))
    labels = {
        "unreachable": "Unlinked or not started",
        "no_response": "No response",
        "kiv": "KIV",
        "missing_form": "Coming, form not acknowledged",
        "missing_reason": "Not Coming, reason missing",
    }
    lines = [f"Outstanding — {event['name']} (#{event_id})"]
    for key, label in labels.items():
        rows = groups[key]
        if rows:
            lines.append(f"\n{label} ({len(rows)}):")
            lines.extend(f"• {row['name']} ({row['member_id']})" for row in rows[:80])
            if len(rows) > 80:
                lines.append(f"…and {len(rows) - 80} more")
    if len(lines) == 1:
        lines.append("\nEveryone has completed the required flow.")
    text = "\n".join(lines)
    for offset in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[offset : offset + 4000])


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    event_id = _event_id_arg(context)
    if event_id is None:
        await update.effective_message.reply_text("Usage: /export EVENT_ID")
        return
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        await update.effective_message.reply_text("Event not found.")
        return
    data = event_csv(event_id, await DATABASE.event_responses(event_id))
    await update.effective_message.reply_document(
        document=InputFile(io.BytesIO(data), filename=f"event_{event_id}_responses.csv"),
        caption=f"Response export for {event['name']}",
    )


async def cmd_delete_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    event_id = _event_id_arg(context)
    if event_id is None:
        await update.effective_message.reply_text("Usage: /delete_event EVENT_ID")
        return
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        await update.effective_message.reply_text("Event not found.")
        return
    if not event["active"]:
        await update.effective_message.reply_text("That event is already deleted.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Delete event", callback_data=f"event_delete:{event_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"event_delete_cancel:{event_id}"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        f"Delete “{event['name']}” (event #{event_id})?\n\n"
        "Reminders will stop and members will no longer see it. Responses will be retained for export or restoration.",
        reply_markup=keyboard,
    )


async def delete_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_bootstrap_admin(query.from_user.id):
        await query.message.reply_text("Administrator access is required.")
        return
    match = re.fullmatch(r"event_delete(_cancel)?:(\d+)", query.data or "")
    if match is None:
        return
    await query.edit_message_reply_markup(reply_markup=None)
    if match.group(1):
        await query.message.reply_text("Event deletion cancelled.")
        return
    event_id = int(match.group(2))
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        await query.message.reply_text("Event not found.")
        return
    await DATABASE.set_event_active(event_id, False)
    await query.message.reply_text(
        f"Deleted “{event['name']}”. Reminders are stopped and responses are retained.\n"
        f"Restore it with /restore_event {event_id}."
    )


async def cmd_restore_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update):
        return
    event_id = _event_id_arg(context)
    if event_id is None:
        await update.effective_message.reply_text("Usage: /restore_event EVENT_ID")
        return
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        await update.effective_message.reply_text("Event not found.")
        return
    if event["active"]:
        await update.effective_message.reply_text("That event is already active.")
        return
    await DATABASE.set_event_active(event_id, True)
    await update.effective_message.reply_text(
        f"Restored “{event['name']}”. Existing responses remain in place and reminders may resume."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if context.user_data.get("awaiting_member_id"):
        if await complete_link(update, text):
            context.user_data.pop("awaiting_member_id", None)
        return
    event_id = context.user_data.get("awaiting_reason_event_id")
    if event_id is not None:
        if len(text) < 3:
            await update.effective_message.reply_text("Please provide a meaningful reason.")
            return
        saved = await DATABASE.set_reason(
            int(event_id), update.effective_user.id, text, now_local(SETTINGS)
        )
        if saved:
            context.user_data.pop("awaiting_reason_event_id", None)
            await update.effective_message.reply_text("Reason recorded. Your event flow is complete.")
        else:
            await update.effective_message.reply_text("I could not record that reason. Use /events and try again.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_member_id", None)
    context.user_data.pop("awaiting_reason_event_id", None)
    context.user_data.pop("awaiting_roster_csv", None)
    await update.effective_message.reply_text("Cancelled.")


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_local(SETTINGS)
    if in_quiet_hours(now, SETTINGS):
        return
    for row in await DATABASE.reminder_candidates(now):
        reminder = reminder_kind(row)
        if reminder is None:
            continue
        column, kind = reminder
        if not reminder_is_due(row, column, now, SETTINGS):
            continue
        event_id = int(row["event_id"])
        deadline = dtparser.parse(row["rsvp_deadline"]).astimezone(SETTINGS.timezone)
        overdue = " The response deadline has passed." if now > deadline else ""
        markup: InlineKeyboardMarkup | None = None
        if kind == "response":
            text = f"Reminder: please respond to “{row['event_name']}”.{overdue}"
            markup = rsvp_keyboard(event_id)
        elif kind == "KIV response":
            text = f"Reminder: your response to “{row['event_name']}” is still KIV. Please choose a final answer.{overdue}"
            markup = rsvp_keyboard(event_id)
        elif kind == "form acknowledgement":
            text = f"Reminder: please submit the form for “{row['event_name']}”, then acknowledge it below."
            if row["form_url"]:
                text += f"\n\nForm: {row['form_url']}"
            markup = form_keyboard(event_id)
        else:
            text = (
                f"Reminder: please provide your reason for not attending “{row['event_name']}”.\n"
                f"Use /reason {event_id} your reason"
            )
        try:
            await context.bot.send_message(
                chat_id=int(row["telegram_user_id"]),
                text=text,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            await DATABASE.mark_reminded(event_id, row["member_id"], column, now)
        except Exception:
            log.exception("Reminder delivery failed for event %s member %s", event_id, row["member_id"])


async def post_init(application: Application) -> None:
    await DATABASE.initialize()
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Link or refresh your membership"),
            BotCommand("events", "View current events"),
            BotCommand("whoami", "Check your linked identity"),
            BotCommand("link", "Link this Telegram account"),
            BotCommand("reason", "Submit a non-attendance reason"),
            BotCommand("admin", "Show organizer commands"),
            BotCommand("cancel", "Cancel the current action"),
        ]
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled Telegram update error", exc_info=context.error)


def build_application(settings: Settings = SETTINGS) -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and configure it.")
    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("link", cmd_link))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("events", cmd_events))
    application.add_handler(CommandHandler("reason", cmd_reason))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("import_members", cmd_import_members))
    application.add_handler(CommandHandler("roster", cmd_roster))
    application.add_handler(CommandHandler("link_member", cmd_link_member))
    application.add_handler(CommandHandler("unlink", cmd_unlink))
    application.add_handler(CommandHandler("set_active", cmd_set_active))
    application.add_handler(CommandHandler("outstanding", cmd_outstanding))
    application.add_handler(CommandHandler("export", cmd_export))
    application.add_handler(CommandHandler("delete_event", cmd_delete_event))
    application.add_handler(CommandHandler("restore_event", cmd_restore_event))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CallbackQueryHandler(rsvp_callback, pattern=r"^rsvp:\d+:(NONE|COMING|NOT_COMING|KIV)$"))
    application.add_handler(CallbackQueryHandler(form_callback, pattern=r"^form:\d+$"))
    application.add_handler(
        CallbackQueryHandler(
            delete_event_callback,
            pattern=r"^event_delete(_cancel)?:\d+$",
        )
    )
    application.add_handler(MessageHandler(filters.Document.ALL, roster_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    if application.job_queue is None:
        raise RuntimeError("Install python-telegram-bot with the job-queue extra")
    application.job_queue.run_repeating(
        reminder_job,
        interval=settings.reminder_check_seconds,
        first=20,
        name="outstanding-reminders",
    )
    return application


def main() -> None:
    application = build_application()
    log.info("Starting Attenalyst for %s with Telegram polling", SETTINGS.group_name)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
