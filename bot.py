"""
PDIG Telegram Bot (Polling MVP) — Admin-gated menu + Event wizard + RSVP + Form Filled tracking + Roster import (name,username,id)

Install:
  pip install python-telegram-bot==21.6 aiosqlite python-dateutil

Run (PowerShell):
  $env:TELEGRAM_BOT_TOKEN="PASTE_TOKEN"
  $env:PDIG_ADMIN_PASSWORD="yourpassword"
  py pdig_bot.py

Roster Import:
  1) /admin <password>
  2) /import_members
  3) Upload CSV as Document with columns (header required):
       name,username,id
     - username without @ (optional ok)
     - id is the Telegram numeric user_id (required)
Notes:
  - Bot still cannot DM users until they /start the bot at least once (Telegram rule).
  - Enrollment: when user runs /start => enrolled=1.
"""

import asyncio
import csv
import io
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime

import aiosqlite
from dateutil import parser as dtparser
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# -------------------- logging --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pdig-bot")

# -------------------- config --------------------
DB_PATH = "pdig_bot.sqlite3"
SG_TZ = timezone(timedelta(hours=8))  # Asia/Singapore

STATUS_NONE = "NONE"
STATUS_COMING = "COMING"
STATUS_NOT_COMING = "NOT_COMING"
STATUS_KIV = "KIV"

SIGNUP_DEADLINE_DAYS_BEFORE = 2
DOORS_OPEN_MINUTES_BEFORE = 30

# Reminders
QUIET_HOURS_START = 22  # 22:00
QUIET_HOURS_END = 9     # 09:00

KIV_NUDGE_EVERY = timedelta(hours=12)
FORM_NUDGE_EVERY = timedelta(hours=12)

# Menu buttons (admin only)
BTN_CREATE = "Create Event"
BTN_NOREPLY = "No Reply"
BTN_EXPORT = "Export CSV"
BTN_HELP = "Help"
BTN_UNENROLLED = "Un-enrolled users"

ADMIN_MENU = ReplyKeyboardMarkup(
    [[BTN_CREATE, BTN_NOREPLY],
     [BTN_EXPORT, BTN_UNENROLLED],
     [BTN_HELP]],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# -------------------- wizard states --------------------
(
    CE_NAME,
    CE_DATE,
    CE_TIME_START,
    CE_TIME_END,
    CE_VENUE,
    CE_DESC,
    CE_ITEMS,
    CE_LINK,
    CE_CONFIRM,
) = range(9)


@dataclass
class DraftEvent:
    name: str = ""
    date_str: str = ""
    start_time_str: str = ""
    end_time_str: str = ""
    venue: str = ""
    description: str = ""
    items_to_bring: str = ""
    signup_url: str = ""


# -------------------- helpers --------------------
def now_sg() -> datetime:
    return datetime.now(tz=SG_TZ)


def in_quiet_hours(dt: datetime) -> bool:
    h = dt.hour
    return (QUIET_HOURS_START <= h) or (h < QUIET_HOURS_END)


def parse_date(date_str: str):
    return dtparser.parse(date_str, dayfirst=False, yearfirst=True).date()


def parse_time_flexible(time_str: str) -> dtime:
    """
    Accepts:
      7pm, 7PM, 7 pm, 7 PM
      1900, 19:00, 19 00
    """
    s = (time_str or "").strip()
    if not s:
        raise ValueError("Empty time")

    s_norm = re.sub(r"\s+", " ", s).strip()

    digits = re.sub(r"[^0-9]", "", s_norm)
    if digits.isdigit() and len(digits) == 4:
        hh = int(digits[:2])
        mm = int(digits[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return dtime(hour=hh, minute=mm)

    t = dtparser.parse(s_norm, fuzzy=True).time()
    return t.replace(second=0, microsecond=0)


def build_dt(date_str: str, time_str: str) -> datetime:
    d = parse_date(date_str)
    t = parse_time_flexible(time_str)
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=SG_TZ)


def fmt_time_ampm(dt: datetime) -> str:
    # "7 PM" or "6.30PM" style
    h = dt.hour
    m = dt.minute
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    if m == 0:
        return f"{h12} {ampm}"
    return f"{h12}.{m:02d}{ampm}"


def fmt_date_long(dt: datetime) -> str:
    # "Monday 9 Feb 2026"
    return dt.strftime("%A") + f" {dt.day} " + dt.strftime("%b %Y")


def a_or_an(name: str) -> str:
    s = (name or "").strip().lower()
    return "an" if (s and s[0] in "aeiou") else "a"


def signup_deadline_date(start_at: datetime) -> datetime.date:
    return (start_at - timedelta(days=SIGNUP_DEADLINE_DAYS_BEFORE)).date()


def event_inline_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Coming", callback_data=f"rsvp:{event_id}:{STATUS_COMING}"),
            InlineKeyboardButton("Not Coming", callback_data=f"rsvp:{event_id}:{STATUS_NOT_COMING}"),
            InlineKeyboardButton("KIV", callback_data=f"rsvp:{event_id}:{STATUS_KIV}"),
        ]]
    )


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Publish", callback_data="ce:confirm"),
            InlineKeyboardButton("Cancel", callback_data="ce:cancel"),
        ]]
    )


