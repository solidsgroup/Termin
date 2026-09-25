# ML Agent Log

## Purpose
Brief, living record of key decisions, changes, and known issues while iterating on this project.

## Summary
- Built a lightweight project management UI with task/subtask grid, assignment badges, and invite flows.
- Added assignment model and invite send/accept flow hooks.
- Integrated Google/Microsoft OAuth (calendar scopes) and user creation.
- Added assignment autocomplete suggestions and badge UX refinements.
- Added user avatar support from Google profile (with fallback to initials).

## Data Model Notes
- `users.avatar_url` now stored for Google profile photos.
- `assignments` links tasks/subtasks to account users or emails.
- `invites` ties magic links to assignments.

## UI/UX Notes
- Gantt tracks in project and Todo views accept clicks to set an undated task's due date, or add a missing start date before an existing due date. Date selection is computed on pointer press, without depending on hovering over the provisional handle. Locked tasks and relative-date rules remain protected.
- Playwright coverage checks Gantt date creation, persistence after reload, clearing/recreating start dates, due-date dragging, and locked tasks.
- Assign badges unified for user/email with hover remove (X) affordance.
- Send-link button reduced to text link.
- Task delete confirm when subtasks or sent invites exist.

## Known Considerations
- Google avatar URLs may fail to load in some environments; UI falls back to initials.
- If external image loading becomes unreliable, consider proxying avatar images through the app.

## Next Ideas
- Add assignment remove confirm or undo.
- Improve assignee suggestions ranking (recent, frequent).
