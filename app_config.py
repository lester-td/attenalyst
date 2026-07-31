from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return frozenset()
    try:
        return frozenset(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a comma-separated list of Telegram user IDs") from exc


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    group_name: str
    timezone_name: str
    timezone: ZoneInfo
    database_path: Path
    admin_telegram_ids: frozenset[int]
    member_id_label: str
    support_contact: str
    allow_self_link: bool
    quiet_hours_start: int
    quiet_hours_end: int
    reminder_interval_hours: int
    reminder_check_seconds: int
    default_rsvp_deadline_hours: int
    web_admin_username: str
    web_admin_password: str
    web_base_url: str

    @classmethod
    def from_env(cls, *, require_secrets: bool = False) -> "Settings":
        timezone_name = os.getenv("GROUP_TIMEZONE", "Asia/Singapore").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Unknown GROUP_TIMEZONE: {timezone_name}") from exc

        raw_db_path = Path(os.getenv("DATABASE_PATH", "attenalyst.sqlite3").strip())
        database_path = raw_db_path if raw_db_path.is_absolute() else BASE_DIR / raw_db_path
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        web_password = os.getenv("WEB_ADMIN_PASSWORD", "").strip()
        if require_secrets and not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")
        if require_secrets and not web_password:
            raise RuntimeError("WEB_ADMIN_PASSWORD is missing from .env")

        quiet_start = _env_int("QUIET_HOURS_START", 22)
        quiet_end = _env_int("QUIET_HOURS_END", 9)
        if not 0 <= quiet_start <= 23 or not 0 <= quiet_end <= 23:
            raise RuntimeError("Quiet hours must be between 0 and 23")

        return cls(
            telegram_bot_token=token,
            group_name=os.getenv("GROUP_NAME", "My Member Group").strip() or "My Member Group",
            timezone_name=timezone_name,
            timezone=timezone,
            database_path=database_path,
            admin_telegram_ids=_env_ids("ADMIN_TELEGRAM_IDS"),
            member_id_label=os.getenv("MEMBER_ID_LABEL", "member ID").strip() or "member ID",
            support_contact=os.getenv("SUPPORT_CONTACT", "a group administrator").strip(),
            allow_self_link=_env_bool("ALLOW_SELF_LINK", True),
            quiet_hours_start=quiet_start,
            quiet_hours_end=quiet_end,
            reminder_interval_hours=max(1, _env_int("REMINDER_INTERVAL_HOURS", 12)),
            reminder_check_seconds=max(60, _env_int("REMINDER_CHECK_SECONDS", 600)),
            default_rsvp_deadline_hours=max(1, _env_int("DEFAULT_RSVP_DEADLINE_HOURS", 48)),
            web_admin_username=os.getenv("WEB_ADMIN_USERNAME", "admin").strip() or "admin",
            web_admin_password=web_password,
            web_base_url=os.getenv("WEB_BASE_URL", "http://localhost:8000").strip().rstrip("/"),
        )