def form_filled_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Form Filled", callback_data=f"formfilled:{event_id}")]]
    )


def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_event_message(d: DraftEvent, start_at: datetime, end_at: datetime) -> str:
    doors_open = start_at - timedelta(minutes=DOORS_OPEN_MINUTES_BEFORE)
    deadline = signup_deadline_date(start_at)

    msg = (
        f"Hello members! We will be holding {a_or_an(d.name)} <b>{html_escape(d.name)}</b>.\n\n"
        f"{html_escape(d.description)}\n\n"
        f"📅 <b>Date &amp; Time:</b> {html_escape(fmt_date_long(start_at))}, {html_escape(fmt_time_ampm(start_at))} "
        f"to {html_escape(fmt_time_ampm(end_at))} (doors open {html_escape(fmt_time_ampm(doors_open))})\n"
        f"📍 <b>Location:</b> {html_escape(d.venue)}\n"
        f"🎒 <b>Items to bring:</b> {html_escape(d.items_to_bring)}\n\n"
        f"Sign up by {deadline.day} {deadline.strftime('%b')}!\n"
    )
    return msg


# -------------------- database --------------------
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        # Roster: expected membership (from CSV). Uses Telegram numeric user_id as PRIMARY KEY.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS roster (
                telegram_user_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT NOT NULL,
                created_at TEXT
            )
            """
        )

        # Members: people who have interacted with bot. enrolled=1 after /start.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                telegram_user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_admin INTEGER DEFAULT 0,
                enrolled INTEGER DEFAULT 0,
                enrolled_at TEXT,

                roster_username TEXT,
                roster_name TEXT,

                created_at TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                venue TEXT NOT NULL,
                description TEXT NOT NULL,
                items_to_bring TEXT NOT NULL,
                signup_url TEXT,
                created_by INTEGER,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                event_id INTEGER,
                telegram_user_id INTEGER,
                status TEXT DEFAULT 'NONE',
                reason_text TEXT,

                form_filled INTEGER DEFAULT 0,
                last_form_nudge_at TEXT,

                last_kiv_nudge_at TEXT,
                updated_at TEXT,

                PRIMARY KEY (event_id, telegram_user_id)
            )
            """
        )
        await db.commit()


async def upsert_member(user_id: int, username: str | None, full_name: str | None):
    username_norm = (username or "").lstrip("@").strip() or None

    roster_name = None
    roster_username = None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(username,''), name FROM roster WHERE telegram_user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            if row:
                roster_username = (row[0] or "").strip() or None
                roster_name = (row[1] or "").strip() or None

        await db.execute(
            """
            INSERT INTO members (telegram_user_id, username, full_name, roster_username, roster_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                full_name = COALESCE(excluded.full_name, members.full_name),
                roster_username = COALESCE(excluded.roster_username, members.roster_username),
                roster_name = COALESCE(excluded.roster_name, members.roster_name)
            """,
            (int(user_id), username_norm, full_name, roster_username, roster_name, now_sg().isoformat()),
        )
        await db.commit()


