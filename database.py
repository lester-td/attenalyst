from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

from models import EventInput, ImportResult, LinkResult, RosterRow, STATUS_COMING, STATUS_NOT_COMING


def normalize_member_id(value: str) -> str:
    return "".join((value or "").strip().upper().split())


def _dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    member_id TEXT PRIMARY KEY COLLATE NOCASE,
                    name TEXT NOT NULL,
                    telegram_user_id INTEGER UNIQUE,
                    telegram_username TEXT,
                    telegram_full_name TEXT,
                    bot_started_at TEXT,
                    linked_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    rsvp_deadline TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    description TEXT NOT NULL,
                    items_to_bring TEXT NOT NULL,
                    form_required INTEGER NOT NULL DEFAULT 0,
                    form_url TEXT,
                    decline_reason_required INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER NOT NULL,
                    published_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS responses (
                    event_id INTEGER NOT NULL,
                    member_id TEXT NOT NULL COLLATE NOCASE,
                    status TEXT NOT NULL DEFAULT 'NONE',
                    reason_text TEXT,
                    form_acknowledged INTEGER NOT NULL DEFAULT 0,
                    last_rsvp_nudge_at TEXT,
                    last_kiv_nudge_at TEXT,
                    last_form_nudge_at TEXT,
                    last_reason_nudge_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, member_id),
                    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
                    FOREIGN KEY (member_id) REFERENCES members(member_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_members_telegram_user_id
                    ON members(telegram_user_id);
                CREATE INDEX IF NOT EXISTS idx_events_start_at
                    ON events(start_at);
                CREATE INDEX IF NOT EXISTS idx_responses_status
                    ON responses(event_id, status);
                """
            )
            await db.commit()

    async def member_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            async with db.execute(
                "SELECT * FROM members WHERE telegram_user_id=? AND active=1",
                (int(telegram_user_id),),
            ) as cursor:
                return _dict(await cursor.fetchone())

    async def member_by_id(self, member_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            async with db.execute(
                "SELECT * FROM members WHERE member_id=?",
                (normalize_member_id(member_id),),
            ) as cursor:
                return _dict(await cursor.fetchone())

    async def mark_bot_started(
        self,
        telegram_user_id: int,
        username: str | None,
        full_name: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE members
                SET telegram_username=?, telegram_full_name=?,
                    bot_started_at=COALESCE(bot_started_at, ?), updated_at=?
                WHERE telegram_user_id=? AND active=1
                """,
                (
                    (username or "").lstrip("@").strip() or None,
                    (full_name or "").strip() or None,
                    now.isoformat(),
                    now.isoformat(),
                    int(telegram_user_id),
                ),
            )
            await db.commit()
        return await self.member_by_telegram_id(telegram_user_id)

    async def claim_member(
        self,
        member_id: str,
        telegram_user_id: int,
        username: str | None,
        full_name: str | None,
        now: datetime,
    ) -> LinkResult:
        normalized = normalize_member_id(member_id)
        if not normalized:
            return LinkResult("not_found")

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT * FROM members WHERE telegram_user_id=? AND active=1",
                (int(telegram_user_id),),
            ) as cursor:
                existing_for_user = await cursor.fetchone()
            if existing_for_user is not None:
                await db.rollback()
                return LinkResult("already_linked", _dict(existing_for_user))

            async with db.execute(
                "SELECT * FROM members WHERE member_id=?",
                (normalized,),
            ) as cursor:
                target = await cursor.fetchone()
            if target is None or not int(target["active"]):
                await db.rollback()
                return LinkResult("not_found")
            if target["telegram_user_id"] is not None:
                await db.rollback()
                return LinkResult("claimed", _dict(target))

            await db.execute(
                """
                UPDATE members
                SET telegram_user_id=?, telegram_username=?, telegram_full_name=?,
                    linked_at=?, bot_started_at=?, updated_at=?
                WHERE member_id=?
                """,
                (
                    int(telegram_user_id),
                    (username or "").lstrip("@").strip() or None,
                    (full_name or "").strip() or None,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    normalized,
                ),
            )
            await db.commit()
        return LinkResult("linked", await self.member_by_id(normalized))

    async def import_roster(self, rows: Iterable[RosterRow], now: datetime) -> ImportResult:
        prepared: list[RosterRow] = []
        seen_member_ids: set[str] = set()
        seen_telegram_ids: set[int] = set()
        for row in rows:
            member_id = normalize_member_id(row.member_id)
            name = row.name.strip()
            if not member_id or not name:
                raise ValueError("Every roster row requires member_id and name")
            if member_id in seen_member_ids:
                raise ValueError(f"Duplicate member_id in CSV: {member_id}")
            if row.telegram_user_id is not None:
                if row.telegram_user_id in seen_telegram_ids:
                    raise ValueError(f"Duplicate telegram_user_id in CSV: {row.telegram_user_id}")
                seen_telegram_ids.add(row.telegram_user_id)
            seen_member_ids.add(member_id)
            prepared.append(
                RosterRow(member_id, name, row.telegram_user_id, row.telegram_username.lstrip("@").strip())
            )

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for row in prepared:
                    await db.execute(
                        """
                        INSERT INTO members (
                            member_id, name, telegram_user_id, telegram_username,
                            linked_at, active, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(member_id) DO UPDATE SET
                            name=excluded.name,
                            telegram_user_id=COALESCE(members.telegram_user_id, excluded.telegram_user_id),
                            telegram_username=COALESCE(members.telegram_username, excluded.telegram_username),
                            linked_at=CASE
                                WHEN members.telegram_user_id IS NULL AND excluded.telegram_user_id IS NOT NULL
                                THEN excluded.linked_at ELSE members.linked_at END,
                            active=1,
                            updated_at=excluded.updated_at
                        """,
                        (
                            row.member_id,
                            row.name,
                            row.telegram_user_id,
                            row.telegram_username or None,
                            now.isoformat() if row.telegram_user_id is not None else None,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ValueError("A Telegram user ID is already linked to another member") from exc

        return ImportResult(len(prepared), sum(row.telegram_user_id is not None for row in prepared))

    async def prelink_member(self, member_id: str, telegram_user_id: int, now: datetime) -> str:
        normalized = normalize_member_id(member_id)
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT telegram_user_id FROM members WHERE member_id=? AND active=1",
                (normalized,),
            ) as cursor:
                target = await cursor.fetchone()
            if target is None:
                await db.rollback()
                return "not_found"
            if target["telegram_user_id"] is not None:
                await db.rollback()
                return "already_linked"
            try:
                await db.execute(
                    "UPDATE members SET telegram_user_id=?, linked_at=?, updated_at=? WHERE member_id=?",
                    (int(telegram_user_id), now.isoformat(), now.isoformat(), normalized),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                await db.rollback()
                return "telegram_id_in_use"
        return "linked"

    async def unlink_member(self, member_id: str, now: datetime) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE members SET telegram_user_id=NULL, telegram_username=NULL,
                    telegram_full_name=NULL, bot_started_at=NULL, linked_at=NULL, updated_at=?
                WHERE member_id=? AND active=1
                """,
                (now.isoformat(), normalize_member_id(member_id)),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_member_active(self, member_id: str, active: bool, now: datetime) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "UPDATE members SET active=?, updated_at=? WHERE member_id=?",
                (int(active), now.isoformat(), normalize_member_id(member_id)),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def roster_counts(self) -> dict[str, int]:
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN telegram_user_id IS NULL THEN 1 ELSE 0 END) AS unlinked,
                       SUM(CASE WHEN telegram_user_id IS NOT NULL AND bot_started_at IS NULL THEN 1 ELSE 0 END) AS not_started,
                       SUM(CASE WHEN bot_started_at IS NOT NULL THEN 1 ELSE 0 END) AS contactable
                FROM members WHERE active=1
                """
            ) as cursor:
                row = await cursor.fetchone()
                return {key: int(row[key] or 0) for key in row.keys()}

    async def roster_issues(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT member_id, name, telegram_user_id,
                       CASE WHEN telegram_user_id IS NULL THEN 'UNLINKED' ELSE 'NOT_STARTED' END AS issue
                FROM members
                WHERE active=1 AND (telegram_user_id IS NULL OR bot_started_at IS NULL)
                ORDER BY name
                """
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def create_event(self, event: EventInput, created_by: int, now: datetime) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO events (
                    name, start_at, end_at, rsvp_deadline, venue, description,
                    items_to_bring, form_required, form_url, decline_reason_required,
                    created_by, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.name,
                    event.start_at.isoformat(),
                    event.end_at.isoformat(),
                    event.rsvp_deadline.isoformat(),
                    event.venue,
                    event.description,
                    event.items_to_bring,
                    int(event.form_required),
                    event.form_url or None,
                    int(event.decline_reason_required),
                    int(created_by),
                    now.isoformat(),
                ),
            )
            event_id = int(cursor.lastrowid)
            await db.execute(
                """
                INSERT INTO responses (event_id, member_id, updated_at)
                SELECT ?, member_id, ? FROM members WHERE active=1
                """,
                (event_id, now.isoformat()),
            )
            await db.commit()
            return event_id

    async def event_by_id(self, event_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            async with db.execute("SELECT * FROM events WHERE id=?", (int(event_id),)) as cursor:
                return _dict(await cursor.fetchone())

    async def set_event_active(self, event_id: int, active: bool) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "UPDATE events SET active=? WHERE id=?",
                (int(active), int(event_id)),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def recent_events(
        self,
        limit: int = 20,
        *,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        async with self.connect() as db:
            where = "" if active is None else "WHERE active=?"
            parameters: tuple[Any, ...] = (int(limit),) if active is None else (int(active), int(limit))
            async with db.execute(
                f"SELECT * FROM events {where} ORDER BY start_at DESC LIMIT ?",
                parameters,
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def open_events_for_member(self, member_id: str, now: datetime) -> list[dict[str, Any]]:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO responses (event_id, member_id, updated_at)
                SELECT id, ?, ? FROM events WHERE active=1 AND end_at>?
                ON CONFLICT(event_id, member_id) DO NOTHING
                """,
                (normalize_member_id(member_id), now.isoformat(), now.isoformat()),
            )
            await db.commit()
            async with db.execute(
                """
                SELECT e.*, r.status, r.reason_text, r.form_acknowledged
                FROM events e JOIN responses r ON r.event_id=e.id
                WHERE r.member_id=? AND e.active=1 AND e.end_at>?
                ORDER BY e.start_at
                """,
                (normalize_member_id(member_id), now.isoformat()),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def contactable_members(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT member_id, name, telegram_user_id
                FROM members
                WHERE active=1 AND telegram_user_id IS NOT NULL AND bot_started_at IS NOT NULL
                ORDER BY name
                """
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def set_status(self, event_id: int, telegram_user_id: int, status: str, now: datetime) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE responses
                SET status=?,
                    reason_text=CASE WHEN ?='NOT_COMING' THEN reason_text ELSE NULL END,
                    updated_at=?
                WHERE event_id=? AND member_id=(
                    SELECT member_id FROM members WHERE telegram_user_id=? AND active=1
                )
                """,
                (status, status, now.isoformat(), int(event_id), int(telegram_user_id)),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_reason(self, event_id: int, telegram_user_id: int, reason: str, now: datetime) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE responses SET reason_text=?, updated_at=?
                WHERE event_id=? AND status=? AND member_id=(
                    SELECT member_id FROM members WHERE telegram_user_id=? AND active=1
                )
                """,
                (reason, now.isoformat(), int(event_id), STATUS_NOT_COMING, int(telegram_user_id)),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def acknowledge_form(self, event_id: int, telegram_user_id: int, now: datetime) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                UPDATE responses SET form_acknowledged=1, updated_at=?
                WHERE event_id=? AND status=? AND member_id=(
                    SELECT member_id FROM members WHERE telegram_user_id=? AND active=1
                ) AND EXISTS(SELECT 1 FROM events WHERE id=? AND form_required=1)
                """,
                (now.isoformat(), int(event_id), STATUS_COMING, int(telegram_user_id), int(event_id)),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def event_responses(self, event_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT m.member_id, m.name, m.telegram_user_id, m.telegram_username,
                       m.bot_started_at, r.status, COALESCE(r.reason_text, '') AS reason_text,
                       r.form_acknowledged
                FROM responses r JOIN members m ON m.member_id=r.member_id
                WHERE r.event_id=? AND m.active=1
                ORDER BY m.name
                """,
                (int(event_id),),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def reminder_candidates(self, now: datetime) -> list[dict[str, Any]]:
        async with self.connect() as db:
            async with db.execute(
                """
                SELECT e.id AS event_id, e.name AS event_name, e.start_at, e.rsvp_deadline,
                       e.form_required, e.form_url, e.decline_reason_required, e.published_at,
                       m.member_id, m.telegram_user_id, r.status, COALESCE(r.reason_text, '') AS reason_text,
                       r.form_acknowledged, r.last_rsvp_nudge_at, r.last_kiv_nudge_at,
                       r.last_form_nudge_at, r.last_reason_nudge_at
                FROM responses r
                JOIN events e ON e.id=r.event_id
                JOIN members m ON m.member_id=r.member_id
                WHERE e.active=1 AND e.start_at>? AND m.active=1
                  AND m.telegram_user_id IS NOT NULL AND m.bot_started_at IS NOT NULL
                """,
                (now.isoformat(),),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def mark_reminded(self, event_id: int, member_id: str, column: str, now: datetime) -> None:
        allowed = {
            "last_rsvp_nudge_at",
            "last_kiv_nudge_at",
            "last_form_nudge_at",
            "last_reason_nudge_at",
        }
        if column not in allowed:
            raise ValueError("Unknown reminder column")
        async with self.connect() as db:
            await db.execute(
                f"UPDATE responses SET {column}=? WHERE event_id=? AND member_id=?",
                (now.isoformat(), int(event_id), normalize_member_id(member_id)),
            )
            await db.commit()
