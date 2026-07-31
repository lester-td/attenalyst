from __future__ import annotations

import io
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from telegram import Bot

from app_config import BASE_DIR, Settings
from database import Database
from models import EventInput
from services import classify_outstanding, event_csv, now_local, parse_local_datetime, publish_event


SETTINGS = Settings.from_env()
DATABASE = Database(SETTINGS.database_path)
TEMPLATES = Jinja2Templates(directory=BASE_DIR / "templates")
SECURITY = HTTPBasic(auto_error=False)


def require_web_admin(credentials: HTTPBasicCredentials | None = Depends(SECURITY)) -> str:
    valid = (
        credentials is not None
        and bool(SETTINGS.web_admin_password)
        and secrets.compare_digest(credentials.username, SETTINGS.web_admin_username)
        and secrets.compare_digest(credentials.password, SETTINGS.web_admin_password)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organizer login required",
            headers={"WWW-Authenticate": 'Basic realm="Attenalyst"'},
        )
    return credentials.username


def csrf_token() -> str:
    return hmac.new(
        SETTINGS.web_admin_password.encode("utf-8"),
        b"attenalyst:create-event",
        hashlib.sha256,
    ).hexdigest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SETTINGS.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")
    if not SETTINGS.web_admin_password:
        raise RuntimeError("WEB_ADMIN_PASSWORD is missing from .env")
    await DATABASE.initialize()
    yield


app = FastAPI(title="Attenalyst", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "Attenalyst"}


@app.get("/")
async def dashboard(request: Request, _: str = Depends(require_web_admin)):
    events = await DATABASE.recent_events(active=True)
    archived_events = await DATABASE.recent_events(limit=10, active=False)
    roster = await DATABASE.roster_counts()
    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "settings": SETTINGS,
            "events": events,
            "archived_events": archived_events,
            "roster": roster,
        },
    )


@app.get("/events/new")
async def new_event(request: Request, _: str = Depends(require_web_admin)):
    now = now_local(SETTINGS).replace(second=0, microsecond=0)
    start = now + timedelta(days=7)
    defaults = {
        "start_at": start.strftime("%Y-%m-%dT%H:%M"),
        "end_at": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "rsvp_deadline": (start - timedelta(hours=SETTINGS.default_rsvp_deadline_hours)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
        "decline_reason_required": True,
    }
    return TEMPLATES.TemplateResponse(
        request=request,
        name="event_form.html",
        context={"settings": SETTINGS, "values": defaults, "error": None, "csrf_token": csrf_token()},
    )


@app.post("/events")
async def create_event(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    start_at: str = Form(...),
    end_at: str = Form(...),
    rsvp_deadline: str = Form(...),
    venue: str = Form(...),
    description: str = Form(...),
    items_to_bring: str = Form(""),
    form_required: str | None = Form(None),
    form_url: str = Form(""),
    decline_reason_required: str | None = Form(None),
    _: str = Depends(require_web_admin),
):
    values = {
        "name": name,
        "start_at": start_at,
        "end_at": end_at,
        "rsvp_deadline": rsvp_deadline,
        "venue": venue,
        "description": description,
        "items_to_bring": items_to_bring,
        "form_required": form_required is not None,
        "form_url": form_url,
        "decline_reason_required": decline_reason_required is not None,
    }
    try:
        if not secrets.compare_digest(csrf, csrf_token()):
            raise HTTPException(status_code=403, detail="Invalid form token")
        parsed_start = parse_local_datetime(start_at, SETTINGS)
        parsed_end = parse_local_datetime(end_at, SETTINGS)
        parsed_deadline = parse_local_datetime(rsvp_deadline, SETTINGS)
        if len(name.strip()) < 3:
            raise ValueError("Event name must be at least three characters")
        if len(venue.strip()) < 2:
            raise ValueError("Venue is required")
        if len(description.strip()) < 3:
            raise ValueError("Description must be at least three characters")
        if parsed_end <= parsed_start:
            raise ValueError("End time must be after the start time")
        if parsed_deadline >= parsed_start:
            raise ValueError("RSVP deadline must be before the event starts")
        requires_form = form_required is not None
        if requires_form:
            parsed_url = urlparse(form_url.strip())
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("A valid http(s) form URL is required when form acknowledgement is enabled")

        event_input = EventInput(
            name=name.strip(),
            start_at=parsed_start,
            end_at=parsed_end,
            rsvp_deadline=parsed_deadline,
            venue=venue.strip(),
            description=description.strip(),
            items_to_bring=items_to_bring.strip() or "None",
            form_required=requires_form,
            form_url=form_url.strip() if requires_form else "",
            decline_reason_required=decline_reason_required is not None,
        )
        async with Bot(SETTINGS.telegram_bot_token) as bot:
            result = await publish_event(DATABASE, bot, event_input, 0, SETTINGS)
        return RedirectResponse(
            f"/events/{result.event_id}?delivered={result.delivered}&attempted={result.attempted}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="event_form.html",
            context={
                "settings": SETTINGS,
                "values": values,
                "error": str(exc),
                "csrf_token": csrf_token(),
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


@app.get("/events/{event_id}")
async def event_detail(
    request: Request,
    event_id: int,
    delivered: int | None = None,
    attempted: int | None = None,
    _: str = Depends(require_web_admin),
):
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    responses = await DATABASE.event_responses(event_id)
    groups = classify_outstanding(event, responses)
    complete = len(responses) - len(
        {
            row["member_id"]
            for rows in groups.values()
            for row in rows
            if row["member_id"]
        }
    )
    return TEMPLATES.TemplateResponse(
        request=request,
        name="event_detail.html",
        context={
            "settings": SETTINGS,
            "event": event,
            "responses": responses,
            "groups": groups,
            "complete": complete,
            "delivered": delivered,
            "attempted": attempted,
            "csrf_token": csrf_token(),
        },
    )


@app.post("/events/{event_id}/delete")
async def delete_event(
    event_id: int,
    csrf: str = Form(...),
    _: str = Depends(require_web_admin),
):
    if not secrets.compare_digest(csrf, csrf_token()):
        raise HTTPException(status_code=403, detail="Invalid form token")
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await DATABASE.set_event_active(event_id, False)
    return RedirectResponse(f"/events/{event_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/events/{event_id}/restore")
async def restore_event(
    event_id: int,
    csrf: str = Form(...),
    _: str = Depends(require_web_admin),
):
    if not secrets.compare_digest(csrf, csrf_token()):
        raise HTTPException(status_code=403, detail="Invalid form token")
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await DATABASE.set_event_active(event_id, True)
    return RedirectResponse(f"/events/{event_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/events/{event_id}/export")
async def export_event(event_id: int, _: str = Depends(require_web_admin)):
    event = await DATABASE.event_by_id(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    data = event_csv(event_id, await DATABASE.event_responses(event_id))
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="event_{event_id}_responses.csv"'},
    )