async def set_enrolled(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE members SET enrolled=1, enrolled_at=? WHERE telegram_user_id=?",
            (now_sg().isoformat(), int(user_id)),
        )
        await db.commit()


async def set_admin(user_id: int, is_admin_val: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE members SET is_admin=? WHERE telegram_user_id=?", (is_admin_val, int(user_id)))
        await db.commit()


async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_admin FROM members WHERE telegram_user_id=?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            return bool(row and int(row[0]) == 1)


async def list_enrolled_member_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT telegram_user_id FROM members WHERE enrolled=1") as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]


async def list_unenrolled_roster_rows():
    """
    Returns roster rows where user has NOT enrolled (/start) yet.
    Output: [(name, username), ...]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT r.name, COALESCE(r.username,'')
            FROM roster r
            LEFT JOIN members m ON m.telegram_user_id = r.telegram_user_id
            WHERE m.telegram_user_id IS NULL OR COALESCE(m.enrolled,0)=0
            ORDER BY r.name
            """
        ) as cur:
            return await cur.fetchall()


async def ensure_response_row(event_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO responses (event_id, telegram_user_id, status, updated_at)
            VALUES (?, ?, 'NONE', ?)
            ON CONFLICT(event_id, telegram_user_id) DO NOTHING
            """,
            (int(event_id), int(user_id), now_sg().isoformat()),
        )
        await db.commit()


async def set_status(event_id: int, user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO responses (event_id, telegram_user_id, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, telegram_user_id) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (int(event_id), int(user_id), status, now_sg().isoformat()),
        )
        await db.commit()


async def set_reason(event_id: int, user_id: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE responses
            SET reason_text=?, updated_at=?
            WHERE event_id=? AND telegram_user_id=?
            """,
            (reason, now_sg().isoformat(), int(event_id), int(user_id)),
        )
        await db.commit()


async def mark_form_filled(event_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE responses
            SET form_filled=1, updated_at=?
            WHERE event_id=? AND telegram_user_id=?
            """,
            (now_sg().isoformat(), int(event_id), int(user_id)),
        )
        await db.commit()


async def get_event(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, name, start_at, end_at, venue, description, items_to_bring, signup_url
            FROM events
            WHERE id=?
            """,
            (int(event_id),),
        ) as cur:
            return await cur.fetchone()


async def list_recent_events(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, start_at FROM events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ) as cur:
            return await cur.fetchall()


async def get_event_responses(event_id: int):
    """
    Returns rows:
      name, username, telegram_user_id, status, reason, form_filled
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT
                COALESCE(m.roster_name, m.full_name, ''),
                COALESCE(m.username,''),
                m.telegram_user_id,
                r.status,
                COALESCE(r.reason_text,''),
                COALESCE(r.form_filled,0)
            FROM responses r
            JOIN members m ON m.telegram_user_id = r.telegram_user_id
            WHERE r.event_id=?
            ORDER BY COALESCE(m.roster_name, m.full_name, m.username)
            """,
            (int(event_id),),
        ) as cur:
            return await cur.fetchall()


async def set_last_kiv_nudge(event_id: int, user_id: int, when: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE responses SET last_kiv_nudge_at=? WHERE event_id=? AND telegram_user_id=?",
            (when.isoformat(), int(event_id), int(user_id)),
        )
        await db.commit()


async def set_last_form_nudge(event_id: int, user_id: int, when: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE responses SET last_form_nudge_at=? WHERE event_id=? AND telegram_user_id=?",
            (when.isoformat(), int(event_id), int(user_id)),
        )
        await db.commit()


async def get_kiv_targets(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT telegram_user_id, COALESCE(last_kiv_nudge_at,'')
            FROM responses
            WHERE event_id=? AND status='KIV'
            """,
            (int(event_id),),
        ) as cur:
            return await cur.fetchall()


async def get_form_targets(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT telegram_user_id, COALESCE(last_form_nudge_at,'')
            FROM responses
            WHERE event_id=? AND status='COMING' AND COALESCE(form_filled,0)=0
            """,
            (int(event_id),),
        ) as cur:
            return await cur.fetchall()


