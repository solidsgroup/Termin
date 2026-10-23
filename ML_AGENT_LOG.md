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
- Todo defaults to Gantt on desktop when no valid view preference is saved. Explicit list/compact preferences persist; the existing mobile list layout remains in place.
- Todo uses aggregate multi-task status for viewers without a personal assignment. Completed tasks viewed through other-assignee filters stay complete and do not extend the Todo Gantt range; assigned users retain their personal completion state.
- Assignment modes distinguish direct assignment, volunteer requests, and confirmation requests. A blank volunteer count asks every invitee to respond; confirmation mode always asks every assignee. Existing volunteer limits are preserved by the nullable-capacity migration.
- Gantt tracks in project and Todo views accept clicks to set an undated task's due date, or add a missing start date before an existing due date. Date selection is computed on pointer press, without depending on hovering over the provisional handle. Locked tasks and relative-date rules remain protected.
- Playwright coverage checks Gantt date creation, persistence after reload, clearing/recreating start dates, due-date dragging, and locked tasks.
- Assign badges unified for user/email with hover remove (X) affordance.
- Send-link button reduced to text link.
- Task delete confirm when subtasks or sent invites exist.

## Volunteer Reliability
- Reproduced delayed volunteer HTTP responses overwriting a newer assignee list in the open drawer. Response handling now resolves the newest task snapshot before updating both the store and drawer.
- Bulk invitations in volunteer/confirmation modes are additive: existing assignees and accepted responses survive project membership changes. Duplicate cleanup preserves acceptance and reattaches invite links to the retained assignment.
- Assignment creation and bulk invitation use the same task lock as volunteer responses. Regression coverage includes out-of-order responses, live account/email acceptance, repeated invitations, reloading, and unvolunteering without removing the assignee.

## Known Considerations
- Google avatar URLs may fail to load in some environments; UI falls back to initials.
- If external image loading becomes unreliable, consider proxying avatar images through the app.

## Next Ideas
- Add assignment remove confirm or undo.
- Improve assignee suggestions ranking (recent, frequent).
