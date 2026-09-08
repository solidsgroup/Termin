import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from time import perf_counter, perf_counter_ns
from unittest.mock import patch

from sqlalchemy import event, inspect as sqlalchemy_inspect


TEST_ROOT = Path(tempfile.mkdtemp(prefix="termin-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_ROOT / 'test.db'}")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:5000")

if "pywebpush" not in sys.modules:
    pywebpush_stub = types.ModuleType("pywebpush")

    class WebPushException(Exception):
        pass

    def webpush(*args, **kwargs):
        return None

    pywebpush_stub.WebPushException = WebPushException
    pywebpush_stub.webpush = webpush
    sys.modules["pywebpush"] = pywebpush_stub

from app import create_app
from app import favicon_cache
from app.canvas_sync import CanvasAssignment, CanvasCourse, CanvasSyncError, apply_canvas_course, fetch_canvas_course
from app.google_drive_sync import GoogleDriveComment, GoogleDriveFile, apply_google_drive_file, fetch_google_drive_file
from app.extensions import db, socketio
from app.info_utils import load_info_payload, normalize_info_payload
from app.models import Assignment, CalendarAccount, CanvasAssignmentLink, DevMailboxMessage, ExternalIdentity, GoogleDriveCommentLink, GoogleDriveIntegration, Group, Project, ProjectMember, ProjectTeamShare, Task, TaskComment, TaskFollower, TaskPrerequisite, TaskUserStatus, TeamInvite, User, UserEmail
from app.realtime import emit_task_updated
from app.task_status import task_status_meta


class DashboardRealtimeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
            DEV_MAILBOX_ENABLED=True,
            DEV_MAILBOX_CAPTURE_ONLY=True,
            MAIL_FROM_EMAIL="noreply@example.com",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def setUp(self):
        self.app = self.__class__.app
        self.client = self.app.test_client()
        with self.app.app_context():
            db.drop_all()
            db.create_all()

    def login(self, client, user):
        user_id = user if isinstance(user, int) else sqlalchemy_inspect(user).identity[0]
        with client.session_transaction() as session:
            session["user_id"] = user_id

    def create_user(self, email, display_name=None):
        user = User(email=email, display_name=display_name or email.split("@", 1)[0], timezone="America/Chicago")
        db.session.add(user)
        db.session.commit()
        return user

    def create_project(self, owner, name="Project", *, is_direct=False, is_team=False, direct_peer=None):
        project = Project(
            name=name,
            owner_id=owner.id,
            is_direct=is_direct,
            is_team=is_team,
            direct_user_a_id=owner.id if is_direct else None,
            direct_user_b_id=direct_peer.id if (is_direct and direct_peer) else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(project)
        db.session.flush()
        if direct_peer:
            db.session.add(ProjectMember(project_id=project.id, user_id=direct_peer.id))
        db.session.commit()
        return project

    def create_task(self, project, title, *, creator=None, due_at=None, status="open"):
        task = Task(
            project_id=project.id,
            creator_user_id=creator.id if creator else None,
            title=title,
            due_at=due_at,
            status=status,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(task)
        db.session.commit()
        return task

    def add_assignment(self, task, *, user=None, email=None, status="assigned"):
        assignment = Assignment(task_id=task.id, user_id=user.id if user else None, email=email, status=status)
        db.session.add(assignment)
        db.session.commit()
        return assignment

    def test_dashboard_routes_render_dashboard_view(self):
        with self.app.app_context():
            user = self.create_user("owner@example.com", "Owner")
            user_id = sqlalchemy_inspect(user).identity[0]

        self.login(self.client, user_id)

        root_response = self.client.get("/")
        dashboard_response = self.client.get("/dashboard")

        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertIn('data-dashboard-current-view="dashboard"', root_response.get_data(as_text=True))
        self.assertIn('data-dashboard-current-view="dashboard"', dashboard_response.get_data(as_text=True))

    def test_problem_task_modes_persist_and_assigning_clears_no_assignee(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = owner.id
            project = self.create_project(owner, "Task modes")
            project_id = project.id

        self.login(self.client, owner_id)
        create_response = self.client.post(
            "/api/tasks",
            json={
                "project_id": project_id,
                "title": "External deadline",
                "due_mode": "urgent",
                "assignee_mode": "none",
            },
        )

        self.assertEqual(create_response.status_code, 201)
        created = create_response.get_json()["task"]
        task_id = created["id"]
        self.assertEqual(created["creator_user_id"], owner_id)
        self.assertEqual(created["due_mode"], "urgent")
        self.assertEqual(created["assignee_mode"], "none")
        self.assertIsNone(created["due_at"])

        bootstrap = self.client.get("/api/dashboard-bootstrap").get_json()["dashboard"]
        bootstrap_task = bootstrap["entities"]["tasks"][str(task_id)]
        self.assertEqual(bootstrap_task["due_mode"], "urgent")
        self.assertEqual(bootstrap_task["assignee_mode"], "none")

        low_priority_response = self.client.patch(
            f"/api/tasks/{task_id}",
            json={"due_mode": "low_priority"},
        )
        self.assertEqual(low_priority_response.status_code, 200)
        self.assertEqual(low_priority_response.get_json()["due_mode"], "low_priority")
        self.assertEqual(low_priority_response.get_json()["assignee_mode"], "none")

        assignment_response = self.client.post(
            "/api/assignments",
            json={
                "target_type": "task",
                "target_id": task_id,
                "email": "owner@example.com",
            },
        )
        self.assertEqual(assignment_response.status_code, 201)
        task_response = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(task_response.get_json()["assignee_mode"], "default")

    def test_problems_route_renders_problem_view(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = owner.id

        self.login(self.client, owner_id)
        response = self.client.get("/problems")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-dashboard-current-view="problems"', response.get_data(as_text=True))
        self.assertIn('data-problems-list', response.get_data(as_text=True))

    def test_dashboard_uses_cached_stylesheets_and_gzip(self):
        with self.app.app_context():
            user = self.create_user("owner@example.com", "Owner")
            user_id = sqlalchemy_inspect(user).identity[0]

        self.login(self.client, user_id)
        response = self.client.get("/dashboard", headers={"Accept-Encoding": "gzip"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", response.headers.get("Vary", ""))
        html = gzip.decompress(response.get_data()).decode("utf-8")
        self.assertIn("/static/base.css?v=", html)
        self.assertIn("/static/dashboard.css?v=", html)

    def test_dashboard_changes_omits_full_entity_store(self):
        with self.app.app_context():
            user = self.create_user("owner@example.com", "Owner")
            user_id = sqlalchemy_inspect(user).identity[0]
            project = self.create_project(user, "Delta")
            self.create_task(project, "Delta task", creator=user)

        self.login(self.client, user_id)
        bootstrap_response = self.client.get("/api/dashboard-bootstrap")
        cursor = bootstrap_response.get_json()["dashboard"]["meta"]["cursor"]
        changes_response = self.client.get(
            "/api/dashboard-changes",
            query_string={"cursor": cursor},
        )

        self.assertEqual(changes_response.status_code, 200)
        dashboard = changes_response.get_json()["dashboard"]
        self.assertNotIn("entities", dashboard)
        self.assertIn("changes", dashboard)

    def test_poll_incomplete_assignees_follow_results_visibility(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            project = self.create_project(owner, "Poll policy")
            task = self.create_task(project, "Private poll", creator=owner)
            self.add_assignment(task, user=owner)
            self.add_assignment(task, user=member)
            task.info = normalize_info_payload(
                {
                    "meta": {
                        "task_type": "poll",
                        "poll": {
                            "question": "Choose one",
                            "results_visibility": "creator",
                            "options": [{"id": "option-a", "label": "Option A"}],
                            "responses": [
                                {
                                    "user_id": member.id,
                                    "email": member.email,
                                    "option_ids": ["option-a"],
                                }
                            ],
                        },
                    }
                }
            )
            db.session.add(task)
            db.session.commit()

            owner_meta = task_status_meta(task, viewer_user_id=owner.id)
            member_meta = task_status_meta(task, viewer_user_id=member.id)

            self.assertTrue(owner_meta["poll_results_visible"])
            self.assertEqual(
                [row["display_name"] for row in owner_meta["poll_incomplete_assignees"]],
                ["Owner"],
            )
            self.assertFalse(member_meta["poll_results_visible"])
            self.assertEqual(member_meta["poll_incomplete_assignees"], [])

            info = load_info_payload(task.info, task.link)
            info["meta"]["poll"]["results_visibility"] = "everyone"
            task.info = normalize_info_payload(info, task.link)
            db.session.add(task)
            db.session.commit()
            member_meta = task_status_meta(task, viewer_user_id=member.id)

            self.assertTrue(member_meta["poll_results_visible"])
            self.assertEqual(
                [row["display_name"] for row in member_meta["poll_incomplete_assignees"]],
                ["Owner"],
            )

    def test_copy_group_to_other_projects_copies_tasks_and_internal_prerequisites(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            peer = self.create_user("peer@example.com", "Peer")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            source_project = self.create_project(owner, "Source")
            target_project = self.create_project(owner, "Target")
            direct_project = self.create_project(owner, "Direct", is_direct=True, direct_peer=peer)
            source_group = Group(
                project_id=source_project.id,
                name="Launch",
                position=1,
                color="#4cc9f0",
                info=normalize_info_payload({"html": "<p>group notes</p>", "attachments": []}),
                link="https://example.com/group",
                description="Group description",
                description_format="markdown",
            )
            db.session.add(source_group)
            db.session.flush()
            first = Task(
                project_id=source_project.id,
                group_id=source_group.id,
                creator_user_id=owner.id,
                title="First",
                position=1,
                status="open",
                status_mode="single",
                info=normalize_info_payload({"html": "<p>first</p>", "attachments": []}),
            )
            second = Task(
                project_id=source_project.id,
                group_id=source_group.id,
                creator_user_id=owner.id,
                title="Second",
                position=2,
                status="complete",
                status_mode="single",
                info=normalize_info_payload({"html": "<p>second</p>", "attachments": []}),
            )
            db.session.add_all([first, second])
            db.session.flush()
            db.session.add(TaskPrerequisite(task_id=second.id, prerequisite_task_id=first.id))
            db.session.commit()
            source_group_id = source_group.id
            target_project_id = target_project.id
            direct_project_id = direct_project.id

        self.login(self.client, owner_id)
        response = self.client.post(
            f"/api/groups/{source_group_id}/copy",
            json={"project_ids": [target_project_id, direct_project_id]},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(len(payload["groups"]), 2)
        self.assertEqual({row["project_id"] for row in payload["groups"]}, {target_project_id, direct_project_id})

        with self.app.app_context():
            for project_id in (target_project_id, direct_project_id):
                copied_group = Group.query.filter_by(project_id=project_id, name="Launch").one()
                self.assertEqual(copied_group.color, "#4cc9f0")
                self.assertEqual(copied_group.link, "https://example.com/group")
                copied_tasks = Task.query.filter_by(group_id=copied_group.id).order_by(Task.position.asc()).all()
                self.assertEqual([task.title for task in copied_tasks], ["First", "Second"])
                self.assertTrue(all(task.project_id == project_id for task in copied_tasks))
                copied_prerequisite = TaskPrerequisite.query.filter_by(task_id=copied_tasks[1].id).one()
                self.assertEqual(copied_prerequisite.prerequisite_task_id, copied_tasks[0].id)

    def test_project_gantt_ranges_persist_in_project_payloads(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Planning")
            project.start_date = datetime(2026, 1, 1).date()
            project.end_date = datetime(2026, 3, 31).date()
            db.session.commit()
            project_id = sqlalchemy_inspect(project).identity[0]

        self.login(self.client, owner_id)
        ranges = [
            {
                "id": "range-alpha",
                "label": "Conference travel",
                "start": "2026-02-03",
                "end": "2026-02-10",
                "color": "#4cc9f0",
            }
        ]

        patch_response = self.client.patch(f"/api/projects/{project_id}", json={"gantt_ranges": ranges})
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.get_json()["gantt_ranges"], ranges)

        project_response = self.client.get(f"/api/projects/{project_id}")
        self.assertEqual(project_response.status_code, 200)
        self.assertEqual(project_response.get_json()["gantt_ranges"], ranges)

        snapshot_response = self.client.get(f"/api/projects/{project_id}/tree_snapshot")
        self.assertEqual(snapshot_response.status_code, 200)
        self.assertEqual(snapshot_response.get_json()["project"]["gantt_ranges"], ranges)

    def test_tree_snapshot_query_count_stays_bounded_as_tasks_grow(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Large project")
            tasks = [
                Task(
                    project_id=project.id,
                    creator_user_id=owner.id,
                    title=f"Task {index}",
                    position=index,
                    status="open",
                )
                for index in range(40)
            ]
            db.session.add_all(tasks)
            db.session.flush()
            db.session.add_all([
                Assignment(task_id=task.id, user_id=owner.id, status="assigned")
                for task in tasks
            ])
            db.session.commit()
            project_id = project.id
            engine = db.engine

        self.login(self.client, owner_id)
        statements = []

        def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            response = self.client.get(f"/api/projects/{project_id}/tree_snapshot")
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        task_count = len(payload["ungrouped_tasks"]) + sum(
            len(group["tasks"])
            for group in payload["groups"]
        )
        self.assertEqual(task_count, 40)
        self.assertLessEqual(
            len(statements),
            30,
            f"Tree snapshot issued {len(statements)} SQL statements for 40 tasks",
        )

    def test_favicon_cache_miss_returns_while_discovery_runs_in_background(self):
        target = f"https://favicon-perf-{perf_counter_ns()}.invalid/example"
        worker_started = Event()
        release_worker = Event()

        def slow_cache(_app, _target):
            worker_started.set()
            release_worker.wait(timeout=2)
            return False

        try:
            with patch("app.favicon_cache.cache_favicon_for_link", side_effect=slow_cache):
                with self.app.test_request_context("/link-favicon"):
                    started_at = perf_counter()
                    response = favicon_cache.favicon_response_for_link(self.app, target)
                    elapsed = perf_counter() - started_at

                self.assertEqual(response.headers.get("X-Termin-Favicon-Pending"), "1")
                self.assertLess(elapsed, 0.1)
                self.assertTrue(worker_started.wait(timeout=1))
                response.close()
        finally:
            release_worker.set()

    def test_automatic_github_sync_is_scheduled_without_blocking_request(self):
        with self.app.app_context():
            owner = self.create_user("github-owner@example.com", "GitHub Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            db.session.add(ExternalIdentity(
                user_id=owner_id,
                provider="github",
                provider_user_id="github-owner",
                access_token="test-token",
            ))
            db.session.commit()

        self.login(self.client, owner_id)
        with patch("app.routes.schedule_github_sync_for_user", return_value=True) as schedule_sync:
            started_at = perf_counter()
            response = self.client.post("/api/github/sync")
            elapsed = perf_counter() - started_at

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"ok": True, "scheduled": True})
        self.assertLess(elapsed, 0.1)
        schedule_sync.assert_called_once()

    def test_canvas_group_import_and_refresh_reconcile_assignments(self):
        source_url = "https://canvas.example.edu/courses/129714/assignments"
        with self.app.app_context():
            owner = self.create_user("canvas-owner@example.com", "Canvas Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Coursework")
            project_id = project.id

        initial_course = CanvasCourse(
            source_url=source_url,
            name="Advanced Flight Structures (Fall 2026)",
            course_id="129714",
            assignments=(
                CanvasAssignment(
                    assignment_id="2850948",
                    title="Problem Set 1",
                    description='<p>Read the <a href="https://example.edu/brief.pdf">brief</a>.</p>',
                    due_at=datetime(2026, 9, 2, 16, 0),
                    html_url="https://canvas.example.edu/courses/129714/assignments/2850948",
                    links=(
                        "https://canvas.example.edu/courses/129714/assignments/2850948",
                        "https://example.edu/brief.pdf",
                    ),
                    position=1,
                    assignment_group_id="622939",
                    assignment_group_name="Problem Sets",
                    points_possible=100.0,
                    unlock_at="2026-08-24T05:00:00Z",
                    start_at=datetime(2026, 8, 24, 0, 0),
                    submission_types=("on_paper",),
                ),
            ),
        )
        self.login(self.client, owner_id)
        with patch("app.routes.fetch_canvas_course", return_value=initial_course):
            response = self.client.post(
                f"/api/projects/{project_id}/canvas-groups",
                json={"url": source_url},
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["group"]["specialty_type"], "canvas")
        self.assertEqual(payload["group"]["canvas"]["source_url"], source_url)
        self.assertEqual(payload["sync"], {"created": 1, "updated": 0, "removed": 0, "total": 1})
        created_task = payload["tasks"][0]
        self.assertTrue(created_task["locked"])
        self.assertEqual(created_task["assignee_mode"], "none")
        self.assertEqual(created_task["due_mode"], "date")
        self.assertEqual(created_task["due_at"], "2026-09-02T16:00:00")
        self.assertEqual(created_task["description_format"], "html")
        self.assertEqual(created_task["start_date"], "2026-08-24")
        self.assertEqual(created_task["info"]["meta"]["canvas"]["assignment_id"], "2850948")
        self.assertEqual(created_task["info"]["meta"]["canvas"]["canvas_task_key"], "2850948")
        self.assertEqual(
            created_task["info"]["links"],
            [
                "https://canvas.example.edu/courses/129714/assignments/2850948",
                "https://example.edu/brief.pdf",
            ],
        )

        group_id = payload["group"]["id"]
        original_task_id = created_task["id"]
        manual_task_response = self.client.post(
            "/api/tasks",
            json={"project_id": project_id, "group_id": group_id, "title": "Manual task"},
        )
        self.assertEqual(manual_task_response.status_code, 409)
        for task_update in (
            {"locked": False},
            {"description": "Locally edited description"},
            {"links": ["https://example.edu/replacement"]},
        ):
            update_response = self.client.patch(f"/api/tasks/{original_task_id}", json=task_update)
            self.assertEqual(update_response.status_code, 423)
            self.assertEqual(update_response.get_json()["error"], "Canvas tasks are managed by Canvas")
        with self.app.app_context():
            db.session.add(TaskFollower(task_id=original_task_id, user_id=owner_id))
            db.session.add(TaskComment(task_id=original_task_id, user_id=owner_id, body="Remember this assignment"))
            db.session.commit()
        refreshed_course = CanvasCourse(
            source_url=source_url,
            name=initial_course.name,
            course_id="129714",
            assignments=(
                CanvasAssignment(
                    assignment_id="2850950",
                    title="Problem Set 2",
                    description="<p>Second assignment.</p>",
                    due_at=None,
                    html_url="https://canvas.example.edu/courses/129714/assignments/2850950",
                    links=("https://canvas.example.edu/courses/129714/assignments/2850950",),
                    position=1,
                ),
            ),
        )
        with patch("app.canvas_sync.fetch_canvas_course", return_value=refreshed_course):
            refresh_response = self.client.post(f"/api/groups/{group_id}/canvas/refresh")

        self.assertEqual(refresh_response.status_code, 200)
        refresh_payload = refresh_response.get_json()
        self.assertEqual(refresh_payload["sync"], {"created": 1, "updated": 0, "removed": 1, "total": 1})
        self.assertEqual(refresh_payload["removed_task_ids"], [original_task_id])
        with self.app.app_context():
            tasks = Task.query.filter_by(group_id=group_id).all()
            self.assertEqual([(task.title, task.locked) for task in tasks], [("Problem Set 2", True)])
            self.assertEqual(CanvasAssignmentLink.query.filter_by(group_id=group_id).count(), 1)
            self.assertEqual(Assignment.query.filter_by(task_id=tasks[0].id).count(), 0)
            self.assertEqual(TaskFollower.query.filter_by(task_id=original_task_id).count(), 0)
            self.assertEqual(TaskComment.query.filter_by(task_id=original_task_id).count(), 0)
            self.assertEqual(load_info_payload(tasks[0].info, tasks[0].link)["meta"]["assignee_mode"], "none")

    def test_google_drive_comments_sync_assignments_resolution_and_deletion(self):
        with self.app.app_context():
            owner = self.create_user("drive-owner@example.com", "Drive Owner")
            assignee = self.create_user("drive-assignee-primary@example.com", "Drive Assignee")
            alternate_assignee_email = "drive-assignee@alternate.example.com"
            db.session.add(ExternalIdentity(
                user_id=assignee.id,
                provider="google",
                provider_user_id="alternate-google-account",
                email=alternate_assignee_email,
            ))
            project = self.create_project(owner, "Drive Project")
            group = Group(project_id=project.id, name="Draft", specialty_type="google_drive")
            db.session.add(group)
            db.session.flush()
            integration = GoogleDriveIntegration(
                group_id=group.id,
                authorized_user_id=owner.id,
                file_id="drive-file-12345",
                file_name="Draft",
            )
            db.session.add(integration)
            db.session.flush()
            initial_file = GoogleDriveFile(
                file_id="drive-file-12345",
                name="Proposal Draft",
                mime_type="application/vnd.google-apps.document",
                web_view_link="https://docs.google.com/document/d/drive-file-12345/edit",
                comments=(
                    GoogleDriveComment(
                        comment_id="comment-1",
                        content=f"@{alternate_assignee_email} Revise the abstract\nwith the new result.",
                        created_at=datetime(2026, 9, 1, 12, 0),
                        modified_at=datetime(2026, 9, 1, 12, 30),
                        resolved=False,
                        deleted=False,
                        assignee_email=alternate_assignee_email,
                        author_name="Reviewer",
                        author_avatar_url="https://example.com/reviewer.png",
                        quoted_content="Old abstract",
                        replies=(),
                    ),
                    GoogleDriveComment(
                        comment_id="comment-2",
                        content="Check the figure caption",
                        created_at=datetime(2026, 9, 1, 13, 0),
                        modified_at=datetime(2026, 9, 1, 13, 0),
                        resolved=True,
                        deleted=False,
                        assignee_email="",
                        author_name="Editor",
                        author_avatar_url="",
                        quoted_content="Figure 2",
                        replies=(),
                    ),
                ),
            )

            result = apply_google_drive_file(
                group,
                integration,
                initial_file,
                actor_user_id=owner.id,
                full_sync=True,
            )
            db.session.commit()

            self.assertEqual(result.as_dict(), {
                "created": 2,
                "updated": 0,
                "removed": 0,
                "total": 2,
                "scanned": 2,
                "full_sync": True,
            })
            tasks = Task.query.filter_by(group_id=group.id).order_by(Task.position.asc()).all()
            first_task_id, second_task_id = tasks[0].id, tasks[1].id
            self.assertEqual([task.title for task in tasks], ["Revise the abstract", "Check the figure caption"])
            self.assertEqual(tasks[0].description.splitlines()[0], "Revise the abstract")
            self.assertEqual([task.status for task in tasks], ["open", "complete"])
            self.assertTrue(all(task.locked for task in tasks))
            first_info = load_info_payload(tasks[0].info)
            self.assertEqual(first_info["meta"]["google_drive"]["comment_id"], "comment-1")
            self.assertEqual(first_info["meta"]["google_drive"]["assignee_email"], alternate_assignee_email)
            first_assignment = Assignment.query.filter_by(task_id=first_task_id).one()
            self.assertEqual(first_assignment.user_id, assignee.id)
            self.assertIsNone(first_assignment.email)
            self.assertEqual(Assignment.query.filter_by(task_id=second_task_id).count(), 0)
            self.assertEqual(load_info_payload(tasks[1].info)["meta"]["assignee_mode"], "none")

            tasks[0].due_at = datetime(2026, 9, 12)
            first_info["meta"]["due_mode"] = "urgent"
            tasks[0].info = normalize_info_payload(first_info, tasks[0].link)
            db.session.commit()

            resolved_comment = GoogleDriveComment(
                **{**initial_file.comments[0].__dict__, "resolved": True, "modified_at": datetime(2026, 9, 2, 9, 0)}
            )
            incremental_file = GoogleDriveFile(
                file_id=initial_file.file_id,
                name=initial_file.name,
                mime_type=initial_file.mime_type,
                web_view_link=initial_file.web_view_link,
                comments=(resolved_comment,),
            )
            incremental_result = apply_google_drive_file(
                group,
                integration,
                incremental_file,
                actor_user_id=owner.id,
                full_sync=False,
            )
            db.session.commit()
            self.assertEqual([task.id for task in incremental_result.updated_tasks], [first_task_id])
            refreshed_first_task = Task.query.get(first_task_id)
            self.assertEqual(refreshed_first_task.status, "complete")
            self.assertEqual(refreshed_first_task.due_at, datetime(2026, 9, 12))
            self.assertEqual(load_info_payload(refreshed_first_task.info)["meta"]["due_mode"], "urgent")
            self.assertIsNotNone(Task.query.get(second_task_id))
            group_version_after_change = Group.query.get(group.id).updated_at

            unchanged_result = apply_google_drive_file(
                group,
                integration,
                incremental_file,
                actor_user_id=owner.id,
                full_sync=False,
            )
            db.session.commit()
            self.assertEqual(unchanged_result.updated_tasks, [])
            self.assertEqual(Group.query.get(group.id).updated_at, group_version_after_change)

            empty_full_file = GoogleDriveFile(
                file_id=initial_file.file_id,
                name=initial_file.name,
                mime_type=initial_file.mime_type,
                web_view_link=initial_file.web_view_link,
                comments=(),
            )
            removed_result = apply_google_drive_file(
                group,
                integration,
                empty_full_file,
                actor_user_id=owner.id,
                full_sync=True,
            )
            db.session.commit()
            self.assertEqual({task.id for task in removed_result.deleted_tasks}, {first_task_id, second_task_id})
            self.assertEqual(Task.query.filter_by(group_id=group.id).count(), 0)
            self.assertEqual(GoogleDriveCommentLink.query.filter_by(group_id=group.id).count(), 0)

    def test_google_drive_connection_records_secondary_google_identity(self):
        with self.app.app_context():
            user = self.create_user("primary@example.com", "Primary User")
            user_id = user.id

        self.login(self.client, user_id)
        with self.client.session_transaction() as session:
            session["google_drive_connect_user_id"] = user_id
            session["google_drive_connect_popup"] = True

        userinfo_response = types.SimpleNamespace(json=lambda: {
            "sub": "secondary-google-account",
            "email": "Secondary.Google@example.com",
            "name": "Secondary Google",
            "picture": "https://example.com/secondary.png",
        })
        with (
            patch("app.auth.oauth.google.authorize_access_token", return_value={
                "access_token": "drive-access-token",
                "refresh_token": "drive-refresh-token",
                "expires_in": 3600,
                "scope": "openid email profile https://www.googleapis.com/auth/drive.readonly",
            }),
            patch("app.auth.oauth.google.get", return_value=userinfo_response),
        ):
            response = self.client.get("/auth/google/callback")

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            identity = ExternalIdentity.query.filter_by(
                provider="google",
                provider_user_id="secondary-google-account",
            ).one()
            self.assertEqual(identity.user_id, user_id)
            self.assertEqual(identity.email, "secondary.google@example.com")
            alias = UserEmail.query.filter_by(email="secondary.google@example.com").one()
            self.assertEqual(alias.user_id, user_id)
            self.assertFalse(alias.is_primary)
            account = CalendarAccount.query.filter_by(user_id=user_id, provider="google").one()
            self.assertEqual(account.provider_user_id, "secondary-google-account")

    def test_google_drive_group_route_requires_scope_and_creates_locked_tasks(self):
        file_url = "https://docs.google.com/document/d/drive-file-98765/edit"
        with self.app.app_context():
            owner = self.create_user("drive-route@example.com", "Drive Route")
            owner_id = owner.id
            project = self.create_project(owner, "Drive Route Project")
            project_id = project.id
            db.session.add(CalendarAccount(
                user_id=owner.id,
                provider="google",
                provider_user_id="google-drive-route",
                access_token="picker-token",
                refresh_token="refresh-token",
                token_expires_at=datetime.utcnow() + timedelta(hours=1),
                scopes="openid email https://www.googleapis.com/auth/drive.file",
            ))
            db.session.commit()

        drive_file = GoogleDriveFile(
            file_id="drive-file-98765",
            name="Review Document",
            mime_type="application/vnd.google-apps.document",
            web_view_link=file_url,
            comments=(
                GoogleDriveComment(
                    comment_id="route-comment-1",
                    content="Resolve this question",
                    created_at=datetime(2026, 9, 3, 10, 0),
                    modified_at=datetime(2026, 9, 3, 10, 0),
                    resolved=False,
                    deleted=False,
                    assignee_email="",
                    author_name="Reviewer",
                    author_avatar_url="",
                    quoted_content="",
                    replies=(),
                ),
            ),
        )
        self.login(self.client, owner_id)
        with patch("app.routes.fetch_google_drive_file", return_value=drive_file):
            response = self.client.post(
                f"/api/projects/{project_id}/google-drive-groups",
                json={"url": file_url},
            )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["group"]["specialty_type"], "google_drive")
        self.assertEqual(payload["group"]["google_drive"]["file_id"], "drive-file-98765")
        self.assertTrue(payload["tasks"][0]["locked"])
        self.assertEqual(payload["tasks"][0]["assignee_mode"], "none")
        task_id = payload["tasks"][0]["id"]
        update_response = self.client.patch(f"/api/tasks/{task_id}", json={"status": "complete"})
        self.assertEqual(update_response.status_code, 423)
        self.assertEqual(update_response.get_json()["error"], "Google Drive tasks are managed by Google Drive")
        due_response = self.client.patch(
            f"/api/tasks/{task_id}",
            json={"due_at": "2026-09-21", "due_mode": "date"},
        )
        self.assertEqual(due_response.status_code, 200)
        self.assertEqual(due_response.get_json()["due_at"], "2026-09-21T00:00:00")
        self.assertEqual(due_response.get_json()["due_mode"], "date")
        self.assertEqual(
            due_response.get_json()["info"]["meta"]["google_drive"]["comment_id"],
            "route-comment-1",
        )

        with patch.dict(self.app.config, {
            "GOOGLE_PICKER_API_KEY": "test-picker-key",
            "GOOGLE_CLOUD_PROJECT_NUMBER": "123456789",
        }):
            picker_response = self.client.get("/api/google/drive/picker-token")
        self.assertEqual(picker_response.status_code, 200)
        self.assertEqual(picker_response.get_json(), {
            "oauth_token": "picker-token",
            "developer_key": "test-picker-key",
            "app_id": "123456789",
        })

    def test_google_drive_connect_prompts_for_google_account_selection(self):
        with self.app.app_context():
            owner = self.create_user("drive-account-choice@example.com", "Drive Account Choice")
            owner_id = owner.id
        self.login(self.client, owner_id)

        with patch("app.auth.oauth.google.authorize_redirect", return_value="oauth redirect") as authorize_redirect:
            response = self.client.get("/connect/google/drive?popup=1")

        self.assertEqual(response.status_code, 200)
        call_kwargs = authorize_redirect.call_args.kwargs
        self.assertEqual(call_kwargs["prompt"], "select_account consent")
        self.assertIn("https://www.googleapis.com/auth/drive.file", call_kwargs["scope"])

    def test_google_drive_fetch_uses_incremental_comment_fields(self):
        class FakeResponse:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        with self.app.app_context():
            owner = self.create_user("drive-fetch@example.com", "Drive Fetch")
            account = CalendarAccount(
                user_id=owner.id,
                provider="google",
                access_token="drive-access-token",
                refresh_token="drive-refresh-token",
                token_expires_at=datetime.utcnow() + timedelta(hours=1),
                scopes="https://www.googleapis.com/auth/drive.file",
            )
            db.session.add(account)
            db.session.commit()
            responses = [
                FakeResponse({
                    "id": "drive-file-24680",
                    "name": "Design Notes",
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": "https://docs.google.com/document/d/drive-file-24680/edit",
                }),
                FakeResponse({
                    "comments": [
                        {
                            "id": "comment-24680",
                            "content": "@ASSIGNEE@example.com Update this section",
                            "createdTime": "2026-09-05T15:00:00Z",
                            "modifiedTime": "2026-09-05T15:05:00Z",
                            "resolved": False,
                            "deleted": False,
                            "mentionedEmailAddresses": ["ASSIGNEE@example.com"],
                            "author": {"displayName": "Reviewer", "photoLink": "https://example.com/avatar.png"},
                            "quotedFileContent": {"value": "Original section"},
                            "replies": [{"content": "I will revise it", "author": {"displayName": "Author"}}],
                        },
                        {
                            "id": "comment-explicit",
                            "content": "Assign this directly",
                            "createdTime": "2026-09-05T15:01:00Z",
                            "modifiedTime": "2026-09-05T15:06:00Z",
                            "assigneeEmailAddress": "EXPLICIT@example.com",
                            "mentionedEmailAddresses": ["mentioned@example.com"],
                        },
                        {
                            "id": "comment-multiple-mentions",
                            "content": "Ask two people",
                            "createdTime": "2026-09-05T15:02:00Z",
                            "modifiedTime": "2026-09-05T15:07:00Z",
                            "mentionedEmailAddresses": ["first@example.com", "second@example.com"],
                        },
                    ],
                }),
            ]
            with patch("app.google_drive_sync.requests.get", side_effect=responses) as drive_get:
                drive_file = fetch_google_drive_file(
                    account,
                    "drive-file-24680",
                    modified_since=datetime(2026, 9, 5, 14, 58),
                )

        self.assertEqual(drive_file.name, "Design Notes")
        self.assertEqual(
            [comment.assignee_email for comment in drive_file.comments],
            ["assignee@example.com", "explicit@example.com", ""],
        )
        self.assertEqual(drive_file.comments[0].modified_at, datetime(2026, 9, 5, 15, 5))
        comment_params = drive_get.call_args_list[1].kwargs["params"]
        self.assertEqual(comment_params["includeDeleted"], "true")
        self.assertEqual(comment_params["startModifiedTime"], "2026-09-05T14:58:00Z")
        self.assertIn("assigneeEmailAddress", comment_params["fields"])
        self.assertIn("mentionedEmailAddresses", comment_params["fields"])
        self.assertIn("resolved", comment_params["fields"])
        self.assertIn("replies", comment_params["fields"])

    def test_canvas_fetch_establishes_anonymous_session_before_assignment_groups_api(self):
        source_url = "https://canvas.example.edu/courses/129714/assignments"

        class FakeResponse:
            def __init__(self, body, *, headers=None):
                self.status_code = 200
                self.headers = headers or {}
                self._body = body

            def iter_content(self, chunk_size):
                del chunk_size
                yield self._body

            def close(self):
                return None

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.calls = []
                self.responses = [
                    FakeResponse(b'<title>Assignments: Test Course</title><script>ENV={"TIMEZONE":"America/Chicago"}</script>'),
                    FakeResponse(json.dumps([{
                        "id": 2,
                        "name": "Homework",
                        "position": 1,
                        "assignments": [{
                            "id": 7,
                            "name": "Homework 1",
                            "description": '<a href="/files/3">Brief</a>',
                            "due_at": "2026-09-03T04:59:00Z",
                            "html_url": "/courses/129714/assignments/7",
                            "position": 1,
                        }],
                    }]).encode("utf-8")),
                ]

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)

        session = FakeSession()
        with patch("app.canvas_sync.requests.Session", return_value=session), patch("app.canvas_sync._assert_public_host"):
            course = fetch_canvas_course(source_url)

        self.assertEqual(session.calls[0][0], source_url)
        self.assertIn("/api/v1/courses/129714/assignment_groups?", session.calls[1][0])
        self.assertIn("include%5B%5D=assignments", session.calls[1][0])
        self.assertIn("include%5B%5D=overrides", session.calls[1][0])
        self.assertNotIn("/api/v1/courses/129714/assignments", session.calls[1][0])
        self.assertEqual(course.name, "Test Course")
        self.assertEqual([(item.assignment_id, item.title) for item in course.assignments], [("7", "Homework 1")])
        self.assertEqual(course.assignments[0].due_at, datetime(2026, 9, 2, 23, 59))
        self.assertEqual(course.assignments[0].start_at, None)
        self.assertEqual(
            course.assignments[0].links,
            (
                "https://canvas.example.edu/courses/129714/assignments/7",
                "https://canvas.example.edu/files/3",
            ),
        )

    def test_canvas_public_assignment_overrides_import_as_separate_tasks(self):
        source_url = "https://canvas.example.edu/courses/129714/assignments"

        class FakeResponse:
            def __init__(self, body, *, headers=None):
                self.status_code = 200
                self.headers = headers or {}
                self._body = body

            def iter_content(self, chunk_size):
                del chunk_size
                yield self._body

            def close(self):
                return None

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.responses = [
                    FakeResponse(b'<title>Assignments: Flight Structures</title><script>ENV={"TIMEZONE":"America/Chicago"}</script>'),
                    FakeResponse(json.dumps([{
                        "id": 622940,
                        "name": "Labs",
                        "position": 1,
                        "assignments": [{
                            "id": 2850942,
                            "name": "Lab 2",
                            "description": '<a href="https://drive.example.edu/lab2">Lab 2</a>',
                            "due_at": None,
                            "unlock_at": None,
                            "lock_at": None,
                            "html_url": "https://canvas.example.edu/courses/129714/assignments/2850942",
                            "position": 2,
                            "assignment_group_id": 622940,
                            "only_visible_to_overrides": True,
                            "visible_to_everyone": False,
                            "overrides": [
                                {
                                    "id": 424661,
                                    "title": "Section AERE-4210-A",
                                    "assignment_id": 2850942,
                                    "due_at": "2026-09-14T18:55:00Z",
                                    "unlock_at": "2026-09-09T05:00:00Z",
                                },
                                {
                                    "id": 424662,
                                    "title": "Section AERE-4210-B",
                                    "assignment_id": 2850942,
                                    "due_at": "2026-09-16T18:55:00Z",
                                    "unlock_at": "2026-09-09T05:00:00Z",
                                },
                            ],
                        }],
                    }]).encode("utf-8")),
                ]

            def get(self, url, **kwargs):
                del url, kwargs
                return self.responses.pop(0)

        with patch("app.canvas_sync.requests.Session", return_value=FakeSession()), patch("app.canvas_sync._assert_public_host"):
            course = fetch_canvas_course(source_url)

        self.assertEqual([item.assignment_id for item in course.assignments], [
            "2850942:override:424661",
            "2850942:override:424662",
        ])
        self.assertEqual([item.title for item in course.assignments], [
            "Lab 2 - Section AERE-4210-A",
            "Lab 2 - Section AERE-4210-B",
        ])
        self.assertEqual(course.assignments[0].due_at, datetime(2026, 9, 14, 13, 55))
        self.assertEqual(course.assignments[1].due_at, datetime(2026, 9, 16, 13, 55))
        self.assertEqual(course.assignments[0].start_at, datetime(2026, 9, 9, 0, 0))
        self.assertEqual(course.assignments[0].source_assignment_id, "2850942")
        self.assertEqual(course.assignments[0].override_id, "424661")

        with self.app.app_context():
            owner = self.create_user("canvas-overrides@example.com", "Canvas Overrides")
            project = self.create_project(owner, "Coursework")
            group = Group(project_id=project.id, name="Canvas Course", specialty_type="canvas", specialty_source_url=source_url)
            db.session.add(group)
            db.session.flush()
            result = apply_canvas_course(group, course, actor_user_id=owner.id)
            db.session.commit()
            tasks = Task.query.filter_by(group_id=group.id).order_by(Task.position.asc()).all()
            infos = [load_info_payload(task.info, task.link) for task in tasks]

        self.assertEqual(result.as_dict(), {"created": 2, "updated": 0, "removed": 0, "total": 2})
        self.assertEqual([task.title for task in tasks], ["Lab 2 - Section AERE-4210-A", "Lab 2 - Section AERE-4210-B"])
        self.assertEqual([task.due_at for task in tasks], [datetime(2026, 9, 14, 13, 55), datetime(2026, 9, 16, 13, 55)])
        self.assertEqual([info["meta"].get("start_date") for info in infos], ["2026-09-09", "2026-09-09"])
        self.assertEqual([info["meta"]["canvas"]["assignment_id"] for info in infos], ["2850942", "2850942"])
        self.assertEqual([info["meta"]["canvas"]["override_id"] for info in infos], ["424661", "424662"])

    def test_canvas_refresh_failure_is_visible_and_throttled(self):
        source_url = "https://canvas.example.edu/courses/12/assignments"
        with self.app.app_context():
            owner = self.create_user("canvas-failure@example.com", "Canvas Failure")
            owner_id = owner.id
            project = self.create_project(owner, "Canvas Failure")
            group = Group(
                project_id=project.id,
                name="Canvas Course",
                specialty_type="canvas",
                specialty_source_url=source_url,
                specialty_last_synced_at=datetime.utcnow() - timedelta(days=2),
            )
            db.session.add(group)
            db.session.commit()
            group_id = group.id

        self.login(self.client, owner_id)
        with patch("app.canvas_sync.fetch_canvas_course", side_effect=CanvasSyncError("Canvas is unavailable")):
            response = self.client.post(f"/api/groups/{group_id}/canvas/refresh")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["group"]["canvas"]["sync_error"], "Canvas is unavailable")
        with patch("app.routes.schedule_canvas_sync_for_user", return_value=True) as schedule_sync:
            automatic_response = self.client.post("/api/canvas/sync")
        self.assertEqual(automatic_response.status_code, 200)
        self.assertEqual(automatic_response.get_json(), {"ok": True, "skipped": True})
        schedule_sync.assert_not_called()

    def test_same_bucket_task_move_returns_affected_positions(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Planning")
            group = Group(project_id=project.id, name="Work", position=1)
            db.session.add(group)
            db.session.flush()
            first = self.create_task(project, "First", creator=owner)
            second = self.create_task(project, "Second", creator=owner)
            third = self.create_task(project, "Third", creator=owner)
            first.group_id = group.id
            second.group_id = group.id
            third.group_id = group.id
            first.position = 1
            second.position = 2
            third.position = 3
            db.session.commit()
            project_id = project.id
            group_id = group.id
            first_id = first.id
            second_id = second.id
            third_id = third.id

        self.login(self.client, owner_id)
        socket_client = socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        self.assertTrue(socket_client.is_connected())
        socket_client.emit("join_project", {"project_id": project_id})
        socket_client.get_received()
        response = self.client.post(
            f"/api/tasks/{third_id}/move",
            json={
                "project_id": project_id,
                "group_id": group_id,
                "before_task_id": first_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["task_id"], third_id)
        affected = {row["id"]: row["position"] for row in payload.get("affected_tasks", [])}
        self.assertEqual(affected, {third_id: 1, first_id: 2, second_id: 3})
        reorder_events = [
            event
            for event in socket_client.get_received()
            if event["name"] == "tasks_updated" and event["args"][0].get("action") == "reordered"
        ]
        self.assertEqual(len(reorder_events), 1)
        socket_positions = {
            row["id"]: row["position"]
            for row in reorder_events[0]["args"][0].get("tasks", [])
        }
        self.assertEqual(socket_positions, {third_id: 1, first_id: 2, second_id: 3})

        with self.app.app_context():
            rows = (
                Task.query.filter(Task.id.in_([first_id, second_id, third_id]))
                .order_by(Task.position.asc(), Task.id.asc())
                .all()
            )
            self.assertEqual([row.id for row in rows], [third_id, first_id, second_id])
            self.assertEqual([row.position for row in rows], [1, 2, 3])

    def test_get_task_includes_status_meta_after_single_status_completion(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Planning")
            task = self.create_task(project, "Complete Me", creator=owner, status="open")
            self.add_assignment(task, user=owner)
            project_id = project.id
            task_id = task.id

        self.login(self.client, owner_id)
        patch_response = self.client.patch(f"/api/tasks/{task_id}", json={"status": "complete"})
        self.assertEqual(patch_response.status_code, 200)

        get_response = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(get_response.status_code, 200)
        payload = get_response.get_json()
        self.assertEqual(payload["project_id"], project_id)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["status_meta"]["mode"], "single")
        self.assertEqual(payload["status_meta"]["task_status_state"], "complete")

    def test_dashboard_action_items_only_show_tasks_assigned_to_current_user(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            other = self.create_user("other@example.com", "Other")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Planning")
            mine = self.create_task(project, "Assigned to me", creator=owner, due_at=datetime.utcnow() - timedelta(days=1))
            someone_else = self.create_task(project, "Assigned to someone else", creator=owner, due_at=datetime.utcnow())
            self.add_assignment(mine, user=owner)
            self.add_assignment(someone_else, user=other)

        self.login(self.client, owner_id)
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Assigned to me", html)
        self.assertNotIn("Assigned to someone else", html)
        self.assertRegex(html, r"Overdue</div>\s*<div class=\"dashboard-stat-value\"[^>]*>1</div>")
        self.assertRegex(html, r"Today / ASAP</div>\s*<div class=\"dashboard-stat-value\"[^>]*>0</div>")

    def test_dashboard_overdue_count_excludes_completed_assigned_tasks(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Planning")
            overdue_open = self.create_task(
                project,
                "Overdue open",
                creator=owner,
                due_at=datetime.utcnow() - timedelta(days=2),
                status="open",
            )
            overdue_complete = self.create_task(
                project,
                "Overdue complete",
                creator=owner,
                due_at=datetime.utcnow() - timedelta(days=3),
                status="complete",
            )
            self.add_assignment(overdue_open, user=owner)
            self.add_assignment(overdue_complete, user=owner)

        self.login(self.client, owner_id)
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Overdue open", html)
        self.assertNotIn("Overdue complete", html)
        self.assertRegex(html, r"Overdue</div>\s*<div class=\"dashboard-stat-value\"[^>]*>1</div>")

    def test_dashboard_action_items_only_include_tasks_assigned_to_current_user(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            other = self.create_user("other@example.com", "Other")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Action Items")
            mine = self.create_task(
                project,
                "Assigned to me",
                creator=owner,
                due_at=datetime.utcnow() - timedelta(days=2),
                status="open",
            )
            someone_else = self.create_task(
                project,
                "Assigned to someone else",
                creator=owner,
                due_at=datetime.utcnow() - timedelta(days=3),
                status="open",
            )
            completed_mine = self.create_task(
                project,
                "Completed mine",
                creator=owner,
                due_at=datetime.utcnow() - timedelta(days=4),
                status="complete",
            )
            self.add_assignment(mine, user=owner)
            self.add_assignment(someone_else, user=other)
            self.add_assignment(completed_mine, user=owner)

        self.login(self.client, owner_id)
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Assigned to me", html)
        self.assertNotIn("Assigned to someone else", html)
        self.assertNotIn("Completed mine", html)

    def test_create_team_creates_team_project_and_member_access(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            member_id = sqlalchemy_inspect(member).identity[0]

        self.login(self.client, owner_id)
        response = self.client.post("/api/teams", json={"name": "Research Team", "member_ids": [member_id]})
        payload = response.get_json()

        self.assertEqual(response.status_code, 201)
        self.assertTrue(payload["project"]["is_team"])
        self.assertFalse(payload["project"]["is_direct"])
        self.assertIsNone(payload["project"]["division_id"])
        self.assertEqual(payload["project"]["display_name"], "Research Team")
        project_id = payload["project"]["id"]

        with self.app.app_context():
            project = Project.query.get(project_id)
            self.assertIsNotNone(project)
            self.assertTrue(project.is_team)
            self.assertFalse(project.is_direct)
            self.assertEqual(project.name, "Research Team")
            self.assertIsNotNone(ProjectMember.query.filter_by(project_id=project_id, user_id=member_id).first())

        member_client = self.app.test_client()
        self.login(member_client, member_id)
        list_response = member_client.get("/api/teams")
        list_payload = list_response.get_json()
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual([item["id"] for item in list_payload["results"]], [project_id])

        tree_response = member_client.get(f"/tree/project/{project_id}")
        self.assertEqual(tree_response.status_code, 200)
        html = tree_response.get_data(as_text=True)
        self.assertIn('data-tree-team-project="%d"' % project_id, html)
        self.assertIn("Research Team", html)

    def test_team_project_members_can_be_added_and_removed(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            member_id = sqlalchemy_inspect(member).identity[0]
            project = self.create_project(owner, "Operations", is_team=True)
            project_id = sqlalchemy_inspect(project).identity[0]

        self.login(self.client, owner_id)
        add_response = self.client.post(f"/api/projects/{project_id}/members", json={"user_id": member_id})
        self.assertEqual(add_response.status_code, 201)
        with self.app.app_context():
            self.assertIsNotNone(ProjectMember.query.filter_by(project_id=project_id, user_id=member_id).first())

        member_client = self.app.test_client()
        self.login(member_client, member_id)
        self.assertEqual(member_client.get(f"/tree/project/{project_id}").status_code, 200)

        remove_response = self.client.delete(f"/api/projects/{project_id}/members/{member_id}")
        self.assertEqual(remove_response.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(ProjectMember.query.filter_by(project_id=project_id, user_id=member_id).first())
        self.assertEqual(member_client.get("/api/teams").get_json()["results"], [])

    def test_team_invite_sends_email_and_accepts_new_member(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            team = self.create_project(owner, "Invite Team", is_team=True)
            team_id = sqlalchemy_inspect(team).identity[0]

        self.login(self.client, owner_id)
        invite_response = self.client.post(f"/api/teams/{team_id}/invites", json={"email": "newperson@example.com"})
        self.assertEqual(invite_response.status_code, 201)
        invite_payload = invite_response.get_json()
        self.assertEqual(invite_payload["status"], "sent")
        self.assertIn("/team-invites/", invite_payload["invite_url"])

        with self.app.app_context():
            invite = TeamInvite.query.filter_by(team_project_id=team_id, email="newperson@example.com").first()
            self.assertIsNotNone(invite)
            self.assertEqual(invite.status, "sent")
            message = DevMailboxMessage.query.filter_by(to_email="newperson@example.com").first()
            self.assertIsNotNone(message)
            self.assertIn("Invite Team", message.subject)
            token = invite.token
            invite_url = f"/team-invites/{token}"

        anonymous_client = self.app.test_client()
        landing_response = anonymous_client.get(invite_url)
        self.assertEqual(landing_response.status_code, 200)
        self.assertIn("Sign in with the invited email address", landing_response.get_data(as_text=True))
        with anonymous_client.session_transaction() as session:
            self.assertEqual(session.get("team_invite_token"), token)

        invited_client = self.app.test_client()
        with self.app.app_context():
            invited = self.create_user("newperson@example.com", "New Person")
            invited_id = sqlalchemy_inspect(invited).identity[0]
        self.login(invited_client, invited_id)
        accept_response = invited_client.post(f"/team-invites/{token}/accept")
        self.assertEqual(accept_response.status_code, 200)
        self.assertIn("You are now a member of Invite Team", accept_response.get_data(as_text=True))

        with self.app.app_context():
            accepted_invite = TeamInvite.query.filter_by(token=token).first()
            self.assertEqual(accepted_invite.status, "accepted")
            self.assertEqual(accepted_invite.accepted_user_id, invited_id)
            self.assertIsNotNone(ProjectMember.query.filter_by(project_id=team_id, user_id=invited_id).first())

    def test_project_can_be_shared_with_team_and_resolves_members_dynamically(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            late_member = self.create_user("late@example.com", "Late")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            member_id = sqlalchemy_inspect(member).identity[0]
            late_member_id = sqlalchemy_inspect(late_member).identity[0]
            project = self.create_project(owner, "Shared Report")
            team = self.create_project(owner, "Research Team", is_team=True)
            db.session.add(ProjectMember(project_id=team.id, user_id=member.id))
            db.session.commit()
            project_id = sqlalchemy_inspect(project).identity[0]
            team_id = sqlalchemy_inspect(team).identity[0]

        self.login(self.client, owner_id)
        share_response = self.client.post(f"/api/projects/{project_id}/teams", json={"team_project_id": team_id})
        self.assertEqual(share_response.status_code, 201)
        teams_response = self.client.get(f"/api/projects/{project_id}/teams")
        self.assertEqual(teams_response.status_code, 200)
        self.assertEqual([item["id"] for item in teams_response.get_json()["teams"]], [team_id])
        self.assertEqual(set(teams_response.get_json()["teams"][0]["member_ids"]), {owner_id, member_id})
        with self.app.app_context():
            self.assertIsNotNone(ProjectTeamShare.query.filter_by(project_id=project_id, team_project_id=team_id).first())
            self.assertIsNone(ProjectMember.query.filter_by(project_id=project_id, user_id=member_id).first())

        member_client = self.app.test_client()
        self.login(member_client, member_id)
        self.assertEqual(member_client.get(f"/tree/project/{project_id}").status_code, 200)
        members_response = member_client.get(f"/api/projects/{project_id}/members")
        self.assertEqual(members_response.status_code, 200)
        member_ids = {item["id"] for item in members_response.get_json()["members"]}
        self.assertIn(member_id, member_ids)

        late_client = self.app.test_client()
        self.login(late_client, late_member_id)
        self.assertEqual(late_client.get(f"/api/projects/{project_id}/tree_snapshot").status_code, 403)

        add_response = self.client.post(f"/api/projects/{team_id}/members", json={"user_id": late_member_id})
        self.assertEqual(add_response.status_code, 201)
        self.assertEqual(late_client.get(f"/tree/project/{project_id}").status_code, 200)
        with self.app.app_context():
            self.assertIsNone(ProjectMember.query.filter_by(project_id=project_id, user_id=late_member_id).first())

    def test_removing_project_team_share_revokes_team_member_access(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            member_id = sqlalchemy_inspect(member).identity[0]
            project = self.create_project(owner, "Shared Report")
            team = self.create_project(owner, "Research Team", is_team=True)
            db.session.add(ProjectMember(project_id=team.id, user_id=member.id))
            db.session.add(ProjectTeamShare(project_id=project.id, team_project_id=team.id))
            db.session.commit()
            project_id = sqlalchemy_inspect(project).identity[0]
            team_id = sqlalchemy_inspect(team).identity[0]

        member_client = self.app.test_client()
        self.login(member_client, member_id)
        self.assertEqual(member_client.get(f"/tree/project/{project_id}").status_code, 200)

        self.login(self.client, owner_id)
        remove_response = self.client.delete(f"/api/projects/{project_id}/teams/{team_id}")
        self.assertEqual(remove_response.status_code, 200)
        self.assertEqual(member_client.get(f"/api/projects/{project_id}/tree_snapshot").status_code, 403)

    def test_direct_project_tree_snapshot_matches_assignments_for_both_users(self):
        with self.app.app_context():
            user_a = self.create_user("brunnels@iastate.edu", "Brunnels")
            user_b = self.create_user("bsrunnels@gmail.com", "Bsrunnels")
            user_a_id = sqlalchemy_inspect(user_a).identity[0]
            user_b_id = sqlalchemy_inspect(user_b).identity[0]
            project = self.create_project(user_a, "Direct", is_direct=True, direct_peer=user_b)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Create seminar flyer", creator=user_a)
            self.add_assignment(task, user=user_a)
            self.add_assignment(task, user=user_b)

        client_a = self.app.test_client()
        client_b = self.app.test_client()
        self.login(client_a, user_a_id)
        self.login(client_b, user_b_id)

        response_a = client_a.get(f"/api/projects/{project_id}/tree_snapshot")
        response_b = client_b.get(f"/api/projects/{project_id}/tree_snapshot")
        payload_a = response_a.get_json()
        payload_b = response_b.get_json()

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual(payload_a["ungrouped_tasks"][0]["title"], "Create seminar flyer")
        self.assertEqual(payload_b["ungrouped_tasks"][0]["title"], "Create seminar flyer")
        self.assertEqual(
            sorted(item["display_email"] for item in payload_a["ungrouped_tasks"][0]["assignments"]),
            sorted(item["display_email"] for item in payload_b["ungrouped_tasks"][0]["assignments"]),
        )

    def test_tree_snapshot_reflects_latest_task_changes_after_update(self):
        with self.app.app_context():
            user_a = self.create_user("owner@example.com", "Owner")
            user_b = self.create_user("member@example.com", "Member")
            user_b_id = sqlalchemy_inspect(user_b).identity[0]
            project = self.create_project(user_a, "Shared", direct_peer=user_b)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Old title", creator=user_a)
            self.add_assignment(task, user=user_a)

            task.title = "New title"
            db.session.add(task)
            self.add_assignment(task, user=user_b)
            db.session.commit()

        client_b = self.app.test_client()
        self.login(client_b, user_b_id)
        response = client_b.get(f"/api/projects/{project_id}/tree_snapshot")
        payload = response.get_json()
        task_payload = payload["ungrouped_tasks"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(task_payload["title"], "New title")
        self.assertEqual(
            sorted(item["display_email"] for item in task_payload["assignments"]),
            ["member@example.com", "owner@example.com"],
        )

    def test_task_updated_socket_payload_includes_assignments(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Realtime", direct_peer=member)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Socket task", creator=owner)
            task_id = sqlalchemy_inspect(task).identity[0]
            self.add_assignment(task, user=owner)
            self.add_assignment(task, user=member)

        socket_client_http = self.app.test_client()
        self.login(socket_client_http, owner_id)
        socket_client = socketio.test_client(self.app, flask_test_client=socket_client_http)
        try:
            self.assertTrue(socket_client.is_connected())
            socket_client.emit("join_project", {"project_id": project_id})
            socket_client.get_received()

            with self.app.app_context():
                task = Task.query.get(task_id)
                emit_task_updated(task)

            received = socket_client.get_received()
            task_events = [event for event in received if event["name"] == "task_updated"]
            self.assertTrue(task_events, "Expected a task_updated socket event")
            payload = task_events[-1]["args"][0]["task"]
            self.assertEqual(payload["title"], "Socket task")
            self.assertEqual(
                sorted(item["display_email"] for item in payload["assignments"]),
                ["member@example.com", "owner@example.com"],
            )
        finally:
            socket_client.disconnect()

    def test_task_patch_emits_one_batch_socket_update_without_individual_duplicate(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            project = self.create_project(owner, "Realtime")
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Socket task", creator=owner)
            task_id = sqlalchemy_inspect(task).identity[0]

        socket_client_http = self.app.test_client()
        self.login(socket_client_http, owner_id)
        socket_client = socketio.test_client(self.app, flask_test_client=socket_client_http)
        try:
            self.assertTrue(socket_client.is_connected())
            socket_client.emit("join_project", {"project_id": project_id})
            socket_client.get_received()

            response = socket_client_http.patch(
                f"/api/tasks/{task_id}",
                json={"due_mode": "urgent", "due_at": ""},
            )
            self.assertEqual(response.status_code, 200)

            received = socket_client.get_received()
            batch_events = [event for event in received if event["name"] == "tasks_updated"]
            individual_events = [event for event in received if event["name"] == "task_updated"]
            self.assertEqual(len(batch_events), 1)
            self.assertEqual(individual_events, [])
            tasks = batch_events[0]["args"][0]["tasks"]
            self.assertEqual([row["id"] for row in tasks].count(task_id), 1)
            task_payload = next(row for row in tasks if row["id"] == task_id)
            self.assertEqual(task_payload["due_mode"], "urgent")
            self.assertTrue(task_payload["updated_at"])
        finally:
            socket_client.disconnect()

    def test_task_updated_socket_payload_is_viewer_specific_for_status_meta(self):
        with self.app.app_context():
            owner = self.create_user("owner@example.com", "Owner")
            member = self.create_user("member@example.com", "Member")
            owner_id = sqlalchemy_inspect(owner).identity[0]
            member_id = sqlalchemy_inspect(member).identity[0]
            project = self.create_project(owner, "Realtime", direct_peer=member)
            project_id = sqlalchemy_inspect(project).identity[0]
            task = self.create_task(project, "Socket status task", creator=owner, status="open")
            task_id = sqlalchemy_inspect(task).identity[0]
            self.add_assignment(task, user=owner)
            self.add_assignment(task, user=member)
            task.status_mode = "multi"
            task.per_user_status_enabled = True
            db.session.add(task)
            db.session.flush()
            db.session.add(TaskUserStatus(task_id=task.id, user_id=owner.id, status="open"))
            db.session.add(TaskUserStatus(task_id=task.id, user_id=member.id, status="critical"))
            db.session.commit()

        owner_http = self.app.test_client()
        member_http = self.app.test_client()
        self.login(owner_http, owner_id)
        self.login(member_http, member_id)
        owner_socket = socketio.test_client(self.app, flask_test_client=owner_http)
        member_socket = socketio.test_client(self.app, flask_test_client=member_http)
        try:
            self.assertTrue(owner_socket.is_connected())
            self.assertTrue(member_socket.is_connected())
            owner_socket.emit("join_project", {"project_id": project_id})
            member_socket.emit("join_project", {"project_id": project_id})
            owner_socket.get_received()
            member_socket.get_received()

            with self.app.app_context():
                task = Task.query.get(task_id)
                emit_task_updated(task)

            owner_events = [event for event in owner_socket.get_received() if event["name"] == "task_updated"]
            member_events = [event for event in member_socket.get_received() if event["name"] == "task_updated"]
            self.assertTrue(owner_events, "Expected owner task_updated event")
            self.assertTrue(member_events, "Expected member task_updated event")
            owner_task = owner_events[-1]["args"][0]["task"]
            member_task = member_events[-1]["args"][0]["task"]
            self.assertEqual(owner_task["status_meta"]["my_status"], "open")
            self.assertEqual(member_task["status_meta"]["my_status"], "critical")
            self.assertEqual(owner_task["status_meta"]["task_status"], "open")
            self.assertEqual(member_task["status_meta"]["task_status"], "open")
        finally:
            owner_socket.disconnect()
            member_socket.disconnect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