async def roster_upsert_many(rows: list[tuple[int, str, str]]) -> int:
    """
    rows: [(telegram_user_id, username, name)]
    """
    async with aiosqlite.connect(DB_PATH) as db:
        count = 0
        for telegram_user_id, username, name in rows:
            try:
                tid = int(telegram_user_id)
            except Exception:
                continue

            u = (username or "").lstrip("@").strip()
            n = (name or "").strip()
            if not n:
                continue

            await db.execute(
                """
                INSERT INTO roster (telegram_user_id, username, name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username=excluded.username,
                    name=excluded.name
                """,
                (tid, u, n, now_sg().isoformat()),
            )
            count += 1
        await db.commit()
        return count


# -------------------- commands --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await upsert_member(u.id, u.username, u.full_name)
    await set_enrolled(u.id)

    # Try to greet using roster_name if available
    display_name = None
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(roster_name,''), COALESCE(full_name,''), COALESCE(username,'') FROM members WHERE telegram_user_id=?",
            (int(u.id),),
        ) as cur:
            row = await cur.fetchone()

    if row:
        roster_name, full_name, username = row
        roster_name = (roster_name or "").strip()
        full_name = (full_name or "").strip()
        username = (username or "").strip()

        if roster_name:
            display_name = roster_name
        elif full_name:
            display_name = full_name
        elif username:
            display_name = f"@{username}"

    if not display_name:
        display_name = "there"

    msg = (
        f"Welcome, {html_escape(display_name)}. You are now registered to receive notifications for Production IG events.\n\n"
        "Please message Lester (@sc85k) if you run into issues. This bot is under development."
    )

    if await is_admin(u.id):
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=ADMIN_MENU)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await upsert_member(u.id, u.username, u.full_name)

    pw = os.getenv("PDIG_ADMIN_PASSWORD", "")
    if not pw:
        await update.message.reply_text("Admin password not set on this bot.")
        return

    if not context.args or context.args[0] != pw:
        await update.message.reply_text("Wrong password.")
        return

    await set_admin(u.id, 1)
    await update.message.reply_text("Admin access granted.", reply_markup=ADMIN_MENU)


async def cmd_import_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    context.user_data["awaiting_roster_csv"] = True
    await update.message.reply_text(
        "Upload your roster CSV as a Document.\n\nRequired columns:\n- name\n- username (without @)\n- id (Telegram numeric user_id)",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("draft_event", None)
    context.user_data.pop("awaiting_reason_for_event", None)
    context.user_data.pop("awaiting_roster_csv", None)
    await update.message.reply_text(
        "Cancelled.",
        reply_markup=ADMIN_MENU if await is_admin(update.effective_user.id) else ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# -------------------- roster upload handler --------------------
async def handle_roster_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_roster_csv"):
        return

    if not await is_admin(update.effective_user.id):
        context.user_data.pop("awaiting_roster_csv", None)
        await update.message.reply_text("Admin only.")
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please upload the CSV as a Document.")
        return

    try:
        f = await context.bot.get_file(doc.file_id)
        b = await f.download_as_bytearray()
        text = b.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))

        # Expect: name,username,id
        batch: list[tuple[int, str, str]] = []
        for row in reader:
            name = (row.get("name") or "").strip()
            username = (row.get("username") or "").strip()
            tid_raw = (row.get("id") or "").strip()
            if not tid_raw:
                continue
            try:
                tid = int(tid_raw)
            except Exception:
                continue
            batch.append((tid, username, name))

        imported = await roster_upsert_many(batch)
        context.user_data.pop("awaiting_roster_csv", None)

        await update.message.reply_text(
            f"Roster imported/updated: {imported} rows.\n"
            "Members still must /start once to enroll (Telegram DM rule).",
            reply_markup=ADMIN_MENU,
        )
    except Exception as e:
        log.exception("Roster import failed")
        await update.message.reply_text(f"Failed to import CSV: {e}\nTry again or /cancel.")


