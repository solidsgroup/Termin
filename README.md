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
1. Create a virtualenv and install dependencies.
2. Set env vars in `.env` (see below).
3. Initialize the database and run migrations.
4. Run the app.

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
