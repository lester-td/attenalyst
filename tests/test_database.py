from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from database import Database
from models import EventInput, RosterRow, STATUS_COMING
from services import classify_outstanding


TZ = ZoneInfo("Asia/Singapore")


class DatabaseWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "test.sqlite3")
        await self.database.initialize()
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=TZ)
        await self.database.import_roster(
            [
                RosterRow("s1234567a", "Alex Member"),
                RosterRow("S7654321B", "Blair Member", 222, "old_username"),
            ],
            self.now,
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_link_survives_username_change_and_relink(self) -> None:
        linked = await self.database.claim_member(
            " S1234567a ", 111, "first_username", "Alex", self.now
        )
        self.assertEqual(linked.status, "linked")
        self.assertEqual(linked.member["member_id"], "S1234567A")

        updated = await self.database.mark_bot_started(
            111, "new_username", "Alex Updated", self.now + timedelta(minutes=1)
        )
        self.assertEqual(updated["telegram_username"], "new_username")
        self.assertEqual(updated["member_id"], "S1234567A")

        event_id = await self.database.create_event(
            EventInput(
                name="Workshop",
                start_at=self.now + timedelta(days=7),
                end_at=self.now + timedelta(days=7, hours=2),
                rsvp_deadline=self.now + timedelta(days=5),
                venue="Room 1",
                description="A reusable event",
                items_to_bring="Notebook",
                form_required=True,
                form_url="https://example.com/form",
            ),
            created_by=999,
            now=self.now,
        )
        self.assertTrue(await self.database.set_status(event_id, 111, STATUS_COMING, self.now))
        self.assertTrue(await self.database.acknowledge_form(event_id, 111, self.now))

        self.assertTrue(await self.database.unlink_member("S1234567A", self.now))
        relinked = await self.database.claim_member(
            "S1234567A", 333, "replacement", "Alex", self.now + timedelta(hours=1)
        )
        self.assertEqual(relinked.status, "linked")
        rows = await self.database.event_responses(event_id)
        alex = next(row for row in rows if row["member_id"] == "S1234567A")
        self.assertEqual(alex["telegram_user_id"], 333)
        self.assertEqual(alex["status"], STATUS_COMING)
        self.assertEqual(alex["form_acknowledged"], 1)

    async def test_whitelist_rejects_unknown_and_duplicate_claims(self) -> None:
        unknown = await self.database.claim_member("UNKNOWN", 111, None, None, self.now)
        self.assertEqual(unknown.status, "not_found")

        first = await self.database.claim_member("S1234567A", 111, None, None, self.now)
        self.assertEqual(first.status, "linked")
        duplicate_account = await self.database.claim_member("S7654321B", 111, None, None, self.now)
        self.assertEqual(duplicate_account.status, "already_linked")
        claimed_identity = await self.database.claim_member("S7654321B", 444, None, None, self.now)
        self.assertEqual(claimed_identity.status, "claimed")

        self.assertTrue(await self.database.set_member_active("S1234567A", False, self.now))
        self.assertIsNone(await self.database.member_by_telegram_id(111))
        inactive_claim = await self.database.claim_member("S1234567A", 555, None, None, self.now)
        self.assertEqual(inactive_claim.status, "not_found")

    async def test_event_seeds_entire_roster_and_classifies_outstanding(self) -> None:
        event_id = await self.database.create_event(
            EventInput(
                name="Member Night",
                start_at=self.now + timedelta(days=3),
                end_at=self.now + timedelta(days=3, hours=2),
                rsvp_deadline=self.now + timedelta(days=2),
                venue="Studio",
                description="Group activity",
                items_to_bring="None",
                form_required=True,
                form_url="https://example.com/register",
            ),
            created_by=999,
            now=self.now,
        )
        event = await self.database.event_by_id(event_id)
        rows = await self.database.event_responses(event_id)
        self.assertEqual(len(rows), 2)
        groups = classify_outstanding(event, rows)
        self.assertEqual(len(groups["no_response"]), 2)
        self.assertEqual(len(groups["unreachable"]), 2)

        self.assertTrue(await self.database.set_event_active(event_id, False))
        self.assertEqual(await self.database.open_events_for_member("S1234567A", self.now), [])
        self.assertEqual(len(await self.database.event_responses(event_id)), 2)
        self.assertEqual(len(await self.database.recent_events(active=True)), 0)
        self.assertEqual(len(await self.database.recent_events(active=False)), 1)

        self.assertTrue(await self.database.set_event_active(event_id, True))
        self.assertEqual(len(await self.database.open_events_for_member("S1234567A", self.now)), 1)


if __name__ == "__main__":
    unittest.main()