# -------------------- menu router (admin-only) --------------------
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text not in {BTN_CREATE, BTN_NOREPLY, BTN_EXPORT, BTN_HELP, BTN_UNENROLLED}:
        return

    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    if text == BTN_HELP:
        await update.message.reply_text(
            "Admin menu:\n"
            "- Create Event: publish event + DM enrolled members\n"
            "- No Reply: list defaulters\n"
            "- Export CSV: export responses\n"
            "- Un-enrolled users: roster members who haven't /start\n"
            "- Import roster: /import_members then upload CSV\n\n"
            "Member flow:\n"
            "- Coming: you will receive signup link + Form Filled button\n"
            "- Not Coming: you must provide valid reason\n"
            "- KIV: bot will nudge until you update",
            reply_markup=ADMIN_MENU,
        )
        return

    if text == BTN_UNENROLLED:
        rows = await list_unenrolled_roster_rows()
        if not rows:
            await update.message.reply_text("All roster users have enrolled (/start).", reply_markup=ADMIN_MENU)
            return

        lines = []
        for name, username in rows:
            handle = f"@{username}" if (username or "").strip() else "(no username)"
            lines.append(f"{name} — {handle}")

        # Telegram message size limit safety
        msg = "Un-enrolled users:\n" + "\n".join(lines[:200])
        if len(lines) > 200:
            msg += f"\n\n(+{len(lines)-200} more not shown)"

        await update.message.reply_text(msg, reply_markup=ADMIN_MENU)
        return

    if text == BTN_CREATE:
        context.user_data["draft_event"] = DraftEvent()
        await update.message.reply_text("<b>Create Event Wizard</b> — 1/8\nEnter event name", parse_mode=ParseMode.HTML)
        return CE_NAME

    if text == BTN_NOREPLY:
        await show_event_picker(update, context, purpose="noreply")
        return

    if text == BTN_EXPORT:
        await show_event_picker(update, context, purpose="export")
        return


# -------------------- event picker for noreply/export --------------------
async def show_event_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, purpose: str):
    rows = await list_recent_events(limit=10)
    if not rows:
        await update.message.reply_text("No events yet.", reply_markup=ADMIN_MENU)
        return

    buttons = []
    for event_id, name, start_at_s in rows:
        start_at = dtparser.parse(start_at_s)
        label = f"{event_id} — {name} ({start_at.strftime('%d %b %Y')})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{purpose}:{event_id}")])

    await update.message.reply_text("Pick an event:", reply_markup=InlineKeyboardMarkup(buttons))


async def pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_admin(q.from_user.id):
        await q.message.reply_text("Admin only.")
        return

    data = q.data or ""
    m = re.match(r"^pick:(noreply|export):(\d+)$", data)
    if not m:
        return

    purpose = m.group(1)
    event_id = int(m.group(2))

    if purpose == "noreply":
        await show_noreply(q.message, event_id)
    else:
        await export_csv(q.message, event_id)


async def show_noreply(message, event_id: int):
    responses = await get_event_responses(event_id)

    none_list = []
    kiv_list = []
    not_coming_no_reason = []
    coming_no_form = []

    for name, username, uid, status, reason, form_filled in responses:
        handle = f"@{username}" if username else f"(uid:{uid})"
        label = f"{name} — {handle}" if name else handle

        if status == STATUS_NONE:
            none_list.append(label)
        elif status == STATUS_KIV:
            kiv_list.append(label)
        elif status == STATUS_NOT_COMING and len(reason.strip()) < 3:
            not_coming_no_reason.append(label)
        elif status == STATUS_COMING and int(form_filled or 0) == 0:
            coming_no_form.append(label)

    lines = [f"No Reply — Event {event_id}"]
    if none_list:
        lines.append("\nNo response:\n" + "\n".join(none_list))
    if kiv_list:
        lines.append("\nKIV (needs update):\n" + "\n".join(kiv_list))
    if coming_no_form:
        lines.append("\nComing but Form Filled not pressed:\n" + "\n".join(coming_no_form))
    if not_coming_no_reason:
        lines.append("\nNot Coming but no reason:\n" + "\n".join(not_coming_no_reason))

    if not (none_list or kiv_list or not_coming_no_reason or coming_no_form):
        lines.append("\nNo defaulters right now.")

    await message.reply_text("\n".join(lines), reply_markup=ADMIN_MENU)


