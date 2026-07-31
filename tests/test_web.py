from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import web
from database import Database
from models import EventInput


class WebSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        web.SETTINGS = replace(
            web.SETTINGS,
            telegram_bot_token="123456:test-token",
            web_admin_username="organizer",
            web_admin_password="test-password",
            database_path=Path(self.tempdir.name) / "web.sqlite3",
        )
        web.DATABASE = Database(web.SETTINGS.database_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_health_and_authenticated_pages(self) -> None:
        with TestClient(web.app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["service"], "Attenalyst")

            unauthorized = client.get("/")
            self.assertEqual(unauthorized.status_code, 401)

            auth = ("organizer", "test-password")
            dashboard = client.get("/", auth=auth)
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("Attenalyst", dashboard.text)

            event_form = client.get("/events/new", auth=auth)
            self.assertEqual(event_form.status_code, 200)
            self.assertIn("Publish and notify", event_form.text)
            self.assertIn('name="csrf"', event_form.text)

            rejected = client.post(
                "/events",
                auth=auth,
                data={
                    "csrf": "invalid",
                    "name": "Test Event",
                    "start_at": "2026-08-01T10:00",
                    "end_at": "2026-08-01T12:00",
                    "rsvp_deadline": "2026-07-30T10:00",
                    "venue": "Room 1",
                    "description": "Test event description",
                },
            )
            self.assertEqual(rejected.status_code, 403)

            now = datetime(2026, 7, 24, 12, 0, tzinfo=ZoneInfo("Asia/Singapore"))
            event_id = asyncio.run(
                web.DATABASE.create_event(
                    EventInput(
                        name="Deletable Event",
                        start_at=now + timedelta(days=5),
                        end_at=now + timedelta(days=5, hours=2),
                        rsvp_deadline=now + timedelta(days=3),
                        venue="Studio",
                        description="Delete route test",
                        items_to_bring="None",
                        form_required=False,
                        form_url="",
                    ),
                    created_by=1,
                    now=now,
                )
            )
            deleted = client.post(
                f"/events/{event_id}/delete",
                auth=auth,
                data={"csrf": web.csrf_token()},
                follow_redirects=False,
            )
            self.assertEqual(deleted.status_code, 303)
            self.assertEqual(asyncio.run(web.DATABASE.event_by_id(event_id))["active"], 0)

            restored = client.post(
                f"/events/{event_id}/restore",
                auth=auth,
                data={"csrf": web.csrf_token()},
                follow_redirects=False,
            )
            self.assertEqual(restored.status_code, 303)
            self.assertEqual(asyncio.run(web.DATABASE.event_by_id(event_id))["active"], 1)


if __name__ == "__main__":
    unittest.main()
