# Termin

Lightweight project management web service with calendar-first collaboration.

## Goals
- Assign tasks without requiring accounts for collaborators.
- Support Google Calendar and Microsoft 365 with real-time sync.
- Provide optional multi-user logins for students/teams.

## Architecture Outline
- Flask API with SQLAlchemy + Alembic.
- OAuth for Google + Microsoft accounts.
- Magic-link invites for non-account collaborators.
- Calendar sync via:
  - Google Calendar push notifications (watch channels).
  - Microsoft Graph subscriptions.

## Local Setup
1. Install dependencies for your system Python.
2. Set env vars in `.env` (see below).
3. Run `python3 app.py`.

The app will check the database on startup and apply migrations if needed.

## Env Vars
- `SECRET_KEY`
- `DATABASE_URL` (default: sqlite at `instance/app.db`)
- `PUBLIC_BASE_URL`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `MS_CLIENT_ID`, `MS_CLIENT_SECRET`
- `GOOGLE_WEBHOOK_SECRET`, `MS_WEBHOOK_SECRET`

## OAuth Redirect URIs
- Google: `http://localhost:5000/auth/google/callback`
- Microsoft: `http://localhost:5000/auth/microsoft/callback`

## Render Deploy
This repo includes [render.yaml](./render.yaml) for a first hosted test deploy on Render.

Current Render shape:
- Python web service
- Gunicorn web server
- Persistent disk mounted at `instance/`
- SQLite stored at `instance/app.db`
- Startup checks/migrations run before Gunicorn starts

Recommended first deploy flow:
1. In Render, create a new Blueprint from this repo.
2. Keep the persistent disk mount at `/opt/render/project/src/instance`.
3. Set OAuth and mail env vars in the Render dashboard as needed.
4. For safe early testing, leave:
   - `DEV_MAILBOX_ENABLED=true`
   - `DEV_MAILBOX_CAPTURE_ONLY=true`
5. Deploy.

Notes:
- The checked-in start command is `python3 render_start.py`.
- That startup path runs DB checks and migrations in the live runtime instance.
- Render pre-deploy commands are not used here because this app currently relies on a persistent disk-backed SQLite database.
- Persistent-disk services on Render do not get zero-downtime deploys.

When you later move to Postgres/object storage, you can switch to a cleaner stateless deployment model.

## Routes (Current)
- `GET /health`
- `GET /api/me`
- `POST /api/tasks`
- `POST /webhooks/google/calendar`
- `POST /webhooks/microsoft/calendar`
 - `GET /login`
 - `GET /login/google`
 - `GET /login/microsoft`
 - `GET /`
 - `GET /invites/<token>`

## Next Implementation Steps
- Create calendar events on task assignment.
- Subscribe to calendar webhooks for real-time updates.
- Wire emails for magic-link invites.
- UI for calendar sync status and assignment activity.

## DB Migrations
If you already initialized the DB, run:
1. `flask db migrate -m "add assignments and invite links"`
2. `flask db upgrade`

## Calendar Sync Notes
- Uses the primary calendar for now.
- Events are created only when a task has a due date.