async def export_csv(message, event_id: int):
    responses = await get_event_responses(event_id)
    header = "event_id,name,telegram_user_id,username,status,reason,form_filled\n"
    lines = [header]
    for name, username, uid, status, reason, form_filled in responses:
        n = (name or "").replace('"', '""')
        u = (username or "").replace('"', '""')
        r = (reason or "").replace('"', '""')
        lines.append(f'{event_id},"{n}",{uid},"{u}",{status},"{r}",{int(form_filled or 0)}\n')

    csv_bytes = "".join(lines).encode("utf-8")
    await message.reply_document(
        document=csv_bytes,
        filename=f"event_{event_id}_responses.csv",
        caption=f"Export for event {event_id}",
    )


# -------------------- create event wizard steps --------------------
async def ce_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    d.name = (update.message.text or "").strip()
    if len(d.name) < 3:
        await update.message.reply_text("<b>Create Event Wizard</b> — 1/8\nEnter event name", parse_mode=ParseMode.HTML)
        return CE_NAME

    await update.message.reply_text("<b>Create Event Wizard</b> — 2/8\nEnter event date (YYYY-MM-DD)", parse_mode=ParseMode.HTML)
    return CE_DATE


async def ce_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    d.date_str = (update.message.text or "").strip()
    try:
        _ = parse_date(d.date_str)
    except Exception:
        await update.message.reply_text("<b>Create Event Wizard</b> — 2/8\nEnter event date (YYYY-MM-DD)", parse_mode=ParseMode.HTML)
        return CE_DATE

    await update.message.reply_text("<b>Create Event Wizard</b> — 3/8\nStart time", parse_mode=ParseMode.HTML)
    return CE_TIME_START


async def ce_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    d.start_time_str = (update.message.text or "").strip()
    try:
        _ = parse_time_flexible(d.start_time_str)
    except Exception:
        await update.message.reply_text("<b>Create Event Wizard</b> — 3/8\nStart time", parse_mode=ParseMode.HTML)
        return CE_TIME_START

    await update.message.reply_text("<b>Create Event Wizard</b> — 4/8\nEnd time", parse_mode=ParseMode.HTML)
    return CE_TIME_END


async def ce_time_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    d.end_time_str = (update.message.text or "").strip()
    try:
        start_at = build_dt(d.date_str, d.start_time_str)
        end_at = build_dt(d.date_str, d.end_time_str)
        if end_at <= start_at:
            await update.message.reply_text("<b>Create Event Wizard</b> — 4/8\nEnd time", parse_mode=ParseMode.HTML)
            return CE_TIME_END
    except Exception:
        await update.message.reply_text("<b>Create Event Wizard</b> — 4/8\nEnd time", parse_mode=ParseMode.HTML)
        return CE_TIME_END

    await update.message.reply_text("<b>Create Event Wizard</b> — 5/8\nVenue", parse_mode=ParseMode.HTML)
    return CE_VENUE


async def ce_venue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    d.venue = (update.message.text or "").strip()
    if len(d.venue) < 2:
        await update.message.reply_text("<b>Create Event Wizard</b> — 5/8\nVenue", parse_mode=ParseMode.HTML)
        return CE_VENUE

    await update.message.reply_text("<b>Create Event Wizard</b> — 6/8\nDescription", parse_mode=ParseMode.HTML)
    return CE_DESC


async def ce_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    d.description = (update.message.text or "").strip()
    if len(d.description) < 3:
        await update.message.reply_text("<b>Create Event Wizard</b> — 6/8\nDescription", parse_mode=ParseMode.HTML)
        return CE_DESC

    await update.message.reply_text("<b>Create Event Wizard</b> — 7/8\nItems to bring (type - for None)", parse_mode=ParseMode.HTML)
    return CE_ITEMS


async def ce_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    v = (update.message.text or "").strip()
    if not v:
        await update.message.reply_text("<b>Create Event Wizard</b> — 7/8\nItems to bring (type - for None)", parse_mode=ParseMode.HTML)
        return CE_ITEMS
    d.items_to_bring = "None" if v == "-" else v

    await update.message.reply_text("<b>Create Event Wizard</b> — 8/8\nSignup link (type - if none yet)", parse_mode=ParseMode.HTML)
    return CE_LINK


