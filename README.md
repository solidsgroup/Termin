# Termin
![Termin logo](logo/termin.svg)

**Termin** is a lightweight, calendar-first project and task collaboration platform. It combines direct messaging-style spaces, shared projects, and todo boards with real-time sync powered by WebSockets, Markdown + MathJax descriptions, and drag-and-drop organization.

![Status badge](https://img.shields.io/badge/status-internal-blue)

## Key Features
- real-time collaboration with per-user status tracking, Markdown+MathJax descriptions, and socket-driven updates
- direct (1:1) task spaces that pair two people with optional group assignments
- todo list + tree views that load client-side data, support keyboard navigation, and render shared metadata
- OAuth for Google/GitHub/Microsoft, magic-link invites, and email/collaborator workflows
- SQLite-backed persistence suitable for Render/any deploy with optional Postgres later

## Architecture & Stack
- Flask + SQLAlchemy + Alembic migrations
- Socket.IO for live updates and notifications
- OAuth integrations (Google, GitHub, Microsoft) with connected accounts and primary email sync
- HTML/JS frontend served from Flask templates (dashboard/tree/todo views) with helper scripts for drag/drop, MathJax, and assignment UI
- Persistent SQLite for Render-friendly deploys (switchable to Postgres or another RDBMS)

## Local Setup (development)
1. Create virtualenv: `python3 -m venv .venv && source .venv/bin/activate`
2. Install Python deps: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` (create one if missing) and fill in secrets.
4. Initialize the database: `flask db upgrade` or run `python3 app.py` and let it auto-run migrations.
5. Start the server: `python3 app.py` (development reload hooked into Flask).
6. Open `http://localhost:5000/` and use the onboarding flow.

The app will check the database on startup and apply migrations if needed.

## Environment Variables
- `SECRET_KEY`
- `DATABASE_URL` (default: `sqlite:///instance/app.db`)
- `PUBLIC_BASE_URL` (for links, notifications)
- OAuth credentials: `GOOGLE_CLIENT_ID`/`SECRET`, `GITHUB_CLIENT_ID`/`SECRET`, `MICROSOFT_CLIENT_ID`/`SECRET`
- `GOOGLE_WEBHOOK_SECRET`
- Mail settings: `MAIL_PROVIDER`, `MAIL_FROM_EMAIL`, `MAIL_FROM_NAME`, `SMTP_*`
- `DEV_MAILBOX_ENABLED`, `DEV_MAILBOX_CAPTURE_ONLY` for local testing

## OAuth Redirect URIs (development)
- Google: `http://localhost:5000/auth/google/callback`
- GitHub: `http://localhost:5000/auth/github/callback`
- Microsoft: `http://localhost:5000/auth/microsoft/callback`

## Render Deploy
Use the provided [render.yaml](./render.yaml) to create a Blueprint deploy on Render. The service runs Gunicorn, mounts `/opt/render/project/src/instance` with SQLite, and executes migrations before launch.

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

## Notable Routes
- `GET /health`
- `GET /api/me`, `POST /api/tasks`, `/api/comments`, `/api/direct-projects`
- `/api/users?q=`, `/api/projects/<id>`, `/api/groups/<id>` for the tree view
- OAuth: `/login`, `/login/google`, `/login/microsoft`, `/auth/*` callbacks
- `/webhooks/google/calendar` for push notifications

## Current Workstreams
- Markdown + MathJax descriptions in settings and comments.
- Enhanced direct project UX with self-spaces and group assignment syncing.
- Tree view with drag-and-drop & context menus.
- Notifications with desktop prompts and multi-server Electron prototypes.

## Database Migrations
Use `flask db migrate` / `flask db upgrade` (Alembic). You can also drop and let `app.py` auto-migrate if the schema converges.

## Calendar Sync Notes
- Uses the primary calendar for now.
- Events are created only when a task has a due date.
