# Attenalyst

Attenalyst is a reusable Telegram attendance workflow for member groups. It links a private roster identity to a stable Telegram user ID, distributes events, records RSVP choices, collects self-attested form completion and non-attendance reasons, and reminds only members with incomplete flows.

The organizer website and Telegram worker share one SQLite database. This is suitable for a single low-traffic deployment on one VPS.

## What it tracks

- No response
- KIV awaiting a final response
- Coming without registration-form acknowledgement
- Not Coming without a reason
- Roster identities that are unlinked or have not started the bot

Responses belong to the roster `member_id`, not a username. Username changes therefore do not break identity links, and unlinking/relinking an account preserves event history.

## Requirements

- Python 3.11 or newer
- A Telegram bot token from BotFather
- A VPS for production deployment

## Local setup

### macOS and Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` before starting either process. At minimum, replace `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_IDS`, and `WEB_ADMIN_PASSWORD`.

Run the Telegram worker:

```bash
python bot.py
```

Run the organizer website in a second terminal:

```bash
uvicorn web:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. The browser will request the username and password configured in `.env`.

## Roster and identity linking

An administrator sends `/import_members` to the bot and uploads a CSV document:

```csv
member_id,name,telegram_user_id,username
S1234567A,Example Member,,
S7654321B,Prelinked Member,987654321,example_username
```

Only `member_id` and `name` are required.

- Without `telegram_user_id`, the member starts the bot and privately enters their member ID.
- With `telegram_user_id`, `/start` recognizes the member immediately.
- Prelinked members must still press `/start` once because Telegram does not let bots initiate a private conversation.
- `username` is optional display metadata and is refreshed whenever the member interacts.

Treat member IDs as personal data. If they are easy for other people to guess, use preloaded Telegram IDs or disable `ALLOW_SELF_LINK` and let administrators link accounts with `/link_member`.

## Organizer commands

- `/admin` — command summary and website address
- `/import_members` — upload the whitelist
- `/roster` — show linking/contactability status
- `/link_member MEMBER_ID TELEGRAM_ID` — prelink an account
- `/unlink MEMBER_ID` — unlink an account without deleting attendance history
- `/set_active MEMBER_ID yes|no` — activate or deactivate a roster identity
- `/delete_event EVENT_ID` — delete an event after confirmation
- `/restore_event EVENT_ID` — restore a deleted event
- `/outstanding EVENT_ID` — list incomplete flows
- `/export EVENT_ID` — export event responses

Members use `/start`, `/link`, `/whoami`, `/events`, and `/reason`.

## VPS deployment

Run the bot and web server as separate supervised processes. They may share SQLite because they run on the same VPS and Attenalyst enables SQLite WAL mode. Keep the web server at one worker when using SQLite.

Example systemd commands, assuming the repository is at `/opt/attenalyst`:

```ini
# /etc/systemd/system/attenalyst-bot.service
[Unit]
Description=Attenalyst Telegram worker
After=network-online.target

[Service]
WorkingDirectory=/opt/attenalyst
ExecStart=/opt/attenalyst/.venv/bin/python bot.py
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/attenalyst-web.service
[Unit]
Description=Attenalyst organizer website
After=network-online.target

[Service]
WorkingDirectory=/opt/attenalyst
ExecStart=/opt/attenalyst/.venv/bin/uvicorn web:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

Place Caddy or Nginx in front of port 8000 and enable HTTPS. The organizer website uses HTTP Basic authentication, so it must not be exposed over plain HTTP in production.

## Backups and privacy

Back up the configured SQLite database file regularly. It contains member IDs, names, Telegram identifiers, RSVP states, and non-attendance reasons. `.env`, SQLite files, and exported CSV files are excluded by `.gitignore` and must not be committed.