async def ce_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d: DraftEvent = context.user_data["draft_event"]
    v = (update.message.text or "").strip()
    d.signup_url = "" if v == "-" else v

    start_at = build_dt(d.date_str, d.start_time_str)
    end_at = build_dt(d.date_str, d.end_time_str)

    preview = render_event_message(d, start_at, end_at)
    if d.signup_url:
        preview += f"\n\nSignup link:\n{html_escape(d.signup_url)}\n"

    await update.message.reply_text(
        preview + "\n\nPublish this event?",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_kb(),
        disable_web_page_preview=True,
    )
    return CE_CONFIRM


async def ce_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_admin(q.from_user.id):
        await q.message.reply_text("Admin only.")
        return ConversationHandler.END

    if q.data == "ce:cancel":
        context.user_data.pop("draft_event", None)
        await q.message.reply_text("Cancelled.", reply_markup=ADMIN_MENU)
        return ConversationHandler.END

    if q.data != "ce:confirm":
        return CE_CONFIRM

    d: DraftEvent = context.user_data.get("draft_event")
    if not d:
        await q.message.reply_text("Draft missing. Please try again.", reply_markup=ADMIN_MENU)
        return ConversationHandler.END

    start_at = build_dt(d.date_str, d.start_time_str)
    end_at = build_dt(d.date_str, d.end_time_str)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO events (name, start_at, end_at, venue, description, items_to_bring, signup_url, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                d.name,
                start_at.isoformat(),
                end_at.isoformat(),
                d.venue,
                d.description,
                d.items_to_bring,
                d.signup_url,
                q.from_user.id,
                now_sg().isoformat(),
            ),
        )
        await db.commit()
        event_id = cur.lastrowid

    member_ids = await list_enrolled_member_ids()
    msg = render_event_message(d, start_at, end_at)
    if d.signup_url:
        msg += f"\n\nSignup link:\n{html_escape(d.signup_url)}\n"

    for uid in member_ids:
        await ensure_response_row(event_id, uid)
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=event_inline_kb(event_id),
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("DM failed to %s: %s", uid, e)

    context.user_data.pop("draft_event", None)
    await q.message.reply_text(
        f'Published event "{d.name}".\nEvent ID: {event_id}\nNotified members.',
        reply_markup=ADMIN_MENU,
    )
    return ConversationHandler.END


# -------------------- RSVP callbacks + reason capture + form filled --------------------
async def rsvp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    u = q.from_user
    await upsert_member(u.id, u.username, u.full_name)

    data = q.data or ""
    m = re.match(r"^rsvp:(\d+):(NONE|COMING|NOT_COMING|KIV)$", data)
    if not m:
        return

    event_id = int(m.group(1))
    status = m.group(2)

    ev = await get_event(event_id)
    if not ev:
        await q.message.reply_text("Event not found.")
        return

    _, name, _, _, _, _, _, signup_url = ev

    await ensure_response_row(event_id, u.id)
    await set_status(event_id, u.id, status)

    if status == STATUS_COMING:
        if signup_url:
            await q.message.reply_text(
                f"You selected Coming.\n\nSignup link:\n{signup_url}",
                reply_markup=form_filled_kb(event_id),
                disable_web_page_preview=True,
            )
        else:
            await q.message.reply_text(
                "You selected Coming.\n\nSignup link: (not provided yet)",
                reply_markup=form_filled_kb(event_id),
            )
        return

    if status == STATUS_NOT_COMING:
        context.user_data["awaiting_reason_for_event"] = event_id
        await q.message.reply_text(
            f"You selected Not Coming for {html_escape(name)}.\n<br>\nPlease type your valid reason in ONE message.",
            parse_mode=ParseMode.HTML,
        )
        return

    if status == STATUS_KIV:
        await q.message.reply_text(
            "You selected KIV.\n\nPlease update later by tapping Coming or Not Coming.\nI will remind you until you update."
        )
        return


async def formfilled_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    u = q.from_user
    await upsert_member(u.id, u.username, u.full_name)

    data = q.data or ""
    m = re.match(r"^formfilled:(\d+)$", data)
    if not m:
        return

    event_id = int(m.group(1))
    await ensure_response_row(event_id, u.id)
    await mark_form_filled(event_id, u.id)
    await q.message.reply_text("Recorded: Form Filled. Thanks.")


async def handle_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event_id = context.user_data.get("awaiting_reason_for_event")
    if not event_id:
        return

    reason = (update.message.text or "").strip()
    if len(reason) < 3:
        await update.message.reply_text("Reason too short. Please type a valid reason in ONE message.")
        return

    await set_reason(int(event_id), update.effective_user.id, reason)
    context.user_data.pop("awaiting_reason_for_event", None)
    await update.message.reply_text("Reason recorded. Thanks.")


# -------------------- reminders: KIV + Coming-but-not-FormFilled --------------------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now = now_sg()
    if in_quiet_hours(now):
        return

    events = await list_recent_events(limit=50)
    for event_id, _name, _ in events:
        ev = await get_event(int(event_id))
        if not ev:
            continue
        _, ev_name, _, _, _, _, _, signup_url = ev

        # KIV nudges
        kiv_targets = await get_kiv_targets(int(event_id))
        for uid, last_s in kiv_targets:
            last = dtparser.parse(last_s) if last_s else None
            if last and (now - last) < KIV_NUDGE_EVERY:
                continue
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=f'Reminder: your RSVP is still KIV for "{ev_name}". Please update.',
                    reply_markup=event_inline_kb(int(event_id)),
                )
                await set_last_kiv_nudge(int(event_id), int(uid), now)
            except Exception as e:
                log.warning("KIV nudge failed to %s: %s", uid, e)

        # Coming but not Form Filled nudges
        form_targets = await get_form_targets(int(event_id))
        for uid, last_s in form_targets:
            last = dtparser.parse(last_s) if last_s else None
            if last and (now - last) < FORM_NUDGE_EVERY:
                continue

            try:
                text = f'Reminder: you selected Coming for "{ev_name}", but you have not pressed Form Filled.'
                if signup_url:
                    text += f"\n\nSignup link:\n{signup_url}"
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=text,
                    reply_markup=form_filled_kb(int(event_id)),
                    disable_web_page_preview=True,
                )
                await set_last_form_nudge(int(event_id), int(uid), now)
            except Exception as e:
                log.warning("Form nudge failed to %s: %s", uid, e)


# -------------------- main --------------------
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable.")

    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db_init())

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("import_members", cmd_import_members))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Roster upload
    app.add_handler(MessageHandler(filters.Document.ALL, handle_roster_upload))

    # Wizard (starts from Create Event button)
    create_event_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(rf"^{re.escape(BTN_CREATE)}$"), menu_router)],
        states={
            CE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_name)],
            CE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_date)],
            CE_TIME_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_time_start)],
            CE_TIME_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_time_end)],
            CE_VENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_venue)],
            CE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_desc)],
            CE_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_items)],
            CE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ce_link)],
            CE_CONFIRM: [CallbackQueryHandler(ce_confirm, pattern=r"^ce:(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(create_event_conv, group=0)

    # Admin menu router (other buttons)
    menu_pattern = rf"^({re.escape(BTN_CREATE)}|{re.escape(BTN_NOREPLY)}|{re.escape(BTN_EXPORT)}|{re.escape(BTN_HELP)}|{re.escape(BTN_UNENROLLED)})$"
    app.add_handler(MessageHandler(filters.Regex(menu_pattern), menu_router), group=1)

    # Event pickers + RSVP + formfilled
    app.add_handler(CallbackQueryHandler(pick_callback, pattern=r"^pick:(noreply|export):\d+$"))
    app.add_handler(CallbackQueryHandler(rsvp_callback, pattern=r"^rsvp:\d+:(NONE|COMING|NOT_COMING|KIV)$"))
    app.add_handler(CallbackQueryHandler(formfilled_callback, pattern=r"^formfilled:\d+$"))

    # Reason capture
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reason))

    # Reminder job
    app.job_queue.run_repeating(reminder_job, interval=600, first=20)

    log.info("Starting bot with polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
